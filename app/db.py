"""SQLite access layer.

Single-writer WAL database. Everything the app knows lives here so the whole
container state is one file the user can back up from /config.

Entity model
------------
stations         a monitored stream
plays            append-only log of every observed play (history + stats)
songs            one row per distinct piece of stream metadata; the thing that
                 gets matched, reviewed and eventually added to a playlist
candidates       ranked provider results attached to a song, for the review UI
aliases          learned normalized-key -> confirmed identity, so a track the
                 user resolves once is auto-matched forever after
playlist_entries song membership in a station playlist + per-target sync state
"""

from __future__ import annotations

import itertools
import sqlite3
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Iterable

from . import config
from .normalize import fingerprint

_local = threading.local()
_write_lock = threading.Lock()

# Bumped by every write. Read-side caches key off it so they can be exact rather
# than merely fresh: an answer computed at generation N is still the right answer
# at generation N, however long ago it was computed.
_write_generation = 0

# Set during init_db. False only on a SQLite build without FTS5, where search
# falls back to LIKE.
FTS_ENABLED = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    slug                TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    holiday             TEXT NOT NULL DEFAULT 'halloween',
    enabled             INTEGER NOT NULL DEFAULT 1,
    -- Preferred source: AzuraCast JSON API (rich, includes history + duration)
    azuracast_base      TEXT,
    azuracast_shortcode TEXT,
    -- Fallback source: raw Icecast/SHOUTcast mount with ICY metadata
    icy_url             TEXT,
    spotify_playlist_id TEXT,
    -- When the playlist was last read back from Spotify to check our record of
    -- what is in it. Reading it is what makes delivery idempotent against manual
    -- edits, and also the single most expensive thing this app does, so it is
    -- done on a schedule rather than on every pass.
    spotify_reconciled_at INTEGER,
    m3u_filename        TEXT,
    last_polled_at      INTEGER,
    last_error          TEXT,
    created_at          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS songs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Stable identity for a piece of stream metadata. AzuraCast gives us a hash;
    -- otherwise we derive one from the normalized artist/title.
    fingerprint     TEXT NOT NULL UNIQUE,
    raw_artist      TEXT NOT NULL DEFAULT '',
    raw_title       TEXT NOT NULL DEFAULT '',
    raw_album       TEXT NOT NULL DEFAULT '',
    norm_artist     TEXT NOT NULL DEFAULT '',
    norm_title      TEXT NOT NULL DEFAULT '',
    duration        INTEGER,
    art_url         TEXT,
    -- pending | matched | review | unmatched | confirmed | archived | nonsong
    status          TEXT NOT NULL DEFAULT 'pending',
    -- Status held before the user archived the song, so restoring puts it back
    -- in the same place in the review queue rather than guessing.
    archived_from   TEXT,
    confidence      REAL NOT NULL DEFAULT 0,
    match_method    TEXT,
    -- Resolved identity
    match_artist    TEXT,
    match_title     TEXT,
    match_album     TEXT,
    match_duration  INTEGER,
    mbid            TEXT,
    isrc            TEXT,
    spotify_id      TEXT,
    spotify_uri     TEXT,
    spotify_url     TEXT,
    match_art_url   TEXT,
    nonsong_reason  TEXT,
    -- Backoff for the Spotify-link healing pass. A song that is simply not on
    -- Spotify can never be linked, and without a memory of having tried, the
    -- pass re-searches the same few every couple of minutes forever.
    link_attempts   INTEGER NOT NULL DEFAULT 0,
    link_after      INTEGER,
    play_count      INTEGER NOT NULL DEFAULT 0,
    first_seen_at   INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL,
    resolved_at     INTEGER,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_attempt_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_songs_status ON songs(status);
CREATE INDEX IF NOT EXISTS idx_songs_last_seen ON songs(last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_songs_normkey ON songs(norm_artist, norm_title);

CREATE TABLE IF NOT EXISTS plays (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id  INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    song_id     INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    played_at   INTEGER NOT NULL,
    UNIQUE(station_id, song_id, played_at)
);
CREATE INDEX IF NOT EXISTS idx_plays_played ON plays(played_at DESC);
CREATE INDEX IF NOT EXISTS idx_plays_station ON plays(station_id, played_at DESC);

CREATE TABLE IF NOT EXISTS candidates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    song_id       INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    source        TEXT NOT NULL,          -- musicbrainz | spotify
    ext_id        TEXT NOT NULL,
    -- Both identities, because a candidate can be the merge of the same
    -- recording found in both databases: the MBID is the canonical identity and
    -- the Spotify id is the playable one, and confirming needs to keep both.
    mbid          TEXT,
    spotify_id    TEXT,
    artist        TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL DEFAULT '',
    album         TEXT NOT NULL DEFAULT '',
    duration      INTEGER,
    art_url       TEXT,
    url           TEXT,
    uri           TEXT,
    isrc          TEXT,
    score         REAL NOT NULL DEFAULT 0,
    score_detail  TEXT,
    rank          INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    UNIQUE(song_id, source, ext_id)
);
CREATE INDEX IF NOT EXISTS idx_candidates_song ON candidates(song_id, score DESC);

