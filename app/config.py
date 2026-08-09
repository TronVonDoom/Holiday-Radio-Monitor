"""Runtime configuration.

Environment variables provide the container-level defaults (what UnRaid sets in the
template). Anything a user should be able to change without restarting the container
lives in the `settings` table instead and is read through `app.db.get_setting`.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "Holiday Radio Matcher"
APP_VERSION = "1.0.3"
USER_AGENT = (
    f"HolidayRadioMatcher/{APP_VERSION} "
    "( https://github.com/TronVonDoom/Holiday-Radio-Monitor )"
)


def _env_path(name: str, default: str) -> Path:
    return Path(os.getenv(name, default)).expanduser()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


# Persisted state. On UnRaid this is bound to /mnt/user/appdata/holiday-radio-matcher.
CONFIG_DIR = _env_path("HRM_CONFIG_DIR", "/config")

# Where generated .m3u8 playlists are written. Bound to a share the user picks.
PLAYLIST_DIR = _env_path("HRM_PLAYLIST_DIR", "/playlists")

DB_PATH = CONFIG_DIR / "holiday-radio.db"

HOST = os.getenv("HRM_HOST", "0.0.0.0")
PORT = _env_int("HRM_PORT", 8080)

# Optional: set to protect the UI when exposed beyond the LAN. Blank disables auth.
AUTH_TOKEN = os.getenv("HRM_AUTH_TOKEN", "").strip()

# Needed only behind a reverse proxy, where the URL the browser used is not the
# URL the container sees. Must match a redirect URI registered in the Spotify app.
SPOTIFY_REDIRECT_URI = os.getenv("HRM_SPOTIFY_REDIRECT_URI", "").strip()

WEB_DIR = Path(__file__).parent / "web"

# Defaults seeded into the `settings` table on first run. Everything here is
# editable in the UI at runtime.
DEFAULT_SETTINGS: dict[str, str] = {
    # --- Matching thresholds -------------------------------------------------
    # >= auto_accept  -> matched automatically and queued for the playlist
    # >= review_floor -> parked in the review queue with ranked candidates
    # <  review_floor -> marked unmatched (still reviewable, no candidates shown)
    "auto_accept_score": "0.92",
    "review_floor_score": "0.62",
    # Tracks shorter than this are treated as jingles/station IDs, never songs.
    "min_song_seconds": "45",
    # --- Providers -----------------------------------------------------------
    "use_musicbrainz": "1",
    "use_spotify": "1",
    "spotify_client_id": "",
    "spotify_client_secret": "",
    "spotify_market": "US",
    # --- Playlist delivery ---------------------------------------------------
    "spotify_sync_enabled": "1",
    "m3u_enabled": "1",
    # --- Poller --------------------------------------------------------------
    "poll_interval_seconds": "45",
    "musicbrainz_rate_limit_seconds": "1.1",
    # How long to stop calling a provider after it returns 503/429. Escalates to
    # 3x, 10x and 30x this value while the throttling persists.
    "musicbrainz_cooldown_seconds": "60",
    "spotify_cooldown_seconds": "60",
}
