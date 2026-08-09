"""MusicBrainz recording lookup.

MusicBrainz is the canonical-identity layer: it is free, needs no credentials,
and returns stable MBIDs. Its rate limit is one request per second per client,
which we honour with a global async lock rather than by sleeping optimistically.

Spacing alone is not enough, though. The /ws/2 search endpoint is defended more
aggressively than a plain lookup, and a station monitor that keeps the matcher
busy will sit at the full one-per-second budget for hours. When MusicBrainz
eventually pushes back with a 503, retrying each spelling in turn keeps sending
requests at full rate to a service that just said no - which is both rude and
self-defeating, because it turns a throttle into a ten-minute stall per song.

So backpressure short-circuits the whole provider: a 503 or 429 opens a
cooldown, every caller is refused locally without a request until it expires,
and matching degrades to Spotify-only instead of grinding to a halt.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .. import config, db
from ..normalize import artist_variants, title_variants

API = "https://musicbrainz.org/ws/2/recording"

# A native relevance score this high means MusicBrainz is confident it found the
# recording, so the remaining spellings would only re-find the same one.
STRONG_NATIVE_SCORE = 90

# Escalating cooldown multipliers applied to `musicbrainz_cooldown_seconds`. A
# one-off throttle costs a minute; a sustained block backs off to half an hour
# rather than probing every minute forever.
COOLDOWN_STEPS = (1, 3, 10, 30)


class ProviderUnavailable(RuntimeError):
    """Raised when the service could not be reached, as opposed to finding nothing."""


class ProviderThrottled(ProviderUnavailable):
    """MusicBrainz asked us to slow down.

    Callers must abandon the entire search rather than trying another spelling:
    the next request would land on a service that has already refused this one.
    """

    def __init__(self, message: str, retry_in: float = 0.0) -> None:
        super().__init__(message)
        self.retry_in = retry_in


_client: httpx.AsyncClient | None = None
_rate_lock = asyncio.Lock()
_last_request = 0.0

# Circuit breaker state. `_throttle_streak` only resets on a successful request,
# so repeated throttling escalates instead of settling into a probing loop.
_cooldown_until = 0.0
_throttle_streak = 0


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"},
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _parse_retry_after(value: str | None) -> float | None:
    """Seconds to wait, from either Retry-After form. None when absent/unusable.

    The header is a delta-seconds integer in practice, but the spec also allows
    an HTTP-date, and the previous `.isdigit()` test silently discarded both a
    date and a fractional value in favour of a much shorter guess.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def cooldown_remaining() -> float:
    """Seconds until MusicBrainz may be called again. 0 when it is available."""
    return max(0.0, _cooldown_until - time.monotonic())


def status() -> dict[str, Any]:
    """Breaker state, so a cold provider is visible instead of looking idle."""
    remaining = cooldown_remaining()
    return {
        "throttled": remaining > 0,
        "cooldown_seconds": round(remaining, 1),
        "throttle_streak": _throttle_streak,
    }


def _open_breaker(retry_after: float | None) -> float:
    """Record backpressure and refuse local calls until the cooldown expires."""
    global _cooldown_until, _throttle_streak
    _throttle_streak += 1
    base = max(5.0, db.get_float("musicbrainz_cooldown_seconds", 60.0))
    step = base * COOLDOWN_STEPS[min(_throttle_streak, len(COOLDOWN_STEPS)) - 1]
    # Honour Retry-After in full when MusicBrainz names a delay, but never wait
    # less than our own escalating floor.
    delay = max(step, retry_after or 0.0)
    _cooldown_until = max(_cooldown_until, time.monotonic() + delay)
    db.log_event(
        f"MusicBrainz asked us to back off; pausing lookups for {delay:.0f}s "
        f"(consecutive throttles: {_throttle_streak}). Matching continues on "
        "Spotify alone until then.",
        level="warn", source="musicbrainz",
    )
    return delay


