"""Background loops: poll stations, resolve songs, deliver playlists.

Three cooperating tasks run for the life of the process:

  poll_loop   - reads each enabled station and records what it played
  match_loop  - drains the pending queue through the matching engine
  sync_loop   - pushes confirmed matches to Spotify and the .m3u8 files

They are deliberately separate: metadata polling must stay punctual even when
MusicBrainz is throttling us, and playlist delivery must not block either.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from . import db, matcher, playlists, sources
from .normalize import fingerprint, is_junk_album, norm_key, titlecase_display
from .providers import spotify

# How many songs one matcher pass will resolve. Kept small so the loop stays
# responsive and MusicBrainz's 1 req/sec budget is spent steadily.
MATCH_BATCH = 8

# A song whose providers were unreachable is retried with escalating backoff
# rather than being written off as unmatched.
MAX_RETRY_ATTEMPTS = 6
RETRY_BACKOFF_SECONDS = 90

# Hard ceiling on one song. The matcher is deliberately serial to stay inside
# MusicBrainz's rate budget, which means a single slow resolve blocks the whole
# queue - so no song is ever allowed to hold the loop indefinitely. A healthy
# resolve is a few seconds; this only fires when a provider is misbehaving.
MATCH_SONG_TIMEOUT = 90

_tasks: list[asyncio.Task] = []
_state: dict[str, Any] = {"polling": False, "matching": False, "last_poll": 0}


# --- ingestion ---------------------------------------------------------------

def _upsert_song(obs: sources.Observation) -> int:
    """Record the metadata for an observation, without counting it as a play.

    AzuraCast returns a rolling history window, so the same play is observed on
    every poll. Only `ingest` knows whether an observation became a new row in
    `plays`, so only `ingest` may touch `play_count` - counting here inflated it
    by roughly the number of times a play stayed inside the history window.
    """
    fp = fingerprint(obs.artist, obs.title)
    album = "" if is_junk_album(obs.album) else obs.album
    now = db.now()

    row = db.query_one("SELECT id, duration, art_url FROM songs WHERE fingerprint = ?", (fp,))
    if row is not None:
        db.execute(
            "UPDATE songs SET last_seen_at = ?, "
            "duration = COALESCE(?, duration), art_url = COALESCE(NULLIF(?, ''), art_url) "
            "WHERE id = ?",
            (now, obs.duration, obs.art_url, row["id"]),
        )
        return int(row["id"])

    cur = db.execute(
        "INSERT INTO songs (fingerprint, raw_artist, raw_title, raw_album, norm_artist, "
        "norm_title, duration, art_url, status, play_count, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)",
        (fp, obs.artist, obs.title, album, norm_key(obs.artist), norm_key(obs.title),
         obs.duration, obs.art_url, now, now),
    )
    return int(cur.lastrowid)


def ingest(station_id: int, observations: list[sources.Observation]) -> int:
    """Record observations, returning how many genuinely new plays were stored."""
    new_plays = 0
    last = db.query_one(
        "SELECT song_id FROM plays WHERE station_id = ? ORDER BY played_at DESC, id DESC LIMIT 1",
        (station_id,),
    )
    last_song_id = int(last["song_id"]) if last else None

    # Oldest first so the history backfills in the order it actually aired.
    for obs in sorted(observations, key=lambda o: o.played_at):
        if not (obs.artist or obs.title):
            continue
        song_id = _upsert_song(obs)

        played_at = obs.played_at
        if played_at <= 0:
            # Sources without timestamps (ICY) can only tell us "this is on now",
            # so we only record a play when the track actually changed.
            if song_id == last_song_id:
                continue
            played_at = db.now()

        cur = db.execute(
            "INSERT OR IGNORE INTO plays (station_id, song_id, played_at) VALUES (?, ?, ?)",
            (station_id, song_id, played_at),
        )
        if cur.rowcount:
            new_plays += 1
            last_song_id = song_id
            # play_count is a cache of COUNT(*) over `plays`, so it is only ever
            # bumped alongside a row that actually landed.
            db.execute(
                "UPDATE songs SET play_count = play_count + 1 WHERE id = ?", (song_id,)
            )

    return new_plays


def _record_poll(station: dict[str, Any], observations: list[sources.Observation],
                 source: str) -> None:
    new_plays = ingest(int(station["id"]), observations)
    db.execute(
        "UPDATE stations SET last_polled_at = ?, last_error = NULL WHERE id = ?",
        (db.now(), station["id"]),
    )
    if new_plays:
        db.log_event(f"{station['name']}: +{new_plays} play(s) via {source}",
                     source="poll")


async def poll_station(station: dict[str, Any]) -> None:
    try:
        observations, source = await sources.fetch_station(station)
    except Exception as exc:  # noqa: BLE001 - shown in the UI, never fatal
        db.execute(
            "UPDATE stations SET last_polled_at = ?, last_error = ? WHERE id = ?",
            (db.now(), str(exc)[:500], station["id"]),
        )
        return
    _record_poll(station, observations, source)


# AzuraCast servers whose unscoped /api/nowplaying did not answer usefully,
# mapped to when it is worth trying again. Without this memo a deployment that
# does not expose it would pay one failed request per server on every single
# poll, which is the opposite of the point.
_batch_unsupported: dict[str, float] = {}
BATCH_RETRY_AFTER = 1800.0


async def _poll_azuracast_server(base: str, group: list[dict[str, Any]]) -> None:
    """Poll every station on one AzuraCast server, in one request where possible.

    Falls back to per-station polling *serially* rather than concurrently. The
    fallback runs when a server is already behaving unexpectedly, which is the
    worst moment to open four connections to it at once.
    """
    retry_at = _batch_unsupported.get(base, 0.0)
    if time.monotonic() >= retry_at:
        try:
            by_shortcode = await sources.fetch_azuracast_server(base)
        except Exception as exc:  # noqa: BLE001 - fall back, never fatal
            if not retry_at:  # say it once per outage, not every 45 seconds
                db.log_event(
                    f"{base} did not answer /api/nowplaying ({exc}); polling its "
                    f"{len(group)} station(s) one at a time instead.",
                    level="warn", source="poll",
                )
            _batch_unsupported[base] = time.monotonic() + BATCH_RETRY_AFTER
        else:
            if _batch_unsupported.pop(base, None):
                db.log_event(f"{base} is answering /api/nowplaying again; back to "
                             "one request per server.", source="poll")
            missed = []
            for station in group:
                observations = by_shortcode.get(
                    (station["azuracast_shortcode"] or "").strip()
                )
                if observations:
                    _record_poll(station, observations, "azuracast")
                else:
                    # In the response but silent, or not in it at all - either way
                    # this station needs asking directly.
                    missed.append(station)
            for station in missed:
                await poll_station(station)
            return

    for station in group:
        await poll_station(station)


async def poll_once() -> None:
    """Read every enabled station, at most one request per server at a time.

    Stations are grouped by AzuraCast server rather than polled independently.
    Six stations used to mean six simultaneous requests, and because a network of
    stations is normally several mounts on *one* server, most of those landed on
    the same host at the same instant, every 45 seconds, forever.
    """
    stations = [dict(r) for r in db.query("SELECT * FROM stations WHERE enabled = 1")]
    if not stations:
        return

    groups: dict[str, list[dict[str, Any]]] = {}
    solo: list[dict[str, Any]] = []
    for station in stations:
        base = (station.get("azuracast_base") or "").strip().rstrip("/")
        if base and station.get("azuracast_shortcode"):
            groups.setdefault(base, []).append(station)
        else:
            solo.append(station)

    # A server hosting one station gains nothing from the batch endpoint - it
    # would return every *other* station on that host too - so it keeps the
    # direct call.
    for base, group in list(groups.items()):
        if len(group) == 1:
            solo.extend(groups.pop(base))

    _state["polling"] = True
    try:
        # Still concurrent, but now across *servers* rather than across stations,
        # so no host sees more than one request from us at a time.
        await asyncio.gather(
            *(_poll_azuracast_server(base, group) for base, group in groups.items()),
            *(poll_station(s) for s in solo),
            return_exceptions=True,
        )
        _state["last_poll"] = db.now()
    finally:
        _state["polling"] = False


# --- matching ----------------------------------------------------------------

def _store_candidates(song_id: int, candidates: list[dict[str, Any]]) -> None:
    import json

    db.execute("DELETE FROM candidates WHERE song_id = ?", (song_id,))
    rows = []
    for rank, cand in enumerate(candidates):
        source, ext_id = cand.get("source", ""), cand.get("ext_id", "")
        # A merged candidate carries both identities explicitly; an unmerged one
        # only has whichever its own provider issued.
        mbid = cand.get("mbid") or (ext_id if source == "musicbrainz" else "")
        spotify_id = cand.get("spotify_id") or (ext_id if source == "spotify" else "")
        rows.append((
            song_id, source, ext_id, mbid, spotify_id,
            cand.get("artist", ""), cand.get("title", ""), cand.get("album", ""),
            cand.get("duration"), cand.get("art_url", ""), cand.get("url", ""),
            cand.get("uri", ""), cand.get("isrc", ""), float(cand.get("score", 0)),
            json.dumps(cand.get("score_detail", {})), rank, db.now(),
        ))
    if rows:
        db.executemany(
            "INSERT OR REPLACE INTO candidates (song_id, source, ext_id, mbid, spotify_id, "
            "artist, title, album, duration, art_url, url, uri, isrc, score, score_detail, "
            "rank, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )


def apply_match(song_id: int, best: dict[str, Any], status: str,
                confidence: float, method: str) -> None:
    db.execute(
        "UPDATE songs SET status = ?, confidence = ?, match_method = ?, "
        "match_artist = ?, match_title = ?, match_album = ?, match_duration = ?, "
        "mbid = ?, isrc = ?, spotify_id = ?, spotify_uri = ?, spotify_url = ?, "
        "match_art_url = ?, resolved_at = ? WHERE id = ?",
        (
            status, confidence, method,
            titlecase_display(best.get("artist", "")),
            titlecase_display(best.get("title", "")),
            best.get("album", ""), best.get("duration"),
            best.get("mbid") or (best.get("ext_id") if best.get("source") == "musicbrainz" else None),
            best.get("isrc") or None,
            best.get("spotify_id") or (best.get("ext_id") if best.get("source") == "spotify" else None),
            best.get("uri") or None,
            best.get("url") or None,
            best.get("art_url") or None,
            db.now(), song_id,
        ),
    )


def enqueue_for_playlists(song_id: int) -> None:
    """Add a resolved song to the playlist of every station that has played it."""
    stations = db.query(
        "SELECT DISTINCT station_id FROM plays WHERE song_id = ?", (song_id,)
    )
    for row in stations:
        db.execute(
            "INSERT OR IGNORE INTO playlist_entries (station_id, song_id, added_at) "
            "VALUES (?, ?, ?)",
            (row["station_id"], song_id, db.now()),
        )


async def match_song(song: dict[str, Any]) -> str:
    db.execute(
        "UPDATE songs SET attempts = attempts + 1, last_attempt_at = ? WHERE id = ?",
        (db.now(), song["id"]),
    )
    result = await matcher.resolve(
        song["raw_artist"], song["raw_title"], song["raw_album"], song["duration"]
    )

    if result.status == "nonsong":
        db.execute(
            "UPDATE songs SET status = 'nonsong', nonsong_reason = ?, confidence = 0, "
            "match_method = ?, resolved_at = ? WHERE id = ?",
            (result.reason, result.method, db.now(), song["id"]),
        )
        return "nonsong"

    if result.candidates:
        _store_candidates(int(song["id"]), result.candidates)

    if result.best is None:
        # A provider outage must not harden into a permanent "not found": leave
        # the song pending so the backoff query picks it up again later.
        if result.retryable:
            # Hand the attempt back. Every provider being unavailable says nothing
            # about this song, and counting it would let a long outage burn the
            # whole queue's retry budget and mark hundreds of songs unmatched.
            # A cooldown makes each of these retries cost no requests at all.
            db.execute(
                "UPDATE songs SET status = 'pending', attempts = MAX(attempts - 1, 0), "
                "nonsong_reason = ? WHERE id = ?",
                (f"Retrying — {result.reason}"[:500], song["id"]),
            )
            return "retry"
        db.execute(
            "UPDATE songs SET status = ?, confidence = 0, match_method = ?, "
            "nonsong_reason = ? WHERE id = ?",
            (result.status, result.method, result.reason[:500], song["id"]),
        )
        return result.status

    best = result.best
    if result.status == "matched":
        # A playlist needs something playable, so try to attach a Spotify URI
        # to identifications that came from MusicBrainz alone.
        try:
            best = await matcher.enrich_with_spotify(
                best, song["raw_artist"], song["raw_title"], song["duration"]
            )
        except spotify.SpotifyThrottled:
            # The identification is already correct and only the playable link is
            # missing, so keep the match. `link_unlinked_songs` attaches the URI
            # once Spotify is answering again, which is far cheaper than throwing
            # away a good match or re-running the whole resolve to get it back.
            pass

    apply_match(int(song["id"]), best, result.status, result.confidence, result.method)

    if result.status == "matched":
        enqueue_for_playlists(int(song["id"]))
    return result.status


async def match_pending(limit: int = MATCH_BATCH) -> dict[str, int]:
    # Escalating backoff keeps a retrying song from monopolising the loop while
    # a provider is down, without ever dropping it.
    songs = [dict(r) for r in db.query(
        "SELECT * FROM songs WHERE status = 'pending' "
        "AND (last_attempt_at IS NULL OR last_attempt_at <= ?) "
        "ORDER BY play_count DESC, last_seen_at DESC LIMIT ?",
        (db.now() - RETRY_BACKOFF_SECONDS, limit)
    )]
    if not songs:
        return {}

    _state["matching"] = True
    counts: dict[str, int] = {}
    try:
        for song in songs:
            try:
                status = await asyncio.wait_for(match_song(song), MATCH_SONG_TIMEOUT)
            except (asyncio.TimeoutError, TimeoutError):
                # match_song already counted this attempt, so the song still ages
                # out through MAX_RETRY_ATTEMPTS instead of retrying forever.
                db.log_event(
                    f"Match timed out after {MATCH_SONG_TIMEOUT}s for "
                    f"{song['raw_artist']} - {song['raw_title']}; leaving it queued",
                    level="warn", source="match",
                )
                attempts = int(song["attempts"]) + 1
                if attempts >= MAX_RETRY_ATTEMPTS:
                    db.execute(
                        "UPDATE songs SET status = 'unmatched', nonsong_reason = ? "
                        "WHERE id = ?",
                        (f"Timed out after {attempts} attempts", song["id"]),
                    )
                else:
                    db.execute(
                        "UPDATE songs SET status = 'pending', nonsong_reason = ? "
                        "WHERE id = ?",
                        (f"Retrying — timed out after {MATCH_SONG_TIMEOUT}s", song["id"]),
                    )
                status = "timeout"
            except Exception as exc:  # noqa: BLE001 - one bad song must not stop the queue
                db.log_event(
                    f"Match failed for {song['raw_artist']} - {song['raw_title']}: {exc}",
                    level="error", source="match",
                )
                # Leave it pending but recorded; attempts stops it spinning forever.
                if int(song["attempts"]) >= 4:
                    db.execute(
                        "UPDATE songs SET status = 'unmatched', nonsong_reason = ? WHERE id = ?",
                        (f"Repeated errors: {exc}"[:500], song["id"]),
                    )
                status = "error"
            counts[status] = counts.get(status, 0) + 1
    finally:
        _state["matching"] = False
    return counts


# --- loops -------------------------------------------------------------------

async def _poll_loop() -> None:
    while True:
        try:
            await poll_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            db.log_event(f"Poll loop error: {exc}", level="error", source="poll")
        await asyncio.sleep(max(15, db.get_int("poll_interval_seconds", 45)))


async def _match_loop() -> None:
    while True:
        try:
            counts = await match_pending()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            db.log_event(f"Match loop error: {exc}", level="error", source="match")
            counts = {}
        # Drain quickly while there is a backlog, idle politely when there is not.
        await asyncio.sleep(2 if counts else 20)


# How many unlinked songs one healing pass will try, and how long it may spend
# trying them.
#
# The count used to be five, chosen "so a long backlog cannot turn into a burst
# of Spotify calls" - but it never controlled the burst. The client's own rate
# lock does that, and now `backoff.Budget` controls the sustained volume as well.
# All five ever governed was how much work happened per pass, and at five per two
# minutes a backlog of two and a half thousand songs needs seventeen hours of
# uninterrupted Spotify availability to clear. That is longer than the gaps
# between outages, so the queue was not draining at all - it was losing ground to
# the new matches arriving behind it.
#
# The work is also far cheaper than that number assumed: most of these songs
# carry an ISRC and resolve in one exact request.
#
# The deadline is what keeps the sync loop punctual. The budget paces by
# sleeping, so a batch that runs into the ceiling slows down rather than being
# refused, and without a clock on it a large batch could hold playlist delivery
# up behind it.
LINK_BATCH = 40
LINK_PASS_SECONDS = 45.0

# Backoff between attempts at the same song: 10 minutes, doubling, settling at a
# day. Plenty of correctly identified songs are simply not on Spotify, and the
# pass has no way to tell those from ones Spotify has not indexed yet. Without a
# memory of having tried, `ORDER BY play_count DESC LIMIT 5` handed back the
# same five most-played unlinkable songs every two minutes and searched them
# again - a few hundred requests an hour, forever, against an application-wide
# quota, achieving nothing. Converging on daily keeps the door open at a cost of
# roughly one search per song per day.
LINK_BACKOFF_BASE = 600
LINK_BACKOFF_MAX = 86400


async def link_unlinked_songs(limit: int = LINK_BATCH) -> int:
    """Attach a Spotify URI to correct matches that do not have one yet.

    A MusicBrainz-only identification is right but not playable, and enrichment
    is skipped outright when Spotify is throttled. Without this pass those songs
    would stay off the Spotify playlist permanently, because sync only ever
    considers entries that already carry a URI.

    This is the only thing that clears that backlog, so it is also the pass whose
    throughput decides whether the Spotify playlists keep up with the stations at
    all. It stops for one of three reasons - the batch is done, the pass has run
    out of clock, or Spotify has stopped answering - and only the first two are
    allowed to cost a song an attempt.
    """
    if not spotify.is_configured() or spotify.cooldown_remaining() > 0:
        return 0

    songs = [dict(r) for r in db.query(
        "SELECT * FROM songs WHERE status IN ('matched', 'confirmed') "
        "AND (spotify_uri IS NULL OR spotify_uri = '') "
        "AND (link_after IS NULL OR link_after <= ?) "
        # An ISRC is an exact identifier: one request, and much the highest hit
        # rate of anything this pass does. A song without one costs up to five
        # text searches for a worse answer, so the cheap certain work goes first
        # and a scarce budget is spent where it converts.
        "ORDER BY (COALESCE(isrc, '') != '') DESC, play_count DESC LIMIT ?",
        (db.now(), limit),
    )]

    deadline = time.monotonic() + LINK_PASS_SECONDS
    linked = 0
    for song in songs:
        if time.monotonic() >= deadline:
            break
        # Record the attempt before making it, so a song that throws, times out
        # or simply is not on Spotify still backs off rather than being retried
        # on the very next pass.
        attempts = int(song["link_attempts"] or 0) + 1
        db.execute(
            "UPDATE songs SET link_attempts = ?, link_after = ? WHERE id = ?",
            (attempts,
             db.now() + min(LINK_BACKOFF_BASE * 2 ** (attempts - 1), LINK_BACKOFF_MAX),
             song["id"]),
        )
        candidate = {
            "source": "musicbrainz" if song["mbid"] else "",
            "artist": song["match_artist"] or song["raw_artist"],
            "title": song["match_title"] or song["raw_title"],
            "album": song["match_album"] or "",
            "duration": song["match_duration"] or song["duration"],
            "isrc": song["isrc"] or "",
            "mbid": song["mbid"],
            "art_url": song["match_art_url"] or "",
        }
        try:
            merged = await matcher.enrich_with_spotify(
                candidate, song["raw_artist"], song["raw_title"], song["duration"]
            )
        except spotify.SpotifyThrottled:
            # Nothing was sent on this song's behalf, so it must not be charged
            # for the outage. Put its backoff back exactly as it was and stop:
            # every remaining song in the batch would be refused too.
            #
            # This is what parked correctly matched songs for a day at a time.
            # The attempt was written before the call, `enrich_with_spotify`
            # swallowed the refusal, and a song Spotify had never been asked
            # about came out of the pass looking like one Spotify had declined -
            # then compounded, because the backoff doubles toward a full day and
            # every subsequent outage stole another attempt from it.
            db.execute(
                "UPDATE songs SET link_attempts = ?, link_after = ? WHERE id = ?",
                (attempts - 1, song["link_after"], song["id"]),
            )
            break
        except Exception as exc:  # noqa: BLE001 - best effort, never fatal
            db.log_event(f"Could not link {candidate['artist']} - {candidate['title']} "
                         f"to Spotify: {exc}", level="warn", source="sync")
            continue
        if not merged.get("uri"):
            continue
        db.execute(
            "UPDATE songs SET spotify_id = ?, spotify_uri = ?, spotify_url = ?, "
            "isrc = COALESCE(NULLIF(?, ''), isrc), "
            "match_art_url = COALESCE(NULLIF(match_art_url, ''), NULLIF(?, '')), "
            "link_attempts = 0, link_after = NULL "
            "WHERE id = ?",
            (merged.get("spotify_id"), merged.get("uri"), merged.get("url"),
             merged.get("isrc") or "", merged.get("art_url") or "", song["id"]),
        )
        enqueue_for_playlists(int(song["id"]))
        linked += 1

    if linked:
        db.log_event(f"Attached a Spotify link to {linked} previously unlinked match(es)",
                     source="sync")
    return linked


async def _sync_loop() -> None:
    while True:
        try:
            await link_unlinked_songs()
            await playlists.sync_all()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            db.log_event(f"Playlist sync error: {exc}", level="error", source="sync")
        await asyncio.sleep(120)


def start() -> None:
    if _tasks:
        return
    loop = asyncio.get_event_loop()
    _tasks.extend([
        loop.create_task(_poll_loop(), name="poll"),
        loop.create_task(_match_loop(), name="match"),
        loop.create_task(_sync_loop(), name="sync"),
    ])


async def stop() -> None:
    for task in _tasks:
        task.cancel()
    for task in _tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    _tasks.clear()
    await sources.aclose()


def state() -> dict[str, Any]:
    return dict(_state)
