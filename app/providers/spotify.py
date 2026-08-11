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
from .backoff import GENTLE_STEPS, Breaker, Meter

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"

# Spotify enforces a rolling-window quota per application, so throttling follows
# the matcher rather than the machine it runs on. Its 429 carries a Retry-After
# that must be taken at face value: clamping it to a shorter wait simply earns
# another 429, and retrying the same call four times over turns one refusal into
# minutes of dead time. See backoff.Breaker for why this is a hard stop.
#
# Unlike MusicBrainz, Spotify does say how long to wait. There are really two
# limits behind that one status code: an ordinary burst over the short rolling
# window is forgiven within seconds, while an application that keeps overrunning
# it is put in a penalty box for the better part of a day. Both arrive as a 429,
# and only the size of Retry-After distinguishes them.
#
# So our own floor stays small and escalates gently - it is only there to stop a
# tight probe loop after the mild kind. A genuinely long wait comes from
# Spotify's own Retry-After, which leads whenever it is longer, up to the cap.
#
# The cap is what stops a Retry-After we cannot verify from parking the provider
# indefinitely, but it was set far too low. Spotify answers a burst that trips its
# longer-window quota with a Retry-After measured in hours, and that figure is
# real: it counts down against a fixed unblock time, so nothing we do shortens it.
# Capping it to fifteen minutes did not get us back sooner, it just meant probing
# a service that had already said "not for another eighteen hours" seventy-odd
# times, every one of them a request the quota still counts.
#
# Six hours keeps the reason the cap exists - we still find out on our own if the
# refusal lifts early, or if the header was nonsense - at three probes across a
# day-long ban instead of seventy.
_breaker = Breaker(
    "Spotify", "spotify_cooldown_seconds", default_seconds=10.0, steps=GENTLE_STEPS,
    max_seconds=6 * 3600.0,
    fallback_note="Matching continues on MusicBrainz alone until then.",
)

# Total wall-clock one search may spend across all its query spellings.
SEARCH_BUDGET = 20.0

# The same ceiling for a search a person is sitting and waiting on. Nothing here
# should take long enough to notice, and a partial result now beats a complete
# one after a stall.
INTERACTIVE_BUDGET = 6.0

# user-library-modify is requested but not required: it is only used to tidy
# away the playlist the access test creates. Asking for it does not invalidate
# an existing link, and REQUIRED_SCOPES deliberately leaves it out so nobody is
# told to reconnect over a cleanup nicety.
SCOPES = ("playlist-modify-public playlist-modify-private playlist-read-private "
          "user-library-modify")

# Everything playlist delivery actually exercises. Kept separate from SCOPES so
# a link made by an older build can be checked against today's requirements.
REQUIRED_SCOPES = ("playlist-modify-private", "playlist-modify-public",
                   "playlist-read-private")

_client: httpx.AsyncClient | None = None
_token_lock = asyncio.Lock()

# One Spotify API call at a time, spaced by a minimum interval - the same
# arrangement MusicBrainz and Apple Music have always had, and the one thing this
# client was missing.
#
# Spotify's quota is a rolling window a few tens of seconds wide, so what trips
# it is not the daily total but the burst. Resolving a song that Spotify cannot
# find costs about eleven requests (five query spellings, an ISRC lookup, then
# the same again to attach a playable link), and the match loop runs eight songs
# a batch with a two second pause between batches. Unpaced, that is most of a
# hundred requests inside a couple of seconds, which is exactly the shape of
# traffic a rolling window exists to refuse.
#
# Spacing them costs throughput nobody is waiting on - the match queue is a
# background drain, not an interactive path - and it converts that burst into a
# steady trickle the window can absorb.
_rate_lock = asyncio.Lock()
_last_request = 0.0
_meter = Meter()

# In-memory access tokens: (value, expires_at_monotonic)
_user_token: tuple[str, float] = ("", 0.0)
_app_token: tuple[str, float] = ("", 0.0)


class SpotifyError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = "",
                 detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        # Spotify's own one-line explanation, unwrapped from its error envelope.
        self.detail = detail


class SpotifyAuthRequired(SpotifyError):
    """Raised when an operation needs a user login that has not happened yet."""


class SpotifyThrottled(SpotifyError):
    """Spotify asked us to slow down.

    A subclass of SpotifyError so playlist delivery still reports it like any
    other failure, but callers that loop over several queries must let it
    through: the next call would only be refused as well.
    """

    def __init__(self, message: str, retry_in: float = 0.0) -> None:
        super().__init__(message, status=429)
        self.retry_in = retry_in


