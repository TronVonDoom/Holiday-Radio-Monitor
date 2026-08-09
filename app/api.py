"""REST API consumed by the web UI."""

from __future__ import annotations

import json
import secrets
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import config, db, matcher, monitor, playlists, sources
from .normalize import norm_key, titlecase_display
from .providers import musicbrainz, spotify

router = APIRouter(prefix="/api")

STATUSES = ("pending", "matched", "review", "unmatched", "confirmed", "rejected", "nonsong")


# --- models ------------------------------------------------------------------

class StationIn(BaseModel):
    name: str
    slug: str = ""
    holiday: str = "halloween"
    azuracast_base: str = ""
    azuracast_shortcode: str = ""
    icy_url: str = ""
    enabled: bool = True


class StationPatch(BaseModel):
    name: str | None = None
    holiday: str | None = None
    enabled: bool | None = None
    azuracast_base: str | None = None
    azuracast_shortcode: str | None = None
    icy_url: str | None = None
    spotify_playlist_id: str | None = None
    m3u_filename: str | None = None


class DiscoverIn(BaseModel):
    base_url: str


class ProbeIn(BaseModel):
    azuracast_base: str = ""
    azuracast_shortcode: str = ""
    icy_url: str = ""


class ConfirmIn(BaseModel):
    candidate_id: int | None = None
    spotify_id: str = ""
    artist: str = ""
    title: str = ""
    album: str = ""
    duration: int | None = None
    mbid: str = ""
    isrc: str = ""
    remember: bool = True


class SearchIn(BaseModel):
    artist: str = ""
    title: str = ""
    limit: int = Field(default=8, ge=1, le=20)


class SettingsIn(BaseModel):
    values: dict[str, str]


class ExchangeIn(BaseModel):
    """Either the full pasted callback URL, or just the bare code."""
    value: str


# --- helpers -----------------------------------------------------------------

def _slugify(text: str) -> str:
    base = norm_key(text).replace(" ", "-")
    return base or secrets.token_hex(4)


def _station_or_404(station_id: int) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM stations WHERE id = ?", (station_id,))
    if row is None:
        raise HTTPException(404, "Station not found")
    return dict(row)


def _song_or_404(song_id: int) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM songs WHERE id = ?", (song_id,))
    if row is None:
        raise HTTPException(404, "Song not found")
    return dict(row)


def _write_alias(song: dict[str, Any], payload: dict[str, Any], kind: str = "match") -> None:
    """Teach the matcher, so this metadata resolves instantly next time."""
    db.execute(
        "INSERT INTO aliases (key_artist, key_title, match_artist, match_title, match_album, "
        "duration, mbid, isrc, spotify_id, spotify_uri, spotify_url, art_url, kind, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(key_artist, key_title) DO UPDATE SET "
        "match_artist=excluded.match_artist, match_title=excluded.match_title, "
        "match_album=excluded.match_album, duration=excluded.duration, mbid=excluded.mbid, "
        "isrc=excluded.isrc, spotify_id=excluded.spotify_id, spotify_uri=excluded.spotify_uri, "
        "spotify_url=excluded.spotify_url, art_url=excluded.art_url, kind=excluded.kind",
        (
            norm_key(song["raw_artist"]), norm_key(song["raw_title"]),
            payload.get("artist", ""), payload.get("title", ""), payload.get("album", ""),
            payload.get("duration"), payload.get("mbid") or None, payload.get("isrc") or None,
            payload.get("spotify_id") or None, payload.get("uri") or None,
            payload.get("url") or None, payload.get("art_url") or None,
            kind, db.now(),
        ),
    )


# --- dashboard ---------------------------------------------------------------

@router.get("/stats")
def stats() -> dict[str, Any]:
    counts = {row["status"]: row["n"] for row in
              db.query("SELECT status, COUNT(*) AS n FROM songs GROUP BY status")}
    total_songs = sum(counts.values())
    resolved = counts.get("matched", 0) + counts.get("confirmed", 0)
    reviewable = counts.get("review", 0) + counts.get("unmatched", 0)
    considered = resolved + reviewable

    return {
        "counts": {s: counts.get(s, 0) for s in STATUSES},
        "total_songs": total_songs,
        "total_plays": db.query_one("SELECT COUNT(*) AS n FROM plays")["n"],
        "playlist_entries": db.query_one("SELECT COUNT(*) AS n FROM playlist_entries")["n"],
        "aliases": db.query_one("SELECT COUNT(*) AS n FROM aliases")["n"],
        "stations": db.query_one("SELECT COUNT(*) AS n FROM stations WHERE enabled = 1")["n"],
        "match_rate": round(resolved / considered, 4) if considered else 0.0,
        "queue": counts.get("pending", 0),
        "worker": monitor.state(),
        "spotify": {
            "configured": spotify.is_configured(),
            "linked": spotify.is_user_linked(),
            "user": db.get_setting("spotify_user_name", ""),
        },
        "playlist_dir": playlists.playlist_dir_status(),
        "version": config.APP_VERSION,
    }


