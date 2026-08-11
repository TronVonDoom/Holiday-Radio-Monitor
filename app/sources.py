"""Stream metadata sources.

Three ways to learn what a station is playing, tried in order of quality:

  1. AzuraCast JSON API   - richest by far. Separate artist/title fields, exact
                            track duration, album art, and a 15-deep play
                            history so a missed poll loses nothing.
  2. Icecast status-json  - separate artist/title and no audio transfer.
  3. Inline ICY metadata  - last resort; requires opening the audio stream and
                            yields only a combined "Artist - Title" string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from . import config, db

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
            headers={"User-Agent": config.USER_AGENT},
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


@dataclass
class Observation:
    """One "this played" report, in whatever detail the source could give.

    Carries no source-side identifier on purpose. AzuraCast offers one and it
    was used as the song's identity, which quietly gave every station - and
    every spelling of the metadata - its own copy of the same song. Identity is
    derived from the normalized artist and title instead; see
    `normalize.fingerprint` for the full account.
    """

    artist: str
    title: str
    album: str = ""
    duration: int | None = None
    played_at: int = 0
    art_url: str = ""
    is_current: bool = False


def _split_combined(text: str) -> tuple[str, str]:
    """Split a combined 'Artist - Title' string, the ICY-only fallback path."""
    text = (text or "").strip()
    if not text:
        return "", ""
    for sep in (" - ", " – ", " — ", " | "):
        if sep in text:
            artist, title = text.split(sep, 1)
            return artist.strip(), title.strip()
    return "", text


# --- AzuraCast ---------------------------------------------------------------

class DiscoveryError(RuntimeError):
    """No AzuraCast station list could be found at the address given."""


# Enough to cover a deep mount path without turning one click into a scan.
MAX_DISCOVERY_BASES = 8
DISCOVERY_TIMEOUT = httpx.Timeout(6.0)


def discovery_bases(url: str) -> list[str]:
    """Candidate AzuraCast roots for whatever was pasted, best guess first.

    People reach for the address they already have, which is usually a stream
    mount like https://host/radio/8020/hro-aac - but the station list lives at
    the server root. AzuraCast can equally be installed under a subpath, so the
    typed path is tried first and then trimmed one segment at a time.
    """
    raw = (url or "").strip()
    if not raw:
        return []
    if not re.match(r"^https?://", raw, re.I):
        raw = f"https://{raw}"

    parts = urlparse(raw)
    if not parts.hostname or any(c.isspace() for c in parts.netloc):
        return []

    path = parts.path.rstrip("/")
    for suffix in ("/api/stations", "/api/nowplaying", "/api"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    segments = [s for s in path.split("/") if s]

    netlocs = [parts.netloc]
    # A mount URL usually carries the streaming port; the web API answers on
    # the normal one, so the bare host is worth a try too.
    if parts.port and parts.port not in (80, 443):
        netlocs.append(parts.hostname or "")

    bases = [
        urlunparse((parts.scheme, netloc,
                    "/" + "/".join(segments[:depth]) if depth else "", "", "", ""))
        for netloc in netlocs
        for depth in range(len(segments), -1, -1)
    ]
    return list(dict.fromkeys(bases))[:MAX_DISCOVERY_BASES]


async def discover_azuracast(base_url: str) -> tuple[str, list[dict[str, Any]]]:
    """List every station on an AzuraCast server. Returns (base_used, stations)."""
    candidates = discovery_bases(base_url)
    if not candidates:
        raise DiscoveryError("Enter the address of the AzuraCast server.")

    for base in candidates:
        try:
            resp = await _get_client().get(f"{base}/api/stations",
                                           timeout=DISCOVERY_TIMEOUT)
            resp.raise_for_status()
            stations = resp.json()
        except Exception:  # noqa: BLE001 - any failure just means "not here"
            continue
        # A JSON array from /api/stations is AzuraCast answering; an empty one
        # is a real answer too, so stop rather than keep climbing.
        if not isinstance(stations, list):
            continue

        out = []
        for s in stations:
            mounts = s.get("mounts") or []
            listen = s.get("listen_url") or (mounts[0].get("url") if mounts else "")
            out.append({
                "name": s.get("name") or "",
                "shortcode": s.get("shortcode") or "",
                "description": s.get("description") or "",
                "listen_url": listen,
                "azuracast_base": base,
            })
        return base, out

    root = candidates[-1]
    raise DiscoveryError(
        f"No AzuraCast station list at that address. Give Discover the server "
        f"itself — {root} — rather than a stream or mount URL."
    )


def _parse_azuracast_song(song: dict[str, Any], duration: int | None,
                          played_at: int, is_current: bool) -> Observation:
    return Observation(
        artist=(song.get("artist") or "").strip(),
        title=(song.get("title") or "").strip(),
        album=(song.get("album") or "").strip(),
        duration=duration if duration and duration > 0 else None,
        played_at=played_at,
        art_url=(song.get("art") or "").strip(),
        is_current=is_current,
    )


def _observations_from_nowplaying(data: dict[str, Any]) -> list[Observation]:
    """Read one station's nowplaying document into observations."""
    out: list[Observation] = []

    now_playing = data.get("now_playing") or {}
    song = now_playing.get("song") or {}
    if song.get("title") or song.get("artist"):
        out.append(_parse_azuracast_song(
            song,
            now_playing.get("duration"),
            int(now_playing.get("played_at") or 0),
            is_current=True,
        ))

    # History lets a 45s poll interval cover a station reliably: even after a
    # restart or an outage we backfill everything we missed.
    for entry in data.get("song_history") or []:
        hsong = entry.get("song") or {}
        if not (hsong.get("title") or hsong.get("artist")):
            continue
        out.append(_parse_azuracast_song(
            hsong,
            entry.get("duration"),
            int(entry.get("played_at") or 0),
            is_current=False,
        ))

    return out


