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
Spotify      the destination, not a catalogue. A match without a Spotify URI
             cannot reach a Spotify playlist, so this is what delivery depends
             on - and it is asked for that URI by exact recording code rather
             than searched for songs the others have already identified. See
             `matching` for the measurements behind that split.
Deezer       coverage, and the only one that hands back an ISRC for free, which
             makes cross-provider identity exact instead of inferred - and is
             what lets the Spotify lookup be exact too.
Apple Music  coverage of the long tail - seasonal compilations, novelty singles
             and small-label reissues that the other three often miss.

Which of them a given path uses is `enabled` (anything a person can trigger)
versus `matching` (the automated loop). They differ only in Spotify.
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
    # Whether the automated match loop fans out to this one. See `matching`.
    background: bool = True


REGISTRY: tuple[Provider, ...] = (
    Provider("musicbrainz", "MusicBrainz", "use_musicbrainz", musicbrainz),
    Provider("spotify", "Spotify", "use_spotify", spotify, background=False),
    Provider("deezer", "Deezer", "use_deezer", deezer),
    Provider("itunes", "Apple Music", "use_itunes", itunes),
)


def by_key(key: str) -> Provider | None:
    wanted = key.strip().lower()
    return next((p for p in REGISTRY if p.key == wanted), None)


def enabled() -> list[Provider]:
    """Providers switched on and usable right now, by any path.

    A provider in a cooldown is deliberately still included: it refuses locally
    without sending a request, and the refusal is what tells the caller the
    difference between "nobody has this recording" and "we could not ask".
    """
    return [p for p in REGISTRY
            if db.get_bool(p.setting, True) and p.module.is_configured()]


def matching() -> list[Provider]:
    """The catalogues the automated match loop asks about a song it just heard.

    Spotify is deliberately not among them, and it is the only provider here that
    is also a *destination*. A match is delivered to a Spotify playlist, which
    needs a URI, and that URI is fetched by exact recording code - not by
    text-searching Spotify's catalogue for a song the other three have already
    identified.

    Measured against this library, Spotify won 26 of 3,469 matches, and of a
    260-song sample not one auto-accepted match would have fallen into review
    without its corroboration. The other three already agree, and Deezer hands
    back the ISRC that makes the delivery lookup exact. So searching Spotify for
    every song the stations play bought almost no identification and spent the
    one quota in this app that is shared application-wide, is not per-machine,
    and answers an overrun with a ban measured in hours rather than seconds.

    Spotify stays in the registry and stays reachable by a person: `enabled`
    still includes it, so an interactive search from the review queue asks the
    catalogue the playlist actually lives in. That is one request, on demand,
    and it is the only route to a song nothing else carries - the opposite kind
    of traffic from a loop that runs on every song, forever.
    """
    return [p for p in enabled() if p.background]


def is_available(key: str) -> bool:
    """Whether one provider may be called at all, by any path.

    Distinct from `is_configured()`, which only asks whether credentials exist.
    A user who unticks a catalogue means it, so the paths that reach a provider
    directly rather than through the fan-out - the ISRC tie-break, most notably -
    have to consult this or the toggle silently does not apply to them.
    """
    provider = by_key(key)
    return bool(provider and db.get_bool(provider.setting, True)
                and provider.module.is_configured())


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
            # So the dashboard can say what a pause actually costs. Spotify going
            # cold stops playlist delivery and leaves identification untouched,
            # which is the opposite of what a cold catalogue means.
            "background": p.background,
            **p.module.status(),
        }
        for p in REGISTRY
    ]
