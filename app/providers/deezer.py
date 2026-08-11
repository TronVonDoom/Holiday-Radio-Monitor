"""Deezer track lookup.

Added because "unmatched" was hiding two completely different problems: songs
whose metadata we could not repair, and songs that MusicBrainz and Spotify
simply do not carry under the spelling the station broadcast. Only the second
kind is fixed by asking somebody else, and Deezer is the cheapest somebody else
there is - no credentials, no application quota, no OAuth dance.

It earns its place for one reason above catalogue size: **the search response
carries the ISRC**. No other provider here gives it away for free. MusicBrainz
charges a second request for it and Spotify only returns it on a track object.
That single field does two jobs downstream:

  * `matcher._is_duplicate` can assert identity exactly rather than inferring it
    from title, artist and length, so a recording found in three databases
    collapses to one candidate instead of two or three near-duplicates.
  * `matcher.enrich_with_spotify` can reach Spotify through `isrc:` - an exact
    lookup - instead of a second fuzzy text search. So a song Deezer identifies
    and Spotify's search could not find still lands in the Spotify playlist.

Deezer allows roughly 50 requests per 5 seconds per IP, and a resolve spends at
most three on it, so this is the one provider with genuine headroom. It is
spaced anyway, gently, because "we are two orders of magnitude inside the budget"
was also true of Spotify right up until the app was banned for most of a day:
the arithmetic was correct about the average and silent about the burst, and a
background queue draining a backlog is all burst. A tenth of a second between
calls costs nothing at this volume and removes the assumption entirely.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .. import config, db
from ..normalize import artist_variants, title_variants
from .backoff import GENTLE_STEPS, Breaker, Meter

API = "https://api.deezer.com/search"

REQUEST_TIMEOUT = 8.0
SEARCH_BUDGET = 12.0
INTERACTIVE_BUDGET = 6.0

# Deezer's `rank` is a popularity figure in the hundreds of thousands, while
# MusicBrainz reports relevance and Spotify popularity on 0-100. `native_score`
# is only ever a final tie-break between candidates that already scored
# identically, so the scales have to be comparable or Deezer would win every
# tie by arithmetic rather than by being the better answer.
RANK_SCALE = 10_000
MAX_RANK = 1_000_000

# Error codes Deezer returns when it wants us to stop, as opposed to when it has
# nothing to say. 4 is the request quota; 700 is the service declaring itself
# busy. Everything else is a malformed query or a missing object, which retrying
# would not fix.
BACKPRESSURE_CODES = {4, 700}


class DeezerUnavailable(RuntimeError):
    """Raised when Deezer could not be reached, as opposed to finding nothing."""


class DeezerThrottled(DeezerUnavailable):
    """Deezer asked us to slow down; abandon the remaining spellings."""

    def __init__(self, message: str, retry_in: float = 0.0) -> None:
        super().__init__(message)
        self.retry_in = retry_in


_client: httpx.AsyncClient | None = None
_rate_lock = asyncio.Lock()
_last_request = 0.0
_meter = Meter()

# Deezer names no wait of its own, but its quota window is five seconds, so a
# refusal clears almost immediately and a generous floor would be pure dead
# time. Gentle escalation, short cap.
_breaker = Breaker(
    "Deezer", "deezer_cooldown_seconds", default_seconds=30.0, steps=GENTLE_STEPS,
    max_seconds=300.0,
    fallback_note="Matching continues on the other providers until then.",
)


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT),
            headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"},
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def cooldown_remaining() -> float:
    """Seconds until Deezer may be called again. 0 when it is available."""
    return _breaker.remaining()


def status() -> dict[str, Any]:
    """Breaker state and send rate, so a cold provider is visible instead of
    looking idle - and a provider heading for a cooldown is visible before it is."""
    return {**_breaker.status(), **_meter.snapshot()}


def resume() -> float:
    """Clear the cooldown now. Returns the seconds of waiting skipped."""
    return _breaker.resume()


def is_configured() -> bool:
    """Deezer's public search needs no credentials, so it is always available."""
    return True


def _quote(value: str) -> str:
    """Make a value safe inside Deezer's `field:"value"` query syntax.

    A stray double quote closes the field early and turns the rest of the title
    into loose keywords, which is how a precise search silently becomes a vague
    one. Backslashes are dropped for the same reason.
    """
    return " ".join(value.replace('"', " ").replace("\\", " ").split())