def _close_breaker() -> None:
    """A successful request means we are inside the budget again."""
    global _throttle_streak, _cooldown_until
    if _throttle_streak:
        db.log_event("MusicBrainz is responding normally again.",
                     level="info", source="musicbrainz")
    _throttle_streak = 0
    _cooldown_until = 0.0


async def _throttled_get(params: dict[str, Any]) -> dict[str, Any] | None:
    """One request at a time, spaced by the configured minimum interval.

    Returns None only when the request genuinely failed. Callers must treat that
    differently from a successful search that found nothing, because loosening a
    query after a *failure* silently swaps a precise result set for a vague one.

    Raises ProviderThrottled when MusicBrainz is pushing back - either because a
    cooldown is already open (in which case no request is sent at all) or because
    this response opened one.
    """
    global _last_request

    remaining = cooldown_remaining()
    if remaining > 0:
        raise ProviderThrottled(
            f"MusicBrainz is in cooldown for another {remaining:.0f}s", remaining
        )

    interval = db.get_float("musicbrainz_rate_limit_seconds", 1.1)

    # One retry, and only for a transport error. A refusal is never retried here:
    # that is the breaker's job.
    for attempt in range(2):
        resp: httpx.Response | None = None
        async with _rate_lock:
            wait = interval - (time.monotonic() - _last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                resp = await _get_client().get(API, params=params)
            except httpx.HTTPError:
                resp = None
            finally:
                _last_request = time.monotonic()

        if resp is None:
            if attempt == 0:
                await asyncio.sleep(1.5)
                continue
            return None

        if resp.status_code in (503, 429):
            delay = _open_breaker(_parse_retry_after(resp.headers.get("Retry-After")))
            raise ProviderThrottled(
                f"MusicBrainz returned {resp.status_code}; backing off {delay:.0f}s",
                delay,
            )
        if resp.status_code != 200:
            return None

        _close_breaker()
        try:
            return resp.json()
        except ValueError:
            return None

    return None


def _lucene_escape(value: str) -> str:
    for ch in r'+-&|!(){}[]^"~*?:\/':
        value = value.replace(ch, " ")
    return " ".join(value.split())


def _parse_recording(rec: dict[str, Any]) -> dict[str, Any]:
    credits = rec.get("artist-credit") or []
    artist = "".join(
        (c.get("name") or c.get("artist", {}).get("name") or "") + (c.get("joinphrase") or "")
        for c in credits
    ).strip()

    album = ""
    secondary_types: list[str] = []
    is_bootleg = False
    releases = rec.get("releases") or []
    if releases:
        # Prefer a studio album over a live record or compilation: a radio
        # station is overwhelmingly likely to be playing the studio cut, and a
        # live take can otherwise win on text alone when no duration is known.
        def release_rank(rel: dict[str, Any]) -> tuple[int, int, int]:
            group = rel.get("release-group") or {}
            secondary = [s.lower() for s in (group.get("secondary-types") or [])]
            status = (rel.get("status") or "").lower()
            return (
                1 if "live" in secondary else 0,
                1 if "compilation" in secondary else 0,
                0 if status == "official" else 1,
            )

        best = sorted(releases, key=release_rank)[0]
        album = best.get("title") or ""
        group = best.get("release-group") or {}
        secondary_types = [s.lower() for s in (group.get("secondary-types") or [])]
        is_bootleg = (best.get("status") or "").lower() == "bootleg"

    length_ms = rec.get("length")
    duration = int(round(length_ms / 1000)) if isinstance(length_ms, (int, float)) else None

    return {
        "source": "musicbrainz",
        "ext_id": rec.get("id") or "",
        "artist": artist,
        "title": rec.get("title") or "",
        "album": album,
        "duration": duration,
        "url": f"https://musicbrainz.org/recording/{rec.get('id')}" if rec.get("id") else "",
        "uri": "",
        "art_url": "",
        "isrc": (rec.get("isrcs") or [""])[0] if rec.get("isrcs") else "",
        "native_score": rec.get("score") or 0,
        "secondary_types": secondary_types,
        "is_bootleg": is_bootleg,
    }


async def search(artist: str, title: str, limit: int = 8) -> list[dict[str, Any]]:
    """Search recordings across plausible artist/title spellings.

    Queries the strongest artist/title combination first and stops as soon as a
    variant returns results, so the common case costs a single request.
    """
    results: dict[str, dict[str, Any]] = {}
    artists = artist_variants(artist) or [artist]
    titles = title_variants(title) or [title]

    # Precise queries: plausible artist spellings against plausible title
    # spellings, best guess first. Capped at 2x2 rather than 2x3 - every extra
    # variant costs a full second of the shared rate budget, and the third
    # artist form is a long shot that the title-only rescue below covers anyway.
    attempts: list[tuple[str, str]] = [
        (a, t) for t in titles[:2] for a in artists[:2] if t
    ]

    seen_queries: set[str] = set()
    any_success = False

    for a, t in attempts:
        query = f'recording:"{_lucene_escape(t)}" AND artist:"{_lucene_escape(a)}"'
        if query in seen_queries:
            continue
        seen_queries.add(query)

        # ProviderThrottled propagates: once MusicBrainz has refused us, trying
        # the next spelling would just be another refused request.
        data = await _throttled_get({"query": query, "fmt": "json", "limit": limit})
        if data is None:
            continue  # request failed; do NOT treat as "no such recording"
        any_success = True
        strong = False
        for rec in data.get("recordings") or []:
            parsed = _parse_recording(rec)
            if parsed["ext_id"] and parsed["ext_id"] not in results:
                results[parsed["ext_id"]] = parsed
            if parsed["native_score"] >= STRONG_NATIVE_SCORE:
                strong = True

        # Enough signal to score confidently; stop spending rate limit. A strong
        # native hit on this spelling ends the search even when it returned only
        # a couple of rows, which is the common case and the main volume saving.
        if strong or len(results) >= limit:
            break

    # Title-only search rescues a badly mangled artist field, but it is
    # inherently low precision: every unrelated song sharing the title comes
    # back. Only reach for it when the precise queries actually ran and found
    # nothing - never to paper over a failed request.
    if not any_success:
        # Every query failed (rate limiting or a network problem). Signalling this
        # keeps the caller from recording a permanent "not found" for a song that
        # was simply never actually looked up.
        raise ProviderUnavailable("MusicBrainz did not respond to any query")

    if not results and titles:
        query = f'recording:"{_lucene_escape(titles[0])}"'
        data = await _throttled_get({"query": query, "fmt": "json", "limit": limit})
        for rec in (data or {}).get("recordings") or []:
            parsed = _parse_recording(rec)
            if parsed["ext_id"]:
                results[parsed["ext_id"]] = parsed

    return list(results.values())


async def fetch_isrc(mbid: str) -> str:
    """Direct recording lookup for the ISRC, used only as a scoring tie-break.

    Returns "" on any failure. Being a tie-break, it is never worth waiting on a
    cooldown for, so an open breaker skips the request entirely - and a refusal
    here feeds the breaker like any other.
    """
    global _last_request
    if not mbid or cooldown_remaining() > 0:
        return ""
    interval = db.get_float("musicbrainz_rate_limit_seconds", 1.1)
    resp: httpx.Response | None = None
    async with _rate_lock:
        wait = interval - (time.monotonic() - _last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            resp = await _get_client().get(
                f"{API}/{mbid}", params={"inc": "isrcs", "fmt": "json"}
            )
        except httpx.HTTPError:
            resp = None
        finally:
            _last_request = time.monotonic()

    if resp is None:
        return ""
    if resp.status_code in (503, 429):
        _open_breaker(_parse_retry_after(resp.headers.get("Retry-After")))
        return ""
    if resp.status_code != 200:
        return ""
    _close_breaker()
    try:
        isrcs = resp.json().get("isrcs") or []
    except ValueError:
        return ""
    return isrcs[0] if isrcs else ""
