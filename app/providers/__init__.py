"""The catalogues the matcher can ask, and one registry describing them.

Every provider module exposes the same small surface, which is what lets the
matcher fan out over them without knowing what any of them is:

    search(artist, title, limit, *, interactive) -> list[candidate dict]
    is_configured() -> bool          # credentials present, where any are needed
    status() -> dict                 # breaker state, for the dashboard
    resume() -> float                # clear a cooldown on the user's say-so
    cooldown_remaining() -> float
    aclose()

A candidate dict is the common currency: source, ext_id, artist, title, album,
duration, url, uri, art_url, isrc, native_score, secondary_types, is_bootleg.
`matcher.score_candidate` reads only those fields, so a provider is free to be
as different underneath as it likes.

The registry below is the single place any of them is named. It used to be three
places - the matcher's fan-out, the API's resume endpoint and two hardcoded
lists in the web UI - which is exactly the kind of list that grows a fourth
entry in two of the three and quietly stops matching.

What each one is for
--------------------
MusicBrainz  canonical identity. Free MBIDs, careful editorial data, no
             commercial bias. Weakest on obscure novelty singles.
Spotify      the playable one. A match without a Spotify URI cannot reach a
             Spotify playlist, so this is the provider delivery depends on.
Deezer       coverage, and the only one that hands back an ISRC for free, which
             makes cross-provider identity exact instead of inferred.
Apple Music  coverage of the long tail - seasonal compilations, novelty singles
             and small-label reissues that the other three often miss.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .. import db
from . import deezer, itunes, musicbrainz, spotify

__all__ = ["Provider", "REGISTRY", "by_key", "enabled", "statuses",
           "deezer", "itunes", "musicbrainz", "spotify"]


@dataclass(frozen=True)
class Provider:
    key: str          # stats key and /api/providers/{key}/resume path segment
    label: str        # what a person is shown, in logs and in the UI
    setting: str      # settings flag that switches it off
    module: Any


REGISTRY: tuple[Provider, ...] = (
    Provider("musicbrainz", "MusicBrainz", "use_musicbrainz", musicbrainz),
    Provider("spotify", "Spotify", "use_spotify", spotify),
    Provider("deezer", "Deezer", "use_deezer", deezer),
    Provider("itunes", "Apple Music", "use_itunes", itunes),
)


def by_key(key: str) -> Provider | None:
    wanted = key.strip().lower()
    return next((p for p in REGISTRY if p.key == wanted), None)


def enabled() -> list[Provider]:
    """Providers switched on and usable right now.

    A provider in a cooldown is deliberately still included: it refuses locally
    without sending a request, and the refusal is what tells the caller the
    difference between "nobody has this recording" and "we could not ask".
    """
    return [p for p in REGISTRY
            if db.get_bool(p.setting, True) and p.module.is_configured()]


def statuses() -> list[dict[str, Any]]:
    """Per-provider state for the dashboard, in registry order.

    Carries the label alongside the key so the UI can name a paused provider and
    offer the resume button for it without holding its own copy of this list.
    """
    return [
        {
            "key": p.key,
            "label": p.label,
            "enabled": db.get_bool(p.setting, True),
            "configured": p.module.is_configured(),
            **p.module.status(),
        }
        for p in REGISTRY
    ]