CREATE TABLE IF NOT EXISTS aliases (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key_artist   TEXT NOT NULL,
    key_title    TEXT NOT NULL,
    match_artist TEXT,
    match_title  TEXT,
    match_album  TEXT,
    duration     INTEGER,
    mbid         TEXT,
    isrc         TEXT,
    spotify_id   TEXT,
    spotify_uri  TEXT,
    spotify_url  TEXT,
    art_url      TEXT,
    -- 'nonsong' aliases teach the filter that this metadata is never a song
    kind         TEXT NOT NULL DEFAULT 'match',
    hits         INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL,
    UNIQUE(key_artist, key_title)
);

CREATE TABLE IF NOT EXISTS playlist_entries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id     INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    song_id        INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    added_at       INTEGER NOT NULL,
    spotify_synced INTEGER NOT NULL DEFAULT 0,
    m3u_synced     INTEGER NOT NULL DEFAULT 0,
    UNIQUE(station_id, song_id)
);
CREATE INDEX IF NOT EXISTS idx_entries_station ON playlist_entries(station_id, added_at DESC);
-- Reclassifying a song clears its playlist membership by song_id, and the merge
-- migration reads entries the same way. Without this both scan the table.
CREATE INDEX IF NOT EXISTS idx_entries_song ON playlist_entries(song_id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    level      TEXT NOT NULL DEFAULT 'info',
    source     TEXT NOT NULL DEFAULT '',
    message    TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);
"""

# Full-text search over the four searchable name columns.
#
# The Library's search box used to be `LIKE '%q%'`, which no index can serve, so
# every keystroke scanned the whole songs table twice - once to count and once
# to page. This is an external-content index: it stores no copy of the rows, only
# the terms, and reads the columns back out of `songs` by rowid.
#
# The update trigger is deliberately `UPDATE OF <the four columns>`. A plain
# `AFTER UPDATE` fires on every poll, because ingesting a play bumps
# `last_seen_at`, and would re-index a song several times an hour for a name
# that never changed.
#
# Bump FTS_GENERATION when the indexed columns or the tokenizer change, so
# existing databases rebuild instead of searching a stale index.
FTS_GENERATION = "1"

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS songs_fts USING fts5(
    raw_artist, raw_title, match_artist, match_title,
    content='songs', content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);

CREATE TRIGGER IF NOT EXISTS songs_fts_insert AFTER INSERT ON songs BEGIN
    INSERT INTO songs_fts(rowid, raw_artist, raw_title, match_artist, match_title)
    VALUES (new.id, new.raw_artist, new.raw_title, new.match_artist, new.match_title);
END;

CREATE TRIGGER IF NOT EXISTS songs_fts_delete AFTER DELETE ON songs BEGIN
    INSERT INTO songs_fts(songs_fts, rowid, raw_artist, raw_title, match_artist, match_title)
    VALUES ('delete', old.id, old.raw_artist, old.raw_title, old.match_artist, old.match_title);
END;

CREATE TRIGGER IF NOT EXISTS songs_fts_update
AFTER UPDATE OF raw_artist, raw_title, match_artist, match_title ON songs BEGIN
    INSERT INTO songs_fts(songs_fts, rowid, raw_artist, raw_title, match_artist, match_title)
    VALUES ('delete', old.id, old.raw_artist, old.raw_title, old.match_artist, old.match_title);
    INSERT INTO songs_fts(rowid, raw_artist, raw_title, match_artist, match_title)
    VALUES (new.id, new.raw_artist, new.raw_title, new.match_artist, new.match_title);
END;
"""


def now() -> int:
    return int(time.time())


def write_generation() -> int:
    """How many writes this process has made. See `_write_generation`."""
    return _write_generation


