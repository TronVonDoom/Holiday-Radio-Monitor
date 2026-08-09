"""Spotify client: search (for matching) and playlist writes (for delivery).

Two credential modes are supported and chosen automatically:

  * Client Credentials - app-only token, enough to *search*. Works as soon as
    the user pastes a client id/secret, with no browser round trip.
  * Authorization Code  - user token, required to *create and modify playlists*.
    Obtained once through the Settings page; the refresh token is stored in the
    database and renewed transparently from then on.
"""

from __future__ import annotations

import asyncio
import base64
import time
import urllib.parse
from typing import Any

import httpx

from .. import config, db
from ..normalize import artist_variants, title_variants

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"

SCOPES = "playlist-modify-public playlist-modify-private playlist-read-private"

_client: httpx.AsyncClient | None = None
_token_lock = asyncio.Lock()

# In-memory access tokens: (value, expires_at_monotonic)
_user_token: tuple[str, float] = ("", 0.0)
_app_token: tuple[str, float] = ("", 0.0)


class SpotifyError(RuntimeError):
    pass


class SpotifyAuthRequired(SpotifyError):
    """Raised when an operation needs a user login that has not happened yet."""


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(20.0))
    return _client


async def aclose() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def credentials() -> tuple[str, str]:
    return (
        db.get_setting("spotify_client_id", "").strip(),
        db.get_setting("spotify_client_secret", "").strip(),
    )


def is_configured() -> bool:
    cid, secret = credentials()
    return bool(cid and secret)


def is_user_linked() -> bool:
    return bool(db.get_setting("spotify_refresh_token", "").strip())


def _basic_auth_header() -> dict[str, str]:
    cid, secret = credentials()
    blob = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    return {"Authorization": f"Basic {blob}"}


# --- OAuth ------------------------------------------------------------------