@router.get("/nowplaying")
def nowplaying() -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT st.id AS station_id, st.name AS station, st.holiday, st.enabled, "
        "st.last_error, st.last_polled_at, s.id AS song_id, s.raw_artist, s.raw_title, "
        "s.status, s.confidence, s.art_url, s.match_artist, s.match_title, s.spotify_url, "
        "p.played_at "
        "FROM stations st "
        "LEFT JOIN plays p ON p.id = ("
        "  SELECT id FROM plays WHERE station_id = st.id ORDER BY played_at DESC, id DESC LIMIT 1) "
        "LEFT JOIN songs s ON s.id = p.song_id "
        "ORDER BY st.name"
    )
    return [dict(r) for r in rows]


@router.get("/recent")
def recent(limit: int = Query(default=40, ge=1, le=200)) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT p.played_at, st.name AS station, st.holiday, s.id AS song_id, "
        "s.raw_artist, s.raw_title, s.status, s.confidence, s.match_artist, "
        "s.match_title, s.art_url, s.spotify_url "
        "FROM plays p JOIN stations st ON st.id = p.station_id "
        "JOIN songs s ON s.id = p.song_id "
        "ORDER BY p.played_at DESC, p.id DESC LIMIT ?", (limit,)
    )
    return [dict(r) for r in rows]


@router.get("/events")
def events(limit: int = Query(default=60, ge=1, le=300)) -> list[dict[str, Any]]:
    return [dict(r) for r in db.query(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
    )]


# --- stations ----------------------------------------------------------------

@router.get("/stations")
def list_stations() -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT st.*, "
        "(SELECT COUNT(*) FROM plays WHERE station_id = st.id) AS plays, "
        "(SELECT COUNT(*) FROM playlist_entries WHERE station_id = st.id) AS playlist_count "
        "FROM stations st ORDER BY st.name"
    )
    return [dict(r) for r in rows]


@router.post("/stations")
def create_station(payload: StationIn) -> dict[str, Any]:
    if not (payload.azuracast_shortcode or payload.icy_url):
        raise HTTPException(400, "Provide an AzuraCast shortcode or an ICY stream URL.")
    slug = payload.slug or _slugify(payload.name)
    if db.query_one("SELECT id FROM stations WHERE slug = ?", (slug,)):
        raise HTTPException(409, f"A station with slug {slug!r} already exists.")

    cur = db.execute(
        "INSERT INTO stations (slug, name, holiday, enabled, azuracast_base, "
        "azuracast_shortcode, icy_url, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (slug, payload.name, payload.holiday, int(payload.enabled),
         payload.azuracast_base.rstrip("/"), payload.azuracast_shortcode,
         payload.icy_url, db.now()),
    )
    db.log_event(f"Added station {payload.name}", source="stations")
    return _station_or_404(int(cur.lastrowid))


@router.patch("/stations/{station_id}")
def update_station(station_id: int, payload: StationPatch) -> dict[str, Any]:
    _station_or_404(station_id)
    fields = payload.model_dump(exclude_none=True)
    if not fields:
        return _station_or_404(station_id)
    if "enabled" in fields:
        fields["enabled"] = int(bool(fields["enabled"]))
    assignments = ", ".join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE stations SET {assignments} WHERE id = ?",
               (*fields.values(), station_id))
    return _station_or_404(station_id)


@router.delete("/stations/{station_id}")
def delete_station(station_id: int) -> dict[str, str]:
    station = _station_or_404(station_id)
    db.execute("DELETE FROM stations WHERE id = ?", (station_id,))
    db.log_event(f"Removed station {station['name']}", source="stations")
    return {"status": "deleted"}