async def fetch_azuracast(base_url: str, shortcode: str) -> list[Observation]:
    base = base_url.rstrip("/")
    resp = await _get_client().get(f"{base}/api/nowplaying/{shortcode}")
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):  # some versions wrap the station in a list
        data = data[0] if data else {}
    return _observations_from_nowplaying(data)


async def fetch_azuracast_server(base_url: str) -> dict[str, list[Observation]]:
    """Every station on one AzuraCast server, keyed by shortcode, in ONE request.

    `/api/nowplaying` returns the same document `/api/nowplaying/{shortcode}`
    does, for every station on the server at once, and AzuraCast serves it from
    a cache it refreshes on song change. Asking for it once and splitting the
    answer is strictly cheaper for the server than asking per station - and it
    is the endpoint AzuraCast itself recommends for polling.

    This matters more than the request count suggests. These are somebody else's
    servers: four stations on one host, polled concurrently every 45 seconds
    forever, is a bot-shaped traffic pattern aimed at infrastructure we do not
    own and cannot get unbanned from. Losing a music catalogue costs us some
    confidence; losing the metadata source costs us everything, because there is
    nothing left to match.

    Raises on anything unexpected so the caller can fall back to per-station
    polling: not every deployment exposes the unscoped endpoint.
    """
    base = base_url.rstrip("/")
    resp = await _get_client().get(f"{base}/api/nowplaying")
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise RuntimeError("/api/nowplaying did not return a list of stations")

    out: dict[str, list[Observation]] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        shortcode = ((entry.get("station") or {}).get("shortcode") or "").strip()
        if shortcode:
            out[shortcode] = _observations_from_nowplaying(entry)
    if not out:
        raise RuntimeError("/api/nowplaying carried no station shortcodes")
    return out


# --- Icecast -----------------------------------------------------------------

def _icecast_status_url(stream_url: str) -> str:
    parts = urlparse(stream_url)
    return urlunparse((parts.scheme, parts.netloc, "/status-json.xsl", "", "", ""))