def authorize_url(redirect_uri: str, state: str) -> str:
    cid, _ = credentials()
    if not cid:
        raise SpotifyError("Spotify client ID is not configured.")
    params = {
        "client_id": cid,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "show_dialog": "false",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


async def exchange_code(code: str, redirect_uri: str) -> None:
    resp = await _get_client().post(
        TOKEN_URL,
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
        headers={**_basic_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code != 200:
        raise SpotifyError(f"Token exchange failed ({resp.status_code}): {resp.text[:300]}")
    payload = resp.json()
    refresh = payload.get("refresh_token", "")
    if refresh:
        db.set_setting("spotify_refresh_token", refresh)
    global _user_token
    _user_token = (payload.get("access_token", ""), time.monotonic() + payload.get("expires_in", 3600) - 60)

    me = await _api_get("/me", user=True)
    db.set_setting("spotify_user_id", me.get("id", ""))
    db.set_setting("spotify_user_name", me.get("display_name") or me.get("id", ""))


def unlink() -> None:
    global _user_token
    _user_token = ("", 0.0)
    for key in ("spotify_refresh_token", "spotify_user_id", "spotify_user_name"):
        db.set_setting(key, "")


async def _user_access_token() -> str:
    global _user_token
    token, expires = _user_token
    if token and time.monotonic() < expires:
        return token

    async with _token_lock:
        token, expires = _user_token
        if token and time.monotonic() < expires:
            return token

        refresh = db.get_setting("spotify_refresh_token", "").strip()
        if not refresh:
            raise SpotifyAuthRequired("Connect your Spotify account in Settings first.")
        if not is_configured():
            raise SpotifyError("Spotify client ID/secret are not configured.")

        resp = await _get_client().post(
            TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": refresh},
            headers={**_basic_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            if resp.status_code in (400, 401):
                # Refresh token was revoked; force a clean re-link rather than
                # retrying a credential that can never succeed.
                db.set_setting("spotify_refresh_token", "")
                raise SpotifyAuthRequired("Spotify login expired. Reconnect in Settings.")
            raise SpotifyError(f"Token refresh failed ({resp.status_code}).")
        payload = resp.json()
        if payload.get("refresh_token"):
            db.set_setting("spotify_refresh_token", payload["refresh_token"])
        _user_token = (
            payload.get("access_token", ""),
            time.monotonic() + payload.get("expires_in", 3600) - 60,
        )
        return _user_token[0]


async def _app_access_token() -> str:
    global _app_token
    token, expires = _app_token
    if token and time.monotonic() < expires:
        return token

    async with _token_lock:
        token, expires = _app_token
        if token and time.monotonic() < expires:
            return token
        if not is_configured():
            raise SpotifyError("Spotify client ID/secret are not configured.")
        resp = await _get_client().post(
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            headers={**_basic_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            raise SpotifyError(f"Client-credentials auth failed ({resp.status_code}).")
        payload = resp.json()
        _app_token = (
            payload.get("access_token", ""),
            time.monotonic() + payload.get("expires_in", 3600) - 60,
        )
        return _app_token[0]


async def _token(user: bool) -> str:
    if user:
        return await _user_access_token()
    # Prefer the user token when present: same quota, and it keeps market
    # resolution consistent with the account the playlists live in.
    if is_user_linked():
        try:
            return await _user_access_token()
        except SpotifyError:
            pass
    return await _app_access_token()


# --- request plumbing --------------------------------------------------------

async def _request(method: str, path: str, *, user: bool = False,
                   params: dict | None = None, json: Any = None) -> Any:
    url = path if path.startswith("http") else f"{API}{path}"
    for attempt in range(4):
        token = await _token(user)
        resp = await _get_client().request(
            method, url, params=params, json=json,
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "2") or 2)
            await asyncio.sleep(min(retry_after, 30) + 0.25)
            continue
        if resp.status_code == 401 and attempt < 3:
            global _user_token, _app_token
            _user_token = ("", 0.0)
            _app_token = ("", 0.0)
            continue
        if resp.status_code >= 400:
            raise SpotifyError(f"Spotify {method} {path} failed ({resp.status_code}): {resp.text[:300]}")
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()
    raise SpotifyError(f"Spotify {method} {path} failed after retries (rate limited).")


async def _api_get(path: str, *, user: bool = False, params: dict | None = None) -> Any:
    return await _request("GET", path, user=user, params=params)


# --- search ------------------------------------------------------------------

def _parse_track(item: dict[str, Any]) -> dict[str, Any]:
    album = item.get("album") or {}
    images = album.get("images") or []
    duration_ms = item.get("duration_ms")
    return {
        "source": "spotify",
        "ext_id": item.get("id") or "",
        "artist": ", ".join(a.get("name", "") for a in item.get("artists") or []),
        "title": item.get("name") or "",
        "album": album.get("title") or album.get("name") or "",
        "duration": int(round(duration_ms / 1000)) if duration_ms else None,
        "url": (item.get("external_urls") or {}).get("spotify", ""),
        "uri": item.get("uri") or "",
        "art_url": images[-1]["url"] if images else (images[0]["url"] if images else ""),
        "isrc": (item.get("external_ids") or {}).get("isrc", ""),
        "native_score": item.get("popularity") or 0,
        "secondary_types": ["compilation"] if (album.get("album_type") == "compilation") else [],
    }


async def search(artist: str, title: str, limit: int = 8) -> list[dict[str, Any]]:
    """Search tracks across plausible artist/title spellings."""
    if not is_configured():
        return []

    market = db.get_setting("spotify_market", "US").strip() or "US"
    results: dict[str, dict[str, Any]] = {}
    artists = artist_variants(artist) or [artist]
    titles = title_variants(title) or [title]

    queries: list[str] = []
    for t in titles[:2]:
        for a in artists[:2]:
            queries.append(f'track:"{t}" artist:"{a}"')
    # Unfielded fallback: Spotify's relevance ranking often rescues metadata
    # that is too mangled for the strict field syntax.
    queries.append(f"{artists[0]} {titles[0]}")

    seen: set[str] = set()
    for query in queries:
        if query in seen:
            continue
        seen.add(query)
        try:
            data = await _api_get(
                "/search",
                params={"q": query, "type": "track", "limit": limit, "market": market},
            )
        except SpotifyError:
            continue
        for item in ((data.get("tracks") or {}).get("items") or []):
            parsed = _parse_track(item)
            if parsed["ext_id"] and parsed["ext_id"] not in results:
                results[parsed["ext_id"]] = parsed
        if len(results) >= limit:
            break

    return list(results.values())


async def search_isrc(isrc: str) -> list[dict[str, Any]]:
    """Exact lookup by recording code - the highest-precision search we have."""
    if not isrc or not is_configured():
        return []
    market = db.get_setting("spotify_market", "US").strip() or "US"
    try:
        data = await _api_get(
            "/search", params={"q": f"isrc:{isrc}", "type": "track", "limit": 5, "market": market}
        )
    except SpotifyError:
        return []
    return [_parse_track(i) for i in ((data.get("tracks") or {}).get("items") or [])]


async def get_track(track_id: str) -> dict[str, Any] | None:
    try:
        return _parse_track(await _api_get(f"/tracks/{track_id}"))
    except SpotifyError:
        return None


# --- playlists ---------------------------------------------------------------

async def current_user() -> dict[str, Any]:
    return await _api_get("/me", user=True)


async def ensure_playlist(playlist_id: str, name: str, description: str) -> str:
    """Return a usable playlist id, creating or repairing it as needed."""
    if playlist_id:
        try:
            existing = await _api_get(f"/playlists/{playlist_id}", user=True,
                                      params={"fields": "id"})
            if existing.get("id"):
                return existing["id"]
        except SpotifyError:
            # Deleted or no longer accessible - fall through and make a new one.
            pass

    user_id = db.get_setting("spotify_user_id", "").strip()
    if not user_id:
        user_id = (await current_user()).get("id", "")
        db.set_setting("spotify_user_id", user_id)

    created = await _request(
        "POST", f"/users/{user_id}/playlists", user=True,
        json={"name": name, "description": description[:300], "public": False},
    )
    return created.get("id", "")


async def playlist_track_uris(playlist_id: str) -> set[str]:
    uris: set[str] = set()
    url = f"/playlists/{playlist_id}/tracks"
    params: dict | None = {"fields": "items(track(uri)),next", "limit": 100}
    while url:
        data = await _api_get(url, user=True, params=params)
        for item in data.get("items") or []:
            track = item.get("track") or {}
            if track.get("uri"):
                uris.add(track["uri"])
        url = data.get("next") or ""
        params = None  # `next` already carries the query string.
    return uris


async def add_tracks(playlist_id: str, uris: list[str]) -> int:
    added = 0
    for i in range(0, len(uris), 100):
        chunk = uris[i:i + 100]
        await _request("POST", f"/playlists/{playlist_id}/tracks", user=True,
                       json={"uris": chunk})
        added += len(chunk)
    return added


async def remove_tracks(playlist_id: str, uris: list[str]) -> None:
    for i in range(0, len(uris), 100):
        chunk = [{"uri": u} for u in uris[i:i + 100]]
        await _request("DELETE", f"/playlists/{playlist_id}/tracks", user=True,
                       json={"tracks": chunk})