@router.post("/stations/discover")
async def discover(payload: DiscoverIn) -> dict[str, Any]:
    try:
        found = await sources.discover_azuracast(payload.base_url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Could not read station list: {exc}") from exc
    known = {row["azuracast_shortcode"] for row in db.query(
        "SELECT azuracast_shortcode FROM stations WHERE azuracast_shortcode IS NOT NULL")}
    for station in found:
        station["already_added"] = station["shortcode"] in known
    return {"stations": found, "count": len(found)}


@router.post("/stations/probe")
async def probe(payload: ProbeIn) -> dict[str, Any]:
    return await sources.probe(
        payload.azuracast_base, payload.azuracast_shortcode, payload.icy_url
    )


@router.post("/stations/{station_id}/poll")
async def poll_station_now(station_id: int) -> dict[str, Any]:
    station = _station_or_404(station_id)
    await monitor.poll_station(station)
    return _station_or_404(station_id)


# --- songs / review ----------------------------------------------------------

@router.get("/songs")
def list_songs(
    status: str = Query(default=""),
    q: str = Query(default=""),
    station_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=300),
    offset: int = Query(default=0, ge=0),
    sort: str = Query(default="recent"),
) -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []

    if status:
        wanted = [s.strip() for s in status.split(",") if s.strip() in STATUSES]
        if wanted:
            where.append(f"s.status IN ({','.join('?' * len(wanted))})")
            params.extend(wanted)
    if q:
        where.append("(s.raw_artist LIKE ? OR s.raw_title LIKE ? OR "
                     "s.match_artist LIKE ? OR s.match_title LIKE ?)")
        params.extend([f"%{q}%"] * 4)
    if station_id:
        where.append("EXISTS (SELECT 1 FROM plays WHERE song_id = s.id AND station_id = ?)")
        params.append(station_id)

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    order = {
        "recent": "s.last_seen_at DESC",
        "plays": "s.play_count DESC, s.last_seen_at DESC",
        "confidence": "s.confidence ASC, s.last_seen_at DESC",
        "artist": "s.raw_artist COLLATE NOCASE ASC",
    }.get(sort, "s.last_seen_at DESC")

    total = db.query_one(f"SELECT COUNT(*) AS n FROM songs s {clause}", params)["n"]
    rows = db.query(
        f"SELECT s.* FROM songs s {clause} ORDER BY {order} LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    return {"total": total, "items": [dict(r) for r in rows],
            "limit": limit, "offset": offset}


@router.get("/songs/{song_id}")
def get_song(song_id: int) -> dict[str, Any]:
    song = _song_or_404(song_id)
    candidates = []
    for row in db.query(
        "SELECT * FROM candidates WHERE song_id = ? ORDER BY score DESC", (song_id,)
    ):
        cand = dict(row)
        try:
            cand["score_detail"] = json.loads(cand.get("score_detail") or "{}")
        except json.JSONDecodeError:
            cand["score_detail"] = {}
        candidates.append(cand)

    stations = [dict(r) for r in db.query(
        "SELECT DISTINCT st.id, st.name, st.holiday FROM plays p "
        "JOIN stations st ON st.id = p.station_id WHERE p.song_id = ?", (song_id,)
    )]
    return {"song": song, "candidates": candidates, "stations": stations}


@router.post("/songs/{song_id}/confirm")
async def confirm_song(song_id: int, payload: ConfirmIn) -> dict[str, Any]:
    song = _song_or_404(song_id)

    if payload.candidate_id:
        row = db.query_one("SELECT * FROM candidates WHERE id = ? AND song_id = ?",
                           (payload.candidate_id, song_id))
        if row is None:
            raise HTTPException(404, "Candidate not found")
        cand = dict(row)
        chosen: dict[str, Any] = {
            "source": cand["source"],
            "ext_id": cand["ext_id"],
            "artist": cand["artist"],
            "title": cand["title"],
            "album": cand["album"],
            "duration": cand["duration"],
            "url": cand["url"],
            "uri": cand["uri"],
            "art_url": cand["art_url"],
            "isrc": cand["isrc"],
            "mbid": cand["ext_id"] if cand["source"] == "musicbrainz" else "",
            "spotify_id": cand["ext_id"] if cand["source"] == "spotify" else "",
        }
    elif payload.spotify_id:
        track = await spotify.get_track(payload.spotify_id)
        if track is None:
            raise HTTPException(400, "That Spotify track could not be loaded.")
        chosen = {**track, "spotify_id": track["ext_id"], "mbid": payload.mbid}
    elif payload.artist or payload.title:
        chosen = {
            "source": "manual", "ext_id": "", "artist": payload.artist,
            "title": payload.title, "album": payload.album, "duration": payload.duration,
            "url": "", "uri": "", "art_url": "", "isrc": payload.isrc,
            "mbid": payload.mbid, "spotify_id": "",
        }
    else:
        raise HTTPException(400, "Provide a candidate, a Spotify id, or manual details.")

    # A confirmation should end up playable wherever possible.
    if not chosen.get("uri"):
        chosen = await matcher.enrich_with_spotify(
            chosen, song["raw_artist"], song["raw_title"], song["duration"]
        )

    chosen["artist"] = titlecase_display(chosen.get("artist", ""))
    chosen["title"] = titlecase_display(chosen.get("title", ""))

    monitor.apply_match(song_id, chosen, "confirmed", 1.0, "user")
    monitor.enqueue_for_playlists(song_id)
    if payload.remember:
        _write_alias(song, chosen, kind="match")

    db.log_event(
        f"Confirmed {song['raw_artist']} - {song['raw_title']} -> "
        f"{chosen['artist']} - {chosen['title']}", source="review",
    )
    return get_song(song_id)


