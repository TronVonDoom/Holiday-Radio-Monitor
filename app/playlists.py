"""Playlist delivery: Spotify playlists and portable .m3u8 files.

Both targets are additive and idempotent. Spotify state is reconciled against
what is actually in the playlist rather than trusted from our own bookkeeping,
so a playlist the user edits by hand never gets duplicate entries.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from . import config, db
from .normalize import norm_key
from .providers import spotify
from .providers.spotify import SpotifyAuthRequired, SpotifyError

PLAYLIST_DESCRIPTION = (
    "Auto-built by Holiday Radio Matcher from the {name} live stream."
)

# How stale our record of a playlist's contents may get before it is checked
# against Spotify itself. Only edits made outside this app can invalidate it,
# so hours is the right order of magnitude, not minutes.
RECONCILE_AFTER = 6 * 3600


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
    # Two song rows can resolve to one recording - the stream spells it
    # differently on different days, or two stations tag it differently - and
    # each carries its own playlist entry. That is one track, so it is one line.
    seen: set[str] = set()
    for song in entries:
        artist = song["match_artist"] or song["raw_artist"]
        title = song["match_title"] or song["raw_title"]
        duration = song["match_duration"] or song["duration"] or -1
        location = song["spotify_url"] or song["spotify_uri"] or ""

        # Identity is the playable location where there is one; an identified
        # song with nowhere to point is deduplicated on what it was identified as.
        key = location or f"{norm_key(artist)}\x1f{norm_key(title)}"
        if key in seen:
            continue
        seen.add(key)

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
    missing = spotify.missing_scopes()
    if missing:
        return {"ok": False, "needs_auth": True,
                "reason": f"The Spotify login is missing the {', '.join(missing)} "
                          "permission(s). Disconnect and reconnect it in Settings."}

    entries = station_entries(int(station["id"]))
    wanted = [e for e in entries if e["spotify_uri"]]

    # Nothing undelivered means nothing to ask Spotify about. This test used to
    # be `if not wanted`, which counted entries already in the playlist - so once
    # a station had ever synced one track it never took the early exit again,
    # and every pass re-read the whole playlist to rediscover that there was
    # nothing to do. At 50 tracks per page, one station per two minutes, that is
    # thousands of pointless requests a day against an application-wide quota,
    # which is what got the whole app rate limited for hours at a time.
    unsent = [e for e in wanted if not e["spotify_synced"]]
    if not unsent:
        return {"ok": True, "added": 0, "reason": "Nothing new to add.",
                "total": len(wanted)}

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

    # Reading the playlist back is what makes delivery idempotent against edits
    # made in Spotify itself, and it is by far the most expensive call here: a
    # thousand-track playlist is twenty requests at 50 per page. Doing it on
    # every pass meant every newly matched song cost a full re-read of the
    # playlist it was joining. Our own spotify_synced record is an accurate log
    # of what we sent, so it carries the common case, and the re-read runs on a
    # schedule to catch the one thing that record cannot know about: a track
    # already present that we never sent.
    reconciled_at = station["spotify_reconciled_at"] or 0
    due = (db.now() - reconciled_at) >= RECONCILE_AFTER
    existing = await spotify.playlist_track_uris(playlist_id) if due else set()
    if due:
        db.execute("UPDATE stations SET spotify_reconciled_at = ? WHERE id = ?",
                   (db.now(), station["id"]))

    # What we have already sent for this station, by *track* rather than by song
    # row. Two rows resolving to the same recording is normal - the stream
    # spells a song differently on different days - and each one gets its own
    # playlist entry, so `unsent` can legitimately contain a track that is
    # already in the playlist. Without this the second row was sent as a new
    # track and the playlist grew a duplicate. The reconcile read above would
    # eventually notice, but it only runs every six hours, so between reads
    # every such pair became a duplicate. This costs no requests: it is our own
    # record of what we sent.
    delivered = {
        row["spotify_uri"] for row in db.query(
            "SELECT DISTINCT s.spotify_uri FROM playlist_entries e "
            "JOIN songs s ON s.id = e.song_id "
            "WHERE e.station_id = ? AND e.spotify_synced = 1 "
            "AND COALESCE(s.spotify_uri, '') != ''",
            (station["id"],),
        )
    }

    to_add: list[str] = []
    seen: set[str] = set()
    for entry in unsent:
        uri = entry["spotify_uri"]
        if uri in existing or uri in delivered or uri in seen:
            continue
        seen.add(uri)
        to_add.append(uri)

    added = await spotify.add_tracks(playlist_id, to_add) if to_add else 0

    # Marks the skipped duplicates delivered too, which is exactly right: their
    # track is in the playlist, so there is nothing left to send for them.
    db.execute(
        "UPDATE playlist_entries SET spotify_synced = 1 WHERE station_id = ? AND song_id IN "
        "(SELECT id FROM songs WHERE COALESCE(spotify_uri, '') != '')",
        (station["id"],),
    )
    if added:
        db.log_event(f"{station['name']}: added {added} track(s) to Spotify", source="sync")
    return {"ok": True, "added": added, "playlist_id": playlist_id, "total": len(wanted)}


async def dedupe_spotify(station: dict[str, Any]) -> dict[str, Any]:
    """Remove repeated tracks from a station's Spotify playlist.

    Sync stopped *creating* duplicates, but a playlist that collected them under
    the old behaviour stays duplicated until something goes and cleans it, and
    the reconcile read cannot: it only ever answers "is this URI present", which
    is true either way.

    Spotify has no "remove one copy" that is safe to rely on here - its removal
    endpoint takes URIs and clears every occurrence of each - so a duplicated
    track is removed outright and added back exactly once. That costs the track
    its position: deduplicated tracks end up at the end of the playlist. For a
    playlist this app generates and orders by when it found each song, that is a
    fair trade for it being correct, and it is why this runs when asked rather
    than on its own.

    Never touches a track that appears once, so nothing the user added by hand
    is disturbed unless they added a second copy of something.
    """
    if not spotify.is_configured() or not spotify.is_user_linked():
        return {"ok": False, "reason": "Spotify is not connected."}

    playlist_id = station["spotify_playlist_id"] or ""
    if not playlist_id:
        return {"ok": False, "reason": "This station has no Spotify playlist yet."}

    uris = await spotify.playlist_track_uri_list(playlist_id)
    counts: dict[str, int] = {}
    for uri in uris:
        counts[uri] = counts.get(uri, 0) + 1
    duplicated = [uri for uri, n in counts.items() if n > 1]
    if not duplicated:
        return {"ok": True, "removed": 0, "tracks": len(uris),
                "reason": "No duplicates to remove."}

    await spotify.remove_tracks(playlist_id, duplicated)
    await spotify.add_tracks(playlist_id, duplicated)

    removed = sum(counts[uri] - 1 for uri in duplicated)
    # Our record of the playlist's contents is now exact, so restart the clock
    # rather than leaving a re-read due.
    db.execute("UPDATE stations SET spotify_reconciled_at = ? WHERE id = ?",
               (db.now(), station["id"]))
    db.log_event(
        f"{station['name']}: removed {removed} duplicate track(s) from the "
        f"Spotify playlist ({len(duplicated)} track(s) were listed more than once).",
        source="sync",
    )
    return {"ok": True, "removed": removed, "tracks": len(uris) - removed,
            "distinct": len(duplicated)}


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


# Last problem reported per station, so a sync that keeps failing says so once
# instead of every two minutes - which would bury the 500-entry log in one
# repeated line and evict everything else worth reading.
_reported: dict[int, tuple[str, float]] = {}
RELOG_AFTER = 1800.0


def _report(station: dict[str, Any], result: dict[str, Any]) -> None:
    """Log a background sync outcome, but only when it is worth reading.

    The background loop used to discard everything sync_station returned, so a
    Spotify delivery that had been failing every two minutes for weeks - expired
    login, missing scope, 403, an open cooldown - was completely silent. The
    manual Sync button surfaced the reason in a toast, and nothing else ever did,
    which is how "not syncing" goes unnoticed for a long time.
    """
    station_id = int(station["id"])
    for target in ("spotify", "m3u"):
        outcome = result.get(target)
        if not outcome or outcome.get("ok"):
            continue
        reason = str(outcome.get("reason") or "unknown error")
        key = f"{target}:{reason}"
        last, when = _reported.get(station_id, ("", 0.0))
        if key == last and (time.monotonic() - when) < RELOG_AFTER:
            return  # same problem as last time, and said recently enough
        _reported[station_id] = (key, time.monotonic())
        label = "Spotify" if target == "spotify" else "M3U"
        db.log_event(
            f"{station['name']}: {label} delivery is failing — {reason}",
            level="warn", source="sync",
        )
        return

    # Back to healthy: say so once, so the log shows the problem ending.
    if _reported.pop(station_id, None):
        db.log_event(f"{station['name']}: playlist delivery is working again.",
                     source="sync")


async def sync_all() -> list[dict[str, Any]]:
    """Sync only stations that actually have undelivered *deliverable* work.

    A song identified through MusicBrainz alone has no Spotify URI, so its entry
    can never be marked spotify_synced - there is nothing to send. Counting those
    as outstanding work made every station permanently pending, so the loop woke
    all of them every two minutes forever. The URI test is what makes "pending"
    mean deliverable rather than merely imperfect.
    """
    pending = db.query(
        "SELECT DISTINCT st.* FROM stations st "
        "JOIN playlist_entries e ON e.station_id = st.id "
        "JOIN songs s ON s.id = e.song_id "
        "WHERE s.status IN ('matched', 'confirmed') "
        "AND ((e.spotify_synced = 0 AND COALESCE(s.spotify_uri, '') != '') "
        "     OR e.m3u_synced = 0)"
    )
    results = []
    for row in pending:
        station = dict(row)
        result = await sync_station(station)
        _report(station, result)
        results.append(result)
    return results


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