def cooldown_remaining() -> float:
    """Seconds until Spotify may be called again. 0 when it is available."""
    return _breaker.remaining()


def status() -> dict[str, Any]:
    """Breaker state and send rate, so a cold provider is visible instead of
    looking idle - and a provider heading for a cooldown is visible before it is."""
    return {**_breaker.status(), **_meter.snapshot()}


def resume() -> float:
    """Clear the cooldown now. Returns the seconds of waiting skipped."""
    return _breaker.resume()


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


def granted_scopes() -> set[str]:
    """Scopes Spotify actually attached to the stored login.

    Empty when the link predates scope recording; every token refresh fills it
    in, so it becomes known on its own within an hour of use.
    """
    return {s for s in db.get_setting("spotify_scopes", "").split() if s}


def missing_scopes() -> list[str]:
    """Required scopes the login provably lacks. Empty when unknown."""
    granted = granted_scopes()
    if not granted:
        return []
    return [s for s in REQUIRED_SCOPES if s not in granted]


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
        # Force the consent screen. Silent re-authorization against a stale or
        # multi-account browser session is a reported source of Spotify
        # returning "server_error", and linking happens once, so always showing
        # the account chooser is the more reliable behaviour here.
        "show_dialog": "true",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def _store_user_token(payload: dict[str, Any]) -> None:
    """Cache the access token and record which scopes Spotify granted it.

    The scope list is the only reliable way to tell a login that can write
    playlists from one that merely looks connected, so it is captured from
    every token response - the initial exchange and each refresh alike.
    """
    global _user_token
    _user_token = (
        payload.get("access_token", ""),
        time.monotonic() + payload.get("expires_in", 3600) - 60,
    )
    if payload.get("scope") is not None:
        db.set_setting("spotify_scopes", payload.get("scope") or "")