@router.post("/songs/{song_id}/reject")
def reject_song(song_id: int, remember: bool = Query(default=False)) -> dict[str, Any]:
    song = _song_or_404(song_id)
    db.execute(
        "UPDATE songs SET status = 'rejected', resolved_at = ?, confidence = 0 WHERE id = ?",
        (db.now(), song_id),
    )
    db.execute("DELETE FROM playlist_entries WHERE song_id = ?", (song_id,))
    if remember:
        _write_alias(song, {}, kind="nonsong")
    return _song_or_404(song_id)


@router.post("/songs/{song_id}/nonsong")
def mark_nonsong(song_id: int, remember: bool = Query(default=True)) -> dict[str, Any]:
    song = _song_or_404(song_id)
    db.execute(
        "UPDATE songs SET status = 'nonsong', nonsong_reason = 'Marked by user', "
        "resolved_at = ?, confidence = 0 WHERE id = ?", (db.now(), song_id),
    )
    db.execute("DELETE FROM playlist_entries WHERE song_id = ?", (song_id,))
    if remember:
        _write_alias(song, {}, kind="nonsong")
    return _song_or_404(song_id)


@router.post("/songs/{song_id}/rematch")
async def rematch_song(song_id: int) -> dict[str, Any]:
    song = _song_or_404(song_id)
    # Drop any learned alias so the search actually runs again.
    db.execute("DELETE FROM aliases WHERE key_artist = ? AND key_title = ?",
               (norm_key(song["raw_artist"]), norm_key(song["raw_title"])))
    db.execute("UPDATE songs SET status = 'pending', attempts = 0 WHERE id = ?", (song_id,))
    await monitor.match_song(_song_or_404(song_id))
    return get_song(song_id)


@router.post("/songs/{song_id}/search")
async def manual_search(song_id: int, payload: SearchIn) -> dict[str, Any]:
    """Free-text search used by the review UI when the automatic candidates miss."""
    song = _song_or_404(song_id)
    artist = payload.artist or song["raw_artist"]
    title = payload.title or song["raw_title"]

    results: list[dict[str, Any]] = []
    if spotify.is_configured():
        try:
            results.extend(await spotify.search(artist, title, payload.limit))
        except Exception as exc:  # noqa: BLE001
            db.log_event(f"Manual Spotify search failed: {exc}", level="warn", source="review")
    if db.get_bool("use_musicbrainz", True):
        try:
            results.extend(await musicbrainz.search(artist, title, payload.limit))
        except Exception as exc:  # noqa: BLE001
            db.log_event(f"Manual MusicBrainz search failed: {exc}", level="warn", source="review")

    scored = []
    for cand in results:
        score, detail = matcher.score_candidate(artist, title, song["duration"], cand)
        scored.append({**cand, "score": score, "score_detail": detail})
    scored.sort(key=lambda c: c["score"], reverse=True)

    monitor._store_candidates(song_id, scored[:12])
    return get_song(song_id)


@router.post("/match/run")
async def run_matcher(limit: int = Query(default=25, ge=1, le=200)) -> dict[str, Any]:
    return {"processed": await monitor.match_pending(limit)}


# --- playlists ---------------------------------------------------------------

@router.get("/playlists")
def playlist_overview() -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT st.id, st.name, st.holiday, st.spotify_playlist_id, st.m3u_filename, "
        "COUNT(e.id) AS entries, "
        "SUM(CASE WHEN e.spotify_synced = 1 THEN 1 ELSE 0 END) AS spotify_synced "
        "FROM stations st LEFT JOIN playlist_entries e ON e.station_id = st.id "
        "GROUP BY st.id ORDER BY st.name"
    )
    return [dict(r) for r in rows]


