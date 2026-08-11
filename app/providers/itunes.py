"""Apple Music / iTunes Search lookup.

The fourth opinion, and the one that most often has something the other three do
not. Holiday radio leans hard on novelty singles, seasonal compilations and
small-label reissues - exactly the corner of recorded music where MusicBrainz's
coverage thins out and where Spotify and Deezer, which both ingest from the same
modern distributors, tend to agree with each other about nothing being there.
The iTunes Store catalogued that material commercially for two decades, so it
answers for a lot of the stubborn tail.

It is deliberately the weakest-privileged provider of the four:

  * No credentials, but also **no ISRC**, so it can corroborate an identity and
    supply a duration - it can never assert one exactly the way Deezer can.
  * **No fielded query syntax.** Everything is one free-text `term`, so the
    careful artist/title separation the other providers get is flattened here.
    Its own relevance ranking is good at absorbing that, which is precisely why
    it is worth asking when a fielded search has already failed.
  * A real rate limit - roughly 20 requests a minute per IP, answered with a
    bare 403 - which is tight enough that this is the one provider besides
    MusicBrainz that needs its requests spaced rather than merely throttled on
    refusal. Hence the lock below, and hence a resolve spending at most two
    requests here where Deezer may spend four.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .. import config, db
from ..normalize import artist_variants, title_variants
from .backoff import Breaker, Meter

API = "https://itunes.apple.com/search"

REQUEST_TIMEOUT = 8.0
SEARCH_BUDGET = 12.0
INTERACTIVE_BUDGET = 6.0

# The US store, always, and deliberately not the Spotify market setting. Nothing
# here is ever played from Apple - the identification is the whole product, and
# playlist delivery goes to Spotify and .m3u8 - so the only thing a storefront
# changes for us is how much of the catalogue is visible. The US store carries
# the deepest holiday and novelty back catalogue, so it is the one that answers
# the question we are actually asking: does this recording exist at all?
STOREFRONT = "US"

# Search results come back in the provider's own relevance order. There is no
# numeric score to read, and `native_score` is only ever a final tie-break
# between candidates that already scored identically, so position is exactly the
# right thing to put there - it is Apple's opinion of which is the better answer.
POSITION_STEP = 5


class ITunesUnavailable(RuntimeError):
    """Raised when the service could not be reached, as opposed to finding nothing."""


class ITunesThrottled(ITunesUnavailable):
    """Apple asked us to slow down; abandon the remaining spellings."""

    def __init__(self, message: str, retry_in: float = 0.0) -> None:
        super().__init__(message)
        self.retry_in = retry_in


_client: httpx.AsyncClient | None = None
_rate_lock = asyncio.Lock()
_last_request = 0.0
_meter = Meter()

# Apple never says how long to wait, so this escalates the MusicBrainz way
# rather than the gentle way: guessing long is the safe direction when the
# service has told us nothing.
_breaker = Breaker(
    "Apple Music", "itunes_cooldown_seconds", default_seconds=60.0,
    max_seconds=1800.0,
    fallback_note="Matching continues on the other providers until then.",
)


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT),
            headers={"User-Agent": config.USER_AGENT},
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def cooldown_remaining() -> float:
    """Seconds until Apple may be called again. 0 when it is available."""
    return _breaker.remaining()


def status() -> dict[str, Any]:
    """Breaker state and send rate, so a cold provider is visible instead of
    looking idle - and a provider heading for a cooldown is visible before it is."""
    return {**_breaker.status(), **_meter.snapshot()}


def resume() -> float:
    """Clear the cooldown now. Returns the seconds of waiting skipped."""
    return _breaker.resume()


def is_configured() -> bool:
    """The Search API needs no credentials, so it is always available."""
    return True


def _artwork(item: dict[str, Any]) -> str:
    """Largest artwork the search response can be talked into giving us.

    Only the 30/60/100px thumbnails are returned, but the size is a path
    segment, so asking for a bigger one works and degrades to the 100px URL if
    Apple ever stops honouring that.
    """
    url = item.get("artworkUrl100") or item.get("artworkUrl60") or item.get("artworkUrl30") or ""
    return url.replace("100x100bb", "600x600bb") if url else ""


def _parse_track(item: dict[str, Any], position: int) -> dict[str, Any]:
    millis = item.get("trackTimeMillis")
    # A track credited to one artist but collected under "Various Artists" is on
    # a compilation, which the scorer discounts slightly - a station is more
    # likely playing the original release than a seasonal repackaging of it.
    collection_artist = (item.get("collectionArtistName") or "").strip().lower()

    return {
        "source": "itunes",
        "ext_id": str(item.get("trackId") or ""),
        "artist": item.get("artistName") or "",
        "title": item.get("trackName") or item.get("trackCensoredName") or "",
        "album": item.get("collectionName") or "",
        "duration": int(round(millis / 1000)) if isinstance(millis, (int, float)) and millis else None,
        "url": item.get("trackViewUrl") or item.get("collectionViewUrl") or "",
        # Not playable through Spotify, and with no ISRC to look one up by,
        # `enrich_with_spotify` falls back to a text search for these.
        "uri": "",
        "art_url": _artwork(item),
        "isrc": "",
        "native_score": max(0, 100 - position * POSITION_STEP),
        "secondary_types": ["compilation"] if collection_artist == "various artists" else [],
        "is_bootleg": False,
    }


async def _get(params: dict[str, Any]) -> list[dict[str, Any]] | None:
    """One spaced search request. None means it failed, [] means no results."""
    global _last_request

    remaining = cooldown_remaining()
    if remaining > 0:
        raise ITunesThrottled(f"Apple Music is in cooldown for another {remaining:.0f}s",
                              remaining)

    interval = db.get_float("itunes_rate_limit_seconds", 3.0)
    resp: httpx.Response | None = None
    async with _rate_lock:
        wait = interval - (time.monotonic() - _last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        _meter.record()
        try:
            resp = await _get_client().get(API, params=params)
        except httpx.HTTPError:
            resp = None
        finally:
            _last_request = time.monotonic()

    if resp is None:
        return None

    # 403 is how the Search API reports "too many requests" - it carries no
    # Retry-After and no explanation, so it is treated as backpressure rather
    # than as a permission problem there is nothing to fix.
    if resp.status_code in (403, 429, 503):
        delay = _breaker.open(resp.headers.get("Retry-After"), resp.status_code)
        raise ITunesThrottled(f"Apple Music returned {resp.status_code}; "
                              f"backing off {delay:.0f}s", delay)
    if resp.status_code != 200:
        return None

    try:
        # The endpoint answers with `text/javascript`, so the body has to be
        # parsed regardless of what the content type claims.
        payload = resp.json()
    except ValueError:
        return None

    _breaker.close()
    results = payload.get("results")
    return results if isinstance(results, list) else []


async def search(artist: str, title: str, limit: int = 8, *,
                 interactive: bool = False) -> list[dict[str, Any]]:
    """Search songs by free text, best guess first.

    Only two requests are ever spent: the strongest artist/title pairing, and -
    when that finds nothing at all - the title on its own. There is no fielded
    syntax to make a third spelling more precise than the first, so extra
    attempts would buy nothing but rate budget, and the budget here is the
    tightest of the four providers.
    """
    results: dict[str, dict[str, Any]] = {}
    artists = artist_variants(artist) or [artist]
    titles = title_variants(title) or [title]
    if not titles:
        return []

    deadline = time.monotonic() + (INTERACTIVE_BUDGET if interactive else SEARCH_BUDGET)
    params = {"media": "music", "entity": "song", "country": STOREFRONT,
              "limit": max(1, min(limit, 25))}

    term = " ".join(p for p in (artists[0] if artists else "", titles[0]) if p).strip()
    # ITunesThrottled propagates: the rescue query would be refused too.
    data = await _get({**params, "term": term})
    if data is None:
        raise ITunesUnavailable("Apple Music did not respond")

    for position, item in enumerate(data):
        parsed = _parse_track(item, position)
        if parsed["ext_id"] and parsed["ext_id"] not in results:
            results[parsed["ext_id"]] = parsed

    if not results and time.monotonic() < deadline:
        # A mangled artist field is the usual reason the combined term missed,
        # and the title alone is the only other question worth asking.
        data = await _get({**params, "term": titles[0]})
        for position, item in enumerate(data or []):
            parsed = _parse_track(item, position)
            if parsed["ext_id"]:
                results[parsed["ext_id"]] = parsed

    return list(results.values())