async def exchange_code(code: str, redirect_uri: str) -> None:
    resp = await _get_client().post(
        TOKEN_URL,
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
        headers={**_basic_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code != 200:
        raise SpotifyError(f"Token exchange failed ({resp.status_code}): {resp.text[:300]}",
                           status=resp.status_code, body=resp.text[:500])
    payload = resp.json()
    refresh = payload.get("refresh_token", "")
    if refresh:
        db.set_setting("spotify_refresh_token", refresh)
    _store_user_token(payload)

    me = await _api_get("/me", user=True)
    db.set_setting("spotify_user_id", me.get("id", ""))
    db.set_setting("spotify_user_name", me.get("display_name") or me.get("id", ""))


def unlink() -> None:
    global _user_token
    _user_token = ("", 0.0)
    for key in ("spotify_refresh_token", "spotify_user_id", "spotify_user_name",
                "spotify_scopes"):
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
        _store_user_token(payload)
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

def _error_text(resp: httpx.Response) -> str:
    """Spotify's own explanation, unwrapped from its error envelope."""
    try:
        payload = resp.json()
    except ValueError:
        return resp.text[:200].strip()
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or "").strip() or resp.reason_phrase
    if isinstance(error, str):
        return payload.get("error_description") or error
    return resp.text[:200].strip()


async def _send(method: str, url: str, token: str, params: dict | None,
                json: Any) -> httpx.Response:
    """Send one paced request, re-checking the cooldown at the last moment.

    The cooldown is re-tested *inside* the lock, and that is the point of doing
    this here rather than at the top of `_request`. Callers queue up behind the
    lock, so a breaker that was closed when a caller was admitted may well be
    open by the time its turn arrives - and the caller that opened it is usually
    the one immediately ahead in the queue.

    Without this the moment a cooldown lapsed was the worst moment in the cycle:
    every waiting caller had already passed the only check there was, so they all
    went out together, and the first thing Spotify saw after letting us back in
    was the same burst that got us refused. Now the first one through takes the
    429 on everyone's behalf and the rest are turned away for free.
    """
    global _last_request

    interval = db.get_float("spotify_rate_limit_seconds", 0.5)
    async with _rate_lock:
        remaining = _breaker.remaining()
        if remaining > 0:
            raise SpotifyThrottled(
                f"Spotify is in cooldown for another {remaining:.0f}s", remaining
            )
        wait = interval - (time.monotonic() - _last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        _meter.record()
        try:
            return await _get_client().request(
                method, url, params=params, json=json,
                headers={"Authorization": f"Bearer {token}"},
            )
        finally:
            # Spacing is measured from when a call *finished*, so a slow response
            # is never followed instantly by the next one.
            _last_request = time.monotonic()


async def _request(method: str, path: str, *, user: bool = False,
                   params: dict | None = None, json: Any = None) -> Any:
    """Perform one Spotify call, honouring backpressure.

    Raises SpotifyThrottled when Spotify is refusing us - either because a
    cooldown is already open, in which case nothing is sent, or because this
    response opened one. A 429 is never retried in place: the whole point of
    Retry-After is that the next attempt would fail too.
    """
    remaining = _breaker.remaining()
    if remaining > 0:
        raise SpotifyThrottled(
            f"Spotify is in cooldown for another {remaining:.0f}s", remaining
        )

    url = path if path.startswith("http") else f"{API}{path}"
    # Only a stale-token retry remains; everything else resolves on the first try.
    for attempt in range(2):
        token = await _token(user)
        resp = await _send(method, url, token, params, json)
        if resp.status_code == 429:
            # The raw header and Spotify's own wording both go to the log: a
            # multi-hour pause is worth being able to attribute to what Spotify
            # actually sent rather than to what we computed from it.
            delay = _breaker.open(resp.headers.get("Retry-After"),
                                  resp.status_code, _error_text(resp))
            raise SpotifyThrottled(
                f"Spotify {method} {path} was rate limited; backing off {delay:.0f}s",
                delay,
            )
        if resp.status_code == 401 and attempt == 0:
            global _user_token, _app_token
            _user_token = ("", 0.0)
            _app_token = ("", 0.0)
            continue
        if resp.status_code >= 400:
            detail = _error_text(resp)
            raise SpotifyError(
                f"Spotify {method} {path} failed ({resp.status_code}): {detail}",
                status=resp.status_code, body=resp.text[:500], detail=detail,
            )
        _breaker.close()
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()
    raise SpotifyError(f"Spotify {method} {path} failed: token could not be refreshed.",
                       status=401)


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


# /search caps `limit` at 10 since February 2026, down from 50. Asking for more
# is rejected outright, so every call is clamped rather than trusted.
SEARCH_LIMIT_MAX = 10


async def search(artist: str, title: str, limit: int = 8, *,
                 interactive: bool = False) -> list[dict[str, Any]]:
    """Search tracks across plausible artist/title spellings.

    `interactive` is for a search a user typed by hand. The variant expansion
    exists to repair mangled *stream* metadata; when a person has just corrected
    the fields themselves, re-guessing their spelling adds latency for results
    that Spotify's own relevance ranking already covers. So that mode sends the
    fielded query and the unfielded fallback, and nothing else.
    """
    if not is_configured():
        return []

    limit = max(1, min(limit, SEARCH_LIMIT_MAX))
    market = db.get_setting("spotify_market", "US").strip() or "US"
    results: dict[str, dict[str, Any]] = {}
    artists = artist_variants(artist) or [artist]
    titles = title_variants(title) or [title]

    queries: list[str] = []
    spellings = 1 if interactive else 2
    for t in titles[:spellings]:
        for a in artists[:spellings]:
            queries.append(f'track:"{t}" artist:"{a}"')
    # Unfielded fallback: Spotify's relevance ranking often rescues metadata
    # that is too mangled for the strict field syntax.
    queries.append(f"{artists[0]} {titles[0]}")

    seen: set[str] = set()
    deadline = time.monotonic() + (INTERACTIVE_BUDGET if interactive else SEARCH_BUDGET)
    for query in queries:
        if query in seen:
            continue
        seen.add(query)
        if time.monotonic() >= deadline:
            break  # out of budget; score what we have rather than stall the queue
        try:
            data = await _api_get(
                "/search",
                params={"q": query, "type": "track", "limit": limit, "market": market},
            )
        except SpotifyThrottled:
            # Abandon the remaining spellings: they would each be refused too.
            # Propagating also lets the caller tell an outage from a real miss.
            raise
        except SpotifyError:
            continue
        for item in ((data.get("tracks") or {}).get("items") or []):
            parsed = _parse_track(item)
            if parsed["ext_id"] and parsed["ext_id"] not in results:
                results[parsed["ext_id"]] = parsed
        # Enough alternatives for the scorer to discriminate between a real
        # match and a karaoke cut. Stopping at the first single hit would be
        # cheaper but can lock onto a cover before the reversed-name spelling
        # ("Elfman Danny") has been tried at all. An interactive search runs
        # both of its queries regardless: the second costs one request and the
        # person doing the choosing wants alternatives, not the first answer.
        if not interactive and len(results) >= 3:
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


FORBIDDEN_HELP = (
    "Spotify refused the write (403). Check that the account is on the Spotify "
    "app's user list (Dashboard → your app → Settings → User Management), that "
    "the app has the Web API product enabled, and that the login still holds "
    "the playlist scopes. Settings → Test Spotify access says which."
)


def _friendly(exc: SpotifyError) -> SpotifyError:
    """Turn a raw Spotify failure into something a user can act on."""
    if exc.status != 403:
        return exc

    missing = missing_scopes()
    if missing:
        return SpotifyAuthRequired(
            "The saved Spotify login is missing the "
            f"{', '.join(missing)} permission(s). Disconnect and reconnect "
            "the account in Settings.", status=403,
        )

    # Spotify usually says nothing but "Forbidden", but when it does name a
    # cause that beats any guess we could offer.
    detail = (exc.detail or "").strip()
    said = "" if detail.lower() in ("", "forbidden") else f" Spotify said: {detail}"
    return SpotifyError(FORBIDDEN_HELP + said, status=403, body=exc.body, detail=detail)


async def create_playlist(name: str, description: str) -> str:
    """Create a private playlist on the linked account.

    Uses POST /me/playlists. The older POST /users/{user_id}/playlists was
    withdrawn from Development Mode apps in Spotify's February 2026 Web API
    changes (enforced for existing apps on 9 March 2026) and now answers a bare
    403 for every caller, with no hint that the endpoint itself is the problem.
    """
    body = {"name": name, "description": description[:300], "public": False}
    try:
        created = await _request("POST", "/me/playlists", user=True, json=body)
    except SpotifyError as exc:
        raise _friendly(exc) from exc
    return created.get("id", "")


async def ensure_playlist(playlist_id: str, name: str, description: str) -> str:
    """Return a usable playlist id, creating or repairing it as needed."""
    if playlist_id:
        try:
            existing = await _api_get(f"/playlists/{playlist_id}", user=True,
                                      params={"fields": "id"})
            if existing.get("id"):
                return existing["id"]
        except SpotifyError as exc:
            # Only a 404 means the playlist is genuinely gone. Treating every
            # failure as "make another one" turns a cooldown, a 403 or a
            # transient 5xx into a duplicate playlist on the account - and buries
            # the real error behind whatever the create attempt reports instead.
            if exc.status != 404:
                raise

    missing = missing_scopes()
    if missing:
        raise SpotifyAuthRequired(
            f"The saved Spotify login is missing the {', '.join(missing)} "
            "permission(s). Disconnect and reconnect the account in Settings."
        )
    return await create_playlist(name, description)


async def playlist_track_uri_list(playlist_id: str) -> list[str]:
    """Every track URI in the playlist, in playlist order, duplicates included.

    Reads /playlists/{id}/items - the February 2026 replacement for
    /playlists/{id}/tracks - where each entry carries the track under `item`.
    `track` is still populated but documented as deprecated, so it is only a
    fallback here.

    Order and repeats are preserved because the duplicate cleanup needs to see
    that a URI appears twice; the sync path only needs membership and collapses
    this to a set.
    """
    uris: list[str] = []
    url = f"/playlists/{playlist_id}/items"
    # limit is capped at 50 on the items endpoint, down from 100 on /tracks.
    params: dict | None = {"fields": "items(item(uri),track(uri)),next", "limit": 50}
    while url:
        data = await _api_get(url, user=True, params=params)
        for entry in data.get("items") or []:
            track = entry.get("item") or entry.get("track") or {}
            if track.get("uri"):
                uris.append(track["uri"])
        url = data.get("next") or ""
        params = None  # `next` already carries the query string.
    return uris


async def playlist_track_uris(playlist_id: str) -> set[str]:
    """Every track URI already in the playlist."""
    return set(await playlist_track_uri_list(playlist_id))


async def add_tracks(playlist_id: str, uris: list[str]) -> int:
    added = 0
    for i in range(0, len(uris), 100):
        chunk = uris[i:i + 100]
        try:
            await _request("POST", f"/playlists/{playlist_id}/items", user=True,
                           json={"uris": chunk})
        except SpotifyError as exc:
            raise _friendly(exc) from exc
        added += len(chunk)
    return added


async def remove_tracks(playlist_id: str, uris: list[str]) -> None:
    for i in range(0, len(uris), 100):
        chunk = [{"uri": u} for u in uris[i:i + 100]]
        # The body key was renamed from `tracks` alongside the path.
        await _request("DELETE", f"/playlists/{playlist_id}/items", user=True,
                       json={"items": chunk})


async def unfollow_playlist(playlist_id: str) -> None:
    """Drop a playlist from the user's library - Spotify has no playlist delete.

    The per-entity DELETE /playlists/{id}/followers was folded into the generic
    library endpoint in February 2026, which takes URIs as a query parameter
    rather than a body and needs user-library-modify.
    """
    await _request("DELETE", "/me/library", user=True,
                   params={"uris": f"spotify:playlist:{playlist_id}"})


# --- diagnostics -------------------------------------------------------------

async def diagnose() -> dict[str, Any]:
    """Walk the whole delivery path and report where it actually breaks.

    A 403 from Spotify carries no usable detail, so the only way to tell a
    scope problem from an app-configuration problem is to try each step in
    order. The write probe creates a private playlist and immediately unfollows
    it, which is why this runs on request rather than on every sync.
    """
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str = "") -> bool:
        checks.append({"name": name, "ok": ok, "detail": detail})
        return ok

    report: dict[str, Any] = {"ok": False, "checks": checks, "hint": ""}

    if not record("Client ID and secret saved", is_configured(),
                  "" if is_configured() else "Add them in Settings and save."):
        report["hint"] = "Spotify is not configured yet."
        return report

    if not record("Account linked", is_user_linked(),
                  "" if is_user_linked() else "Use 'Connect Spotify account'."):
        report["hint"] = "Search works without this, playlists do not."
        return report

    try:
        await _user_access_token()
        record("Login token refreshes", True)
    except SpotifyError as exc:
        record("Login token refreshes", False, str(exc))
        report["hint"] = "Reconnect the Spotify account in Settings."
        return report

    try:
        me = await current_user()
    except SpotifyError as exc:
        record("Account readable (/me)", False, str(exc))
        report["hint"] = FORBIDDEN_HELP if exc.status == 403 else str(exc)
        return report

    # `country`, `product` and `email` were dropped from the user object in
    # February 2026, so there is nothing left here worth reporting but identity.
    user_id = (me.get("id") or "").strip()
    record("Account readable (/me)", True,
           f"{me.get('display_name') or user_id} ({user_id})")
    report["user_id"] = user_id

    stored = db.get_setting("spotify_user_id", "").strip()
    if stored and stored != user_id:
        db.set_setting("spotify_user_id", user_id)
    record("Stored account id matches the login", not stored or stored == user_id,
           "" if not stored or stored == user_id
           else f"was {stored}, now corrected to {user_id}")

    granted = granted_scopes()
    missing = missing_scopes()
    report["scopes"] = sorted(granted)
    if not granted:
        record("Permissions granted", True,
               "not recorded yet — it is filled in on the next token refresh")
    elif missing:
        record("Permissions granted", False, f"missing {', '.join(missing)}")
        report["hint"] = ("Disconnect and reconnect the Spotify account to grant "
                          "the missing permissions.")
        return report
    else:
        record("Permissions granted", True, " ".join(sorted(granted)))

    try:
        await _api_get("/me/playlists", user=True, params={"limit": 1})
        record("Can read your playlists", True)
    except SpotifyError as exc:
        record("Can read your playlists", False, str(exc))

    probe_id = ""
    try:
        probe_id = await create_playlist(
            "Holiday Radio Matcher — access test",
            "Temporary playlist created to verify write access. Safe to delete.",
        )
        record("Can create a playlist", bool(probe_id))
        report["ok"] = bool(probe_id)
    except SpotifyError as exc:
        record("Can create a playlist", False, str(exc))
        report["hint"] = str(exc)
        return report

    if probe_id:
        try:
            await unfollow_playlist(probe_id)
            record("Test playlist cleaned up", True)
        except SpotifyError as exc:
            hint = ("reconnect the account to grant cleanup permission"
                    if "user-library-modify" not in granted_scopes()
                    else str(exc))
            record("Test playlist cleaned up", False,
                   f"remove 'Holiday Radio Matcher — access test' from your "
                   f"library by hand — {hint}")

    report["hint"] = "Everything Spotify delivery needs is working."
    return report