@router.get("/playlists/{station_id}/tracks")
def playlist_tracks(station_id: int) -> list[dict[str, Any]]:
    _station_or_404(station_id)
    return playlists.station_entries(station_id)


@router.post("/playlists/sync")
async def sync_now(station_id: int | None = Query(default=None)) -> dict[str, Any]:
    if station_id:
        return {"results": [await playlists.sync_station(_station_or_404(station_id))]}
    stations = [dict(r) for r in db.query("SELECT * FROM stations")]
    return {"results": [await playlists.sync_station(s) for s in stations]}


# --- settings ----------------------------------------------------------------

SECRET_KEYS = {"spotify_client_secret", "spotify_refresh_token"}


@router.get("/settings")
def get_settings() -> dict[str, Any]:
    values = db.all_settings()
    # Never echo secrets back to the browser; report presence instead.
    redacted = {k: ("********" if (k in SECRET_KEYS and v) else v) for k, v in values.items()}
    redacted.pop("spotify_refresh_token", None)
    return {
        "values": redacted,
        "spotify": {
            "configured": spotify.is_configured(),
            "linked": spotify.is_user_linked(),
            "user": db.get_setting("spotify_user_name", ""),
            "scopes": spotify.SCOPES,
            "missing_scopes": spotify.missing_scopes(),
        },
        "playlist_dir": playlists.playlist_dir_status(),
        "config_dir": str(config.CONFIG_DIR),
    }


@router.put("/settings")
def put_settings(payload: SettingsIn) -> dict[str, Any]:
    for key, value in payload.values.items():
        # The UI sends the redaction placeholder back untouched when the user
        # did not retype a secret.
        if key in SECRET_KEYS and value == "********":
            continue
        db.set_setting(key, value)
    return get_settings()


# --- Spotify OAuth -----------------------------------------------------------

def _redirect_uri(request: Request) -> str:
    """Where Spotify sends the user back after they approve access.

    Spotify rejects anything that is not HTTPS or a literal loopback address, so
    the auto-derived LAN URL (http://192.168.x.x:8686/...) is refused with
    "redirect_uri: Insecure". The Settings field is checked first so the fix is
    always reachable from the UI, even when the env var was set to a bad value.
    """
    override = db.get_setting("spotify_redirect_uri", "").strip()
    if override:
        return override
    if config.SPOTIFY_REDIRECT_URI:
        return config.SPOTIFY_REDIRECT_URI
    return str(request.url_for("spotify_callback"))


def check_redirect_uri(uri: str) -> str:
    """Return a warning if Spotify will reject this redirect URI, else ''."""
    parsed = urlparse(uri)
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https":
        return ""
    if host in ("127.0.0.1", "::1") and parsed.scheme == "http":
        return ""
    if host == "localhost":
        return ("Spotify does not accept 'localhost'. Use 127.0.0.1 instead, "
                "or an https:// address.")
    return ("Spotify only accepts https:// addresses or the literal loopback "
            "address 127.0.0.1 — a plain http:// LAN address is rejected with "
            "\"redirect_uri: Insecure\".")


@router.get("/spotify/login")
def spotify_login(request: Request) -> Any:
    if not spotify.is_configured():
        raise HTTPException(400, "Set the Spotify client ID and secret first.")
    redirect_uri = _redirect_uri(request)
    state = secrets.token_urlsafe(16)
    db.set_setting("spotify_oauth_state", state)
    # The token exchange must present the identical redirect URI, so remember
    # exactly which one this authorization used.
    db.set_setting("spotify_oauth_redirect", redirect_uri)
    return {
        "url": spotify.authorize_url(redirect_uri, state),
        "redirect_uri": redirect_uri,
        "warning": check_redirect_uri(redirect_uri),
    }


async def _finish_link(code: str, state: str, redirect_uri: str) -> None:
    expected = db.get_setting("spotify_oauth_state", "")
    if not code:
        raise HTTPException(400, "No authorization code was found.")
    if expected and state and state != expected:
        raise HTTPException(400, "Authorization state did not match. Start the link again.")
    await spotify.exchange_code(code, redirect_uri)
    db.set_setting("spotify_oauth_state", "")
    db.log_event("Spotify account connected", source="spotify")