def _parse_track(item: dict[str, Any]) -> dict[str, Any]:
    artist = (item.get("artist") or {}).get("name") or ""
    album = item.get("album") or {}
    duration = item.get("duration")
    rank = item.get("rank") or 0

    return {
        "source": "deezer",
        "ext_id": str(item.get("id") or ""),
        "artist": artist,
        # `title` keeps the version suffix ("... (Live)") that `title_short`
        # discards, and the scorer's live and karaoke penalties read exactly
        # that suffix. Taking the short form here would hide a re-recording.
        "title": item.get("title") or item.get("title_short") or "",
        "album": album.get("title") or "",
        "duration": int(duration) if isinstance(duration, (int, float)) and duration else None,
        "url": item.get("link") or "",
        # Nothing here is playable through Spotify, so no URI. `enrich_with_spotify`
        # turns the ISRC below into one.
        "uri": "",
        "art_url": album.get("cover_medium") or album.get("cover_big") or album.get("cover") or "",
        "isrc": (item.get("isrc") or "").strip().upper(),
        "native_score": min(100, int(min(rank, MAX_RANK) // RANK_SCALE)),
        "secondary_types": [],
        "is_bootleg": False,
    }


async def _get(params: dict[str, Any]) -> list[dict[str, Any]] | None:
    """One search request. None means the request failed, [] means no results.

    Deezer signals a refusal with **HTTP 200** and an error object in the body
    rather than a 4xx, so a caller that only checks the status code reads a
    quota rejection as a song that does not exist and records it as permanently
    unmatched. That is the whole reason this function exists separately.
    """
    global _last_request

    remaining = cooldown_remaining()
    if remaining > 0:
        raise DeezerThrottled(f"Deezer is in cooldown for another {remaining:.0f}s", remaining)

    interval = db.get_float("deezer_rate_limit_seconds", 0.1)
    async with _rate_lock:
        # Re-checked inside the lock: a caller admitted while the breaker was
        # closed can reach the front of the queue after another has opened it.
        remaining = cooldown_remaining()
        if remaining > 0:
            raise DeezerThrottled(
                f"Deezer is in cooldown for another {remaining:.0f}s", remaining
            )
        wait = interval - (time.monotonic() - _last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        _meter.record()
        try:
            resp = await _get_client().get(API, params=params)
        except httpx.HTTPError:
            return None
        finally:
            _last_request = time.monotonic()

    if resp.status_code in (429, 503):
        delay = _breaker.open(resp.headers.get("Retry-After"), resp.status_code)
        raise DeezerThrottled(f"Deezer returned {resp.status_code}; backing off {delay:.0f}s",
                              delay)
    if resp.status_code != 200:
        return None

    try:
        payload = resp.json()
    except ValueError:
        return None

    error = payload.get("error")
    if isinstance(error, dict) and error:
        code = error.get("code")
        message = str(error.get("message") or error.get("type") or "refused")
        if code in BACKPRESSURE_CODES:
            # No Retry-After exists on this path - the refusal is not even an
            # HTTP error - so the breaker's own floor is all there is to go on.
            delay = _breaker.open(None, resp.status_code, message)
            raise DeezerThrottled(f"Deezer refused the search ({message}); "
                                  f"backing off {delay:.0f}s", delay)
        return None

    _breaker.close()
    data = payload.get("data")
    return data if isinstance(data, list) else []


async def search(artist: str, title: str, limit: int = 8, *,
                 interactive: bool = False) -> list[dict[str, Any]]:
    """Search tracks across plausible artist/title spellings.

    Mirrors the other providers: fielded queries first, best guess first, and a
    title-only rescue only when the precise queries genuinely ran and found
    nothing. `interactive` narrows the expansion for a search a person typed by
    hand, who does not need us second-guessing their spelling.
    """
    results: dict[str, dict[str, Any]] = {}
    artists = artist_variants(artist) or [artist]
    titles = title_variants(title) or [title]

    spellings = 1 if interactive else 2
    attempts = [(a, t) for t in titles[:spellings] for a in artists[:spellings] if t]

    seen: set[str] = set()
    any_success = False
    deadline = time.monotonic() + (INTERACTIVE_BUDGET if interactive else SEARCH_BUDGET)

    for a, t in attempts:
        query = f'artist:"{_quote(a)}" track:"{_quote(t)}"'
        if query in seen:
            continue
        seen.add(query)
        if time.monotonic() >= deadline:
            break

        # DeezerThrottled propagates: the next spelling would be refused too.
        data = await _get({"q": query, "limit": limit})
        if data is None:
            continue  # request failed; not evidence that the recording is absent
        any_success = True
        for item in data:
            parsed = _parse_track(item)
            if parsed["ext_id"] and parsed["ext_id"] not in results:
                results[parsed["ext_id"]] = parsed
        if len(results) >= 3:
            # Enough alternatives for the scorer to tell a real match from a
            # karaoke cut; the remaining spellings would re-find the same rows.
            break

    if not any_success:
        raise DeezerUnavailable("Deezer did not respond to any query")

    if not results and titles and time.monotonic() < deadline:
        # Low precision - every unrelated song sharing the title comes back - so
        # it is a last resort, and never a way to paper over a failed request.
        data = await _get({"q": f'track:"{_quote(titles[0])}"', "limit": limit})
        for item in data or []:
            parsed = _parse_track(item)
            if parsed["ext_id"]:
                results[parsed["ext_id"]] = parsed

    return list(results.values())
