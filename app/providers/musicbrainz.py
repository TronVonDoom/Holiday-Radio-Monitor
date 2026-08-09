"""MusicBrainz recording lookup.

MusicBrainz is the canonical-identity layer: it is free, needs no credentials,
and returns stable MBIDs. Its rate limit is one request per second per client,
which we honour with a global async lock rather than by sleeping optimistically.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .. import config, db
from ..normalize import artist_variants, title_variants

API = "https://musicbrainz.org/ws/2/recording"


class ProviderUnavailable(RuntimeError):
    """Raised when the service could not be reached, as opposed to finding nothing."""

_client: httpx.AsyncClient | None = None
_rate_lock = asyncio.Lock()
_last_request = 0.0


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


async def _throttled_get(params: dict[str, Any]) -> dict[str, Any] | None:
    """One request at a time, spaced by the configured minimum interval.

    Returns None only when the request genuinely failed. Callers must treat that
    differently from a successful search that found nothing, because loosening a
    query after a *failure* silently swaps a precise result set for a vague one.
    """
    global _last_request
    interval = db.get_float("musicbrainz_rate_limit_seconds", 1.1)

    for attempt in range(3):
        async with _rate_lock:
            wait = interval - (time.monotonic() - _last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                resp = await _get_client().get(API, params=params)
            except httpx.HTTPError:
                _last_request = time.monotonic()
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            finally:
                _last_request = time.monotonic()

        if resp.status_code in (503, 429):
            # Rate limited. Backing off and retrying preserves match quality;
            # giving up here would quietly degrade it.
            retry_after = resp.headers.get("Retry-After")
            delay = float(retry_after) if (retry_after or "").isdigit() else 1.5 * (attempt + 1)
            await asyncio.sleep(min(delay, 10.0))
            continue
        if resp.status_code != 200:
            return None
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

    # Precise queries: every plausible artist spelling against every plausible
    # title spelling, best guess first.
    attempts: list[tuple[str, str]] = [
        (a, t) for t in titles[:2] for a in artists[:3] if t
    ]

    seen_queries: set[str] = set()
    any_success = False

    for a, t in attempts:
        query = f'recording:"{_lucene_escape(t)}" AND artist:"{_lucene_escape(a)}"'
        if query in seen_queries:
            continue
        seen_queries.add(query)

        data = await _throttled_get({"query": query, "fmt": "json", "limit": limit})
        if data is None:
            continue  # request failed; do NOT treat as "no such recording"
        any_success = True
        for rec in data.get("recordings") or []:
            parsed = _parse_recording(rec)
            if parsed["ext_id"] and parsed["ext_id"] not in results:
                results[parsed["ext_id"]] = parsed

        # Enough signal to score confidently; stop spending rate limit.
        if len(results) >= limit:
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
    """Direct recording lookup for the ISRC, used only as a scoring tie-break."""
    global _last_request
    if not mbid:
        return ""
    interval = db.get_float("musicbrainz_rate_limit_seconds", 1.1)
    async with _rate_lock:
        wait = interval - (time.monotonic() - _last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            resp = await _get_client().get(
                f"{API}/{mbid}", params={"inc": "isrcs", "fmt": "json"}
            )
        except httpx.HTTPError:
            return ""
        finally:
            _last_request = time.monotonic()

    if resp.status_code != 200:
        return ""
    try:
        isrcs = resp.json().get("isrcs") or []
    except ValueError:
        return ""
    return isrcs[0] if isrcs else ""