@router.get("/spotify/callback", name="spotify_callback")
async def spotify_callback(request: Request, code: str = "", state: str = "",
                           error: str = "") -> Any:
    """Hit directly when the browser can actually reach this app's callback."""
    if error:
        return RedirectResponse(f"/?spotify=error&reason={error}")
    redirect_uri = db.get_setting("spotify_oauth_redirect", "") or _redirect_uri(request)
    try:
        await _finish_link(code, state, redirect_uri)
    except HTTPException as exc:
        return RedirectResponse(f"/?spotify=error&reason={exc.detail}")
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(f"/?spotify=error&reason={str(exc)[:120]}")
    return RedirectResponse("/?spotify=linked")


@router.post("/spotify/exchange")
async def spotify_exchange(payload: ExchangeIn) -> dict[str, Any]:
    """Complete the link from a pasted callback URL.

    Spotify requires a loopback or https redirect, which usually cannot reach a
    container on another machine. Letting that redirect fail and pasting the
    resulting address back here avoids needing a tunnel or a reverse proxy.
    """
    raw = payload.value.strip()
    if not raw:
        raise HTTPException(400, "Paste the URL you were redirected to.")

    code, state = raw, ""
    if "://" in raw or raw.startswith("?") or "code=" in raw:
        query = urlparse(raw).query or raw.lstrip("?")
        params = parse_qs(query)
        if params.get("error"):
            raise HTTPException(400, f"Spotify returned an error: {params['error'][0]}")
        if not params.get("code"):
            raise HTTPException(400, "That URL has no 'code' parameter in it.")
        code = params["code"][0]
        state = (params.get("state") or [""])[0]

    redirect_uri = db.get_setting("spotify_oauth_redirect", "").strip()
    if not redirect_uri:
        raise HTTPException(400, "Start the link with 'Connect Spotify account' first.")

    try:
        await _finish_link(code, state, redirect_uri)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - surface Spotify's own wording
        raise HTTPException(400, f"Spotify rejected the link: {str(exc)[:300]}") from exc
    return {"status": "linked", "user": db.get_setting("spotify_user_name", "")}


@router.post("/spotify/unlink")
def spotify_unlink() -> dict[str, str]:
    spotify.unlink()
    return {"status": "unlinked"}


@router.post("/spotify/diagnose")
async def spotify_diagnose() -> dict[str, Any]:
    """Test every step playlist delivery depends on, and say which one fails.

    A POST because the write check really does create a playlist on the account
    (and then unfollows it) - that round trip is the only honest answer to
    "can this app write playlists?".
    """
    try:
        report = await spotify.diagnose()
    except Exception as exc:  # noqa: BLE001 - the report is the error channel
        raise HTTPException(400, f"Could not check Spotify access: {exc}") from exc
    if not report["ok"]:
        db.log_event(f"Spotify access check failed: {report['hint']}",
                     level="warn", source="spotify")
    return report


@router.get("/spotify/redirect-uri")
def spotify_redirect_uri(request: Request) -> dict[str, str]:
    uri = _redirect_uri(request)
    return {"redirect_uri": uri, "warning": check_redirect_uri(uri),
            "suggested_loopback": _suggested_loopback(request)}


def _suggested_loopback(request: Request) -> str:
    """A ready-made loopback URI on the port this app is actually served on."""
    port = request.url.port or config.PORT
    return f"http://127.0.0.1:{port}/api/spotify/callback"


# --- aliases -----------------------------------------------------------------

@router.get("/aliases")
def list_aliases(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
    return [dict(r) for r in db.query(
        "SELECT * FROM aliases ORDER BY hits DESC, id DESC LIMIT ?", (limit,)
    )]


@router.delete("/aliases/{alias_id}")
def delete_alias(alias_id: int) -> dict[str, str]:
    db.execute("DELETE FROM aliases WHERE id = ?", (alias_id,))
    return {"status": "deleted"}


@router.get("/export/{station_id}.json")
def export_station(station_id: int) -> JSONResponse:
    station = _station_or_404(station_id)
    return JSONResponse({
        "station": station["name"],
        "tracks": [
            {
                "artist": e["match_artist"] or e["raw_artist"],
                "title": e["match_title"] or e["raw_title"],
                "album": e["match_album"],
                "duration": e["match_duration"] or e["duration"],
                "isrc": e["isrc"],
                "mbid": e["mbid"],
                "spotify_url": e["spotify_url"],
                "confidence": e["confidence"],
            }
            for e in playlists.station_entries(station_id)
        ],
    })