async def fetch_icecast_status(stream_url: str) -> list[Observation]:
    """Read the mount's current track from Icecast's status document."""
    resp = await _get_client().get(_icecast_status_url(stream_url))
    resp.raise_for_status()
    data = resp.json()
    sources = (data.get("icestats") or {}).get("source") or []
    if isinstance(sources, dict):
        sources = [sources]

    wanted_path = urlparse(stream_url).path.rstrip("/")
    chosen: dict[str, Any] | None = None
    for src in sources:
        listen = src.get("listenurl") or ""
        if urlparse(listen).path.rstrip("/") == wanted_path:
            chosen = src
            break
    if chosen is None and sources:
        chosen = sources[0]
    if chosen is None:
        return []

    artist = (chosen.get("artist") or "").strip()
    title = (chosen.get("title") or "").strip()
    if not artist and not title:
        artist, title = _split_combined(chosen.get("yp_currently_playing") or "")
    if not artist and not title:
        return []

    return [Observation(artist=artist, title=title, played_at=0, is_current=True)]


# --- Raw ICY -----------------------------------------------------------------

ICY_TITLE_RE = re.compile(r"StreamTitle='(.*?)';", re.S)


async def fetch_icy(stream_url: str) -> list[Observation]:
    """Pull just enough audio to read one inline ICY metadata block."""
    headers = {"Icy-MetaData": "1", "User-Agent": config.USER_AGENT}
    async with _get_client().stream("GET", stream_url, headers=headers) as resp:
        resp.raise_for_status()
        try:
            interval = int(resp.headers.get("icy-metaint", "0"))
        except ValueError:
            interval = 0
        if interval <= 0:
            return []

        buffer = bytearray()
        need = interval + 1
        async for chunk in resp.aiter_bytes(4096):
            buffer.extend(chunk)
            if len(buffer) < need:
                continue
            length = buffer[interval] * 16
            if length == 0:
                # Metadata unchanged since the last block; nothing to report.
                return []
            need = interval + 1 + length
            if len(buffer) < need:
                continue
            blob = bytes(buffer[interval + 1: interval + 1 + length])
            text = blob.decode("utf-8", errors="replace").rstrip("\x00")
            match = ICY_TITLE_RE.search(text)
            if not match:
                return []
            artist, title = _split_combined(match.group(1))
            if not artist and not title:
                return []
            return [Observation(artist=artist, title=title, played_at=0, is_current=True)]
    return []


# --- dispatch ----------------------------------------------------------------

async def fetch_station(station: dict[str, Any]) -> tuple[list[Observation], str]:
    """Return (observations, source_name), raising only if every source fails."""
    errors: list[str] = []

    if station.get("azuracast_base") and station.get("azuracast_shortcode"):
        try:
            obs = await fetch_azuracast(station["azuracast_base"], station["azuracast_shortcode"])
            if obs:
                return obs, "azuracast"
            errors.append("azuracast: empty response")
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI as last_error
            errors.append(f"azuracast: {exc}")

    if station.get("icy_url"):
        try:
            obs = await fetch_icecast_status(station["icy_url"])
            if obs:
                return obs, "icecast"
            errors.append("icecast: mount not found")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"icecast: {exc}")

        try:
            obs = await fetch_icy(station["icy_url"])
            if obs:
                return obs, "icy"
            errors.append("icy: no metadata block")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"icy: {exc}")

    raise RuntimeError(" | ".join(errors) or "No metadata source configured")


async def probe(azuracast_base: str = "", azuracast_shortcode: str = "",
                icy_url: str = "") -> dict[str, Any]:
    """Test a station's configuration and report what we can read from it."""
    station = {
        "azuracast_base": azuracast_base,
        "azuracast_shortcode": azuracast_shortcode,
        "icy_url": icy_url,
    }
    try:
        observations, source = await fetch_station(station)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    current = next((o for o in observations if o.is_current), observations[0])
    return {
        "ok": True,
        "source": source,
        "now_playing": {
            "artist": current.artist,
            "title": current.title,
            "album": current.album,
            "duration": current.duration,
            "art_url": current.art_url,
        },
        "history_depth": len(observations),
    }