def connect() -> sqlite3.Connection:
    """Per-thread connection. FastAPI's threadpool reuses threads, so this is cheap."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        _local.conn = conn
    return conn


def init_db() -> None:
    conn = connect()
    with _write_lock:
        conn.executescript(SCHEMA)
        for key, value in config.DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO NOTHING",
                (key, value),
            )
        _migrate(conn)
        # Last, because the dedupe migration rewrites `songs` wholesale and the
        # index has to be built from whatever it leaves behind.
        _init_fts(conn)
        conn.commit()


def _init_fts(conn: sqlite3.Connection) -> None:
    """Build the search index, and cope with a SQLite that has no FTS5.

    Called with `_write_lock` already held, so it must use `conn` directly.

    FTS5 is compiled into every SQLite this app is likely to meet, but "likely"
    is not "always", and a missing module should cost the user a slower search
    rather than a container that will not start.
    """
    global FTS_ENABLED
    try:
        conn.executescript(FTS_SCHEMA)
    except sqlite3.Error as exc:  # noqa: BLE001 - any failure means no FTS
        FTS_ENABLED = False
        conn.execute(
            "INSERT INTO events(level, source, message, created_at) VALUES(?,?,?,?)",
            ("warn", "setup",
             f"This SQLite build has no FTS5 ({exc}); library search will fall "
             "back to a slower scan.", now()),
        )
        return

    FTS_ENABLED = True

    # Whether the index has been populated is recorded rather than measured. An
    # external-content FTS5 table answers a bare `COUNT(*)` by reading straight
    # through to `songs`, so counting the two and comparing them always agrees
    # and would never rebuild anything. Bump FTS_GENERATION to force a rebuild
    # when the columns or the tokenizer here change.
    built = conn.execute(
        "SELECT value FROM settings WHERE key = 'fts_index_generation'"
    ).fetchone()
    if built is not None and built["value"] == FTS_GENERATION:
        return

    conn.execute("INSERT INTO songs_fts(songs_fts) VALUES('rebuild')")
    conn.execute(
        "INSERT INTO settings(key, value) VALUES('fts_index_generation', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (FTS_GENERATION,),
    )


def _migrate(conn: sqlite3.Connection) -> None:
    """One-time data repairs. Called with `_write_lock` already held.

    Must use `conn` directly: the module-level helpers take the same non-reentrant
    lock and would deadlock.
    """
    done = {
        row["key"] for row in
        conn.execute("SELECT key FROM settings WHERE value = '1' AND key LIKE 'migrated_%'")
    }

    # `CREATE TABLE IF NOT EXISTS` leaves an existing table untouched, so columns
    # added after first run have to be attached explicitly.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(songs)")}
    if "archived_from" not in columns:
        conn.execute("ALTER TABLE songs ADD COLUMN archived_from TEXT")
    if "link_attempts" not in columns:
        conn.execute("ALTER TABLE songs ADD COLUMN link_attempts INTEGER NOT NULL DEFAULT 0")
    if "link_after" not in columns:
        conn.execute("ALTER TABLE songs ADD COLUMN link_after INTEGER")

    station_columns = {row["name"] for row in conn.execute("PRAGMA table_info(stations)")}
    if "spotify_reconciled_at" not in station_columns:
        conn.execute("ALTER TABLE stations ADD COLUMN spotify_reconciled_at INTEGER")

    cand_columns = {row["name"] for row in conn.execute("PRAGMA table_info(candidates)")}
    for column in ("mbid", "spotify_id"):
        if column not in cand_columns:
            conn.execute(f"ALTER TABLE candidates ADD COLUMN {column} TEXT")

    # Spotify's cooldown floor used to match MusicBrainz's 60s and escalate to
    # 30x it. Spotify sends an accurate Retry-After, so that floor was pure dead
    # time on top of the wait Spotify actually asked for - half an hour of it at
    # the top step. Only rewrite the value if it is still the old default; a
    # figure the user chose themselves is theirs to keep.
    if "migrated_spotify_cooldown_floor" not in done:
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = 'spotify_cooldown_seconds' "
            "AND value = '60'", (config.DEFAULT_SETTINGS["spotify_cooldown_seconds"],)
        )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('migrated_spotify_cooldown_floor', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'"
        )

    # play_count used to be incremented once per *observation*. Because AzuraCast
    # serves a rolling history window, a single play was counted on every poll it
    # remained visible for, inflating the figure roughly thirtyfold. Recompute it
    # from `plays`, which was always deduplicated and is the real record.
    if "migrated_play_count_from_plays" not in done:
        conn.execute(
            "UPDATE songs SET play_count = "
            "(SELECT COUNT(*) FROM plays WHERE plays.song_id = songs.id)"
        )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('migrated_play_count_from_plays', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'"
        )

    # 'rejected' was a permanent skip with no way back. Archiving replaced it, so
    # anything skipped under the old rule becomes restorable instead of stranded
    # in a status nothing in the UI can reach any more.
    if "migrated_rejected_to_archived" not in done:
        conn.execute("UPDATE songs SET status = 'archived' WHERE status = 'rejected'")
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('migrated_rejected_to_archived', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'"
        )

    # Song identity used to come from the source's own id when one was offered,
    # which meant the same recording got a separate row per station (see
    # `normalize.fingerprint`). Every one of those rows was matched separately,
    # reviewed separately and delivered to the playlist separately. Rebuild the
    # fingerprints on the new rule and fold the collisions together.
    if "migrated_fingerprint_dedupe" not in done:
        merged = _merge_duplicate_songs(conn)
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('migrated_fingerprint_dedupe', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'"
        )
        if merged:
            conn.execute(
                "INSERT INTO events(level, source, message, created_at) VALUES(?,?,?,?)",
                ("info", "setup",
                 f"Merged {merged} duplicate song row(s) that the old per-station "
                 "identity had split apart. Play counts and playlist membership "
                 "were preserved.", now()),
            )


# How much a status is worth when two rows for the same song have to become one.
# A verdict the user gave by hand outranks anything the engine decided, and a
# resolved row outranks an unresolved one, because the merged row should be the
# furthest along rather than merely the oldest.
_STATUS_RANK = {
    "confirmed": 6, "matched": 5, "review": 4, "pending": 3,
    "unmatched": 2, "archived": 1, "nonsong": 0,
}

# Blank fields on the survivor that a duplicate may be able to fill in. Only
# ever used to fill a hole - never to overwrite - so the survivor's own
# identification is always the one that stands.
_FILLABLE = (
    "raw_album", "duration", "art_url", "match_artist", "match_title",
    "match_album", "match_duration", "mbid", "isrc", "spotify_id",
    "spotify_uri", "spotify_url", "match_art_url", "match_method",
)


def _merge_duplicate_songs(conn: sqlite3.Connection) -> int:
    """Fold rows that are the same song into one. Returns how many were removed.

    Called with `_write_lock` held, so it must use `conn` directly.

    Everything that pointed at a removed row is moved onto the survivor first:
    plays keep their history (a play already recorded against the survivor at
    the same second is dropped as the duplicate observation it is), and playlist
    membership keeps the earliest join date and the *union* of the delivery
    flags - a track already pushed to Spotify under one row must not look
    undelivered just because it survived as the other, or sync would send it a
    second time and put it in the playlist twice.
    """
    rows = conn.execute("SELECT * FROM songs").fetchall()

    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(fingerprint(row["raw_artist"], row["raw_title"]), []).append(row)

    # Park every fingerprint somewhere it cannot collide before writing the new
    # ones. `fingerprint` is UNIQUE, and a row being given its canonical value
    # can otherwise collide with a *different* row that still holds that value
    # as its old one - which aborts the whole migration on a constraint the end
    # state does not actually violate.
    conn.execute("UPDATE songs SET fingerprint = 'tmp:' || id")

    def rank(row: sqlite3.Row) -> tuple[int, int, float, int]:
        return (
            _STATUS_RANK.get(row["status"], 0),
            1 if (row["spotify_uri"] or "") else 0,
            float(row["confidence"] or 0.0),
            -int(row["id"]),          # oldest wins a tie
        )

    removed = 0
    for fp, members in groups.items():
        if len(members) > 1:
            members = sorted(members, key=rank, reverse=True)
            keeper, losers = members[0], members[1:]
            keeper_id = int(keeper["id"])
            loser_ids = [int(m["id"]) for m in losers]

            for column in _FILLABLE:
                if keeper[column] in (None, "", 0):
                    filled = next((m[column] for m in losers
                                   if m[column] not in (None, "", 0)), None)
                    if filled is not None:
                        conn.execute(f"UPDATE songs SET {column} = ? WHERE id = ?",
                                     (filled, keeper_id))

            conn.execute(
                "UPDATE songs SET first_seen_at = ?, last_seen_at = ? WHERE id = ?",
                (min(int(m["first_seen_at"]) for m in members),
                 max(int(m["last_seen_at"]) for m in members), keeper_id),
            )

            for loser_id in loser_ids:
                # OR IGNORE keeps a play the survivor already has; the leftovers
                # are removed by the foreign key cascade when the row goes.
                conn.execute("UPDATE OR IGNORE plays SET song_id = ? WHERE song_id = ?",
                             (keeper_id, loser_id))

                for entry in conn.execute(
                    "SELECT * FROM playlist_entries WHERE song_id = ?", (loser_id,)
                ).fetchall():
                    existing = conn.execute(
                        "SELECT * FROM playlist_entries WHERE station_id = ? AND song_id = ?",
                        (entry["station_id"], keeper_id),
                    ).fetchone()
                    if existing is None:
                        conn.execute(
                            "UPDATE playlist_entries SET song_id = ? WHERE id = ?",
                            (keeper_id, entry["id"]),
                        )
                        continue
                    conn.execute(
                        "UPDATE playlist_entries SET added_at = ?, spotify_synced = ?, "
                        "m3u_synced = ? WHERE id = ?",
                        (min(int(existing["added_at"]), int(entry["added_at"])),
                         max(int(existing["spotify_synced"]), int(entry["spotify_synced"])),
                         max(int(existing["m3u_synced"]), int(entry["m3u_synced"])),
                         existing["id"]),
                    )

                # Cascades to this row's remaining plays, entries and candidates.
                conn.execute("DELETE FROM songs WHERE id = ?", (loser_id,))
                removed += 1
        else:
            keeper_id = int(members[0]["id"])

        conn.execute("UPDATE songs SET fingerprint = ? WHERE id = ?", (fp, keeper_id))

    if removed:
        # play_count is a cache of COUNT(*) over `plays`, and merging changed
        # both sides of that, so it is rebuilt rather than added up.
        conn.execute(
            "UPDATE songs SET play_count = "
            "(SELECT COUNT(*) FROM plays WHERE plays.song_id = songs.id)"
        )
    return removed


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, tuple(params)).fetchall()


def query_one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return connect().execute(sql, tuple(params)).fetchone()


def execute(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    global _write_generation
    conn = connect()
    with _write_lock:
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        _write_generation += 1
        return cur


def executemany(sql: str, seq: Iterable[Iterable[Any]]) -> None:
    global _write_generation
    conn = connect()
    with _write_lock:
        conn.executemany(sql, [tuple(p) for p in seq])
        conn.commit()
        _write_generation += 1


# --- settings ---------------------------------------------------------------

def get_setting(key: str, default: str = "") -> str:
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    if row is None:
        return config.DEFAULT_SETTINGS.get(key, default)
    return row["value"]


def get_float(key: str, default: float) -> float:
    try:
        return float(get_setting(key, str(default)))
    except (TypeError, ValueError):
        return default


def get_int(key: str, default: int) -> int:
    try:
        return int(float(get_setting(key, str(default))))
    except (TypeError, ValueError):
        return default


def get_bool(key: str, default: bool = False) -> bool:
    return get_setting(key, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


_UPSERT_SETTING = (
    "INSERT INTO settings(key, value) VALUES(?, ?) "
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
)


def set_setting(key: str, value: str) -> None:
    execute(_UPSERT_SETTING, (key, str(value)))


def set_settings(values: Mapping[str, Any]) -> None:
    """Write many settings as one transaction.

    Saving the Settings page writes about two dozen keys. One at a time, that is
    two dozen turns of the write lock and two dozen fsyncs for what is logically
    a single edit — and a crash halfway leaves half a page applied.
    """
    if not values:
        return
    executemany(_UPSERT_SETTING, [(k, str(v)) for k, v in values.items()])


def all_settings() -> dict[str, str]:
    merged = dict(config.DEFAULT_SETTINGS)
    for row in query("SELECT key, value FROM settings"):
        merged[row["key"]] = row["value"]
    return merged


# --- events -----------------------------------------------------------------

# The log is a diagnostic tail, not an archive, so it is kept bounded — but the
# trim used to run on every single line written, from the poll loop, which meant
# an aggregate and a delete for each. Trimming every hundredth line costs a
# hundredth as much and keeps the table within `_PRUNE_EVERY` rows of the cap.
_EVENTS_KEPT = 500
_PRUNE_EVERY = 100
_event_counter = itertools.count(1)


def log_event(message: str, level: str = "info", source: str = "") -> None:
    execute(
        "INSERT INTO events(level, source, message, created_at) VALUES(?, ?, ?, ?)",
        (level, source, message[:2000], now()),
    )
    if next(_event_counter) % _PRUNE_EVERY == 0:
        execute(
            "DELETE FROM events WHERE id < (SELECT MAX(id) - ? FROM events)",
            (_EVENTS_KEPT,),
        )


def db_path() -> Path:
    return config.DB_PATH
