"""Playlist delivery: Spotify playlists and portable .m3u8 files.

Both targets are additive and idempotent. Spotify state is reconciled against
what is actually in the playlist rather than trusted from our own bookkeeping,
so a playlist the user edits by hand never gets duplicate entries.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from . import config, db
from .providers import spotify
from .providers.spotify import SpotifyAuthRequired, SpotifyError

PLAYLIST_DESCRIPTION = (
    "Auto-built by Holiday Radio Matcher from the {name} live stream."
)


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w\s.-]", "", name).strip().replace(" ", "-")
    return re.sub(r"-+", "-", cleaned).lower() or "playlist"


def station_entries(station_id: int) -> list[dict[str, Any]]:
    """Every deliverable song for a station, oldest addition first."""
    rows = db.query(
        "SELECT s.*, e.added_at, e.spotify_synced, e.m3u_synced "
        "FROM playlist_entries e JOIN songs s ON s.id = e.song_id "
        "WHERE e.station_id = ? AND s.status IN ('matched', 'confirmed') "
        "ORDER BY e.added_at ASC",
        (station_id,),
    )
    return [dict(r) for r in rows]


# --- M3U ---------------------------------------------------------------------

def write_m3u(station: dict[str, Any]) -> tuple[int, str]:
    """Write one extended M3U for a station. Returns (entry_count, path)."""
    entries = station_entries(int(station["id"]))
    filename = station["m3u_filename"] or f"{_safe_filename(station['name'])}.m3u8"
    target = config.PLAYLIST_DIR / filename
    target.parent.mkdir(parents=True, exist_ok=True)

    lines = ["#EXTM3U", f"#PLAYLIST:{station['name']}"]
    written = 0
    for song in entries:
        artist = song["match_artist"] or song["raw_artist"]
        title = song["match_title"] or song["raw_title"]
        duration = song["match_duration"] or song["duration"] or -1
        location = song["spotify_url"] or song["spotify_uri"] or ""

        lines.append(f"#EXTINF:{duration},{artist} - {title}")
        if song["match_album"]:
            lines.append(f"#EXTALB:{song['match_album']}")
        if song["isrc"]:
            lines.append(f"#EXTISRC:{song['isrc']}")
        if location:
            lines.append(location)
        else:
            # No playable location yet (identified but not on Spotify). Keep it
            # recorded rather than silently dropping a correct match.
            lines.append(f"#EXT-X-UNRESOLVED:{artist} - {title}")
        written += 1

    # Atomic replace so a reader never sees a half-written playlist.
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, target)

    db.execute(
        "UPDATE playlist_entries SET m3u_synced = 1 WHERE station_id = ?",
        (station["id"],),
    )
    return written, str(target)


# --- Spotify -----------------------------------------------------------------

async def sync_spotify(station: dict[str, Any]) -> dict[str, Any]:
    if not spotify.is_configured():
        return {"ok": False, "reason": "Spotify is not configured."}
    if not spotify.is_user_linked():
        return {"ok": False, "reason": "Spotify account is not connected."}

    entries = station_entries(int(station["id"]))
    wanted = [e for e in entries if e["spotify_uri"]]
    if not wanted:
        return {"ok": True, "added": 0, "reason": "Nothing to add."}

    playlist_id = await spotify.ensure_playlist(
        station["spotify_playlist_id"] or "",
        f"{station['name']} — Matched",
        PLAYLIST_DESCRIPTION.format(name=station["name"]),
    )
    if not playlist_id:
        return {"ok": False, "reason": "Could not create the Spotify playlist."}

    if playlist_id != station["spotify_playlist_id"]:
        db.execute("UPDATE stations SET spotify_playlist_id = ? WHERE id = ?",
                   (playlist_id, station["id"]))

    existing = await spotify.playlist_track_uris(playlist_id)

    to_add: list[str] = []
    seen: set[str] = set()
    for entry in wanted:
        uri = entry["spotify_uri"]
        if uri in existing or uri in seen:
            continue
        seen.add(uri)
        to_add.append(uri)

    added = await spotify.add_tracks(playlist_id, to_add) if to_add else 0

    db.execute(
        "UPDATE playlist_entries SET spotify_synced = 1 WHERE station_id = ? AND song_id IN "
        "(SELECT id FROM songs WHERE spotify_uri IS NOT NULL)",
        (station["id"],),
    )
    if added:
        db.log_event(f"{station['name']}: added {added} track(s) to Spotify", source="sync")
    return {"ok": True, "added": added, "playlist_id": playlist_id, "total": len(wanted)}


# --- orchestration -----------------------------------------------------------

async def sync_station(station: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"station": station["name"]}

    if db.get_bool("m3u_enabled", True):
        try:
            count, path = write_m3u(station)
            result["m3u"] = {"ok": True, "entries": count, "path": path}
        except Exception as exc:  # noqa: BLE001
            result["m3u"] = {"ok": False, "reason": str(exc)}

    if db.get_bool("spotify_sync_enabled", True):
        try:
            result["spotify"] = await sync_spotify(station)
        except SpotifyAuthRequired as exc:
            result["spotify"] = {"ok": False, "reason": str(exc), "needs_auth": True}
        except SpotifyError as exc:
            result["spotify"] = {"ok": False, "reason": str(exc)}

    return result


async def sync_all() -> list[dict[str, Any]]:
    """Sync only stations that actually have undelivered work."""
    pending = db.query(
        "SELECT DISTINCT st.* FROM stations st "
        "JOIN playlist_entries e ON e.station_id = st.id "
        "JOIN songs s ON s.id = e.song_id "
        "WHERE s.status IN ('matched', 'confirmed') "
        "AND (e.spotify_synced = 0 OR e.m3u_synced = 0)"
    )
    return [await sync_station(dict(row)) for row in pending]


def playlist_dir_status() -> dict[str, Any]:
    path: Path = config.PLAYLIST_DIR
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".hrm-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        writable = True
        error = ""
    except Exception as exc:  # noqa: BLE001
        writable = False
        error = str(exc)
    return {"path": str(path), "writable": writable, "error": error}
