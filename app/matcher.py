"""The matching engine.

Resolution runs in tiers, cheapest and most certain first:

  0. Non-song filter      - station IDs and jingles are rejected outright.
  1. Learned alias        - anything the user has confirmed before resolves
                            instantly, offline, at full confidence. Radio
                            rotations repeat heavily, so this tier carries most
                            of the traffic once the queue has been worked once.
  2. Provider search      - every enabled catalogue queried concurrently across
                            plausible spellings of the artist and title. See
                            `app.providers` for what each one is there for.
  3. Scoring              - each candidate scored on artist, title and duration
                            agreement, with penalties for re-recordings.
  4. Corroboration        - when independent databases land on the same
                            recording, confidence is raised by how many of them
                            did; when they disagree near the threshold, an ISRC
                            cross-check breaks the tie.

The output is a confidence in [0, 1] which the caller maps onto:
    >= auto_accept   -> matched, queued for the playlist
    >= review_floor  -> review, shown with ranked candidates
    <  review_floor  -> unmatched
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

from . import db, providers
from .normalize import (
    artist_tokens,
    artist_variants,
    detect_nonsong,
    has_impostor_marker,
    norm_key,
    title_variants,
)
from .providers import musicbrainz, spotify

# Relative weights when every signal is available. Title carries slightly more
# than artist because stream artist fields are the more corrupted of the two.
W_TITLE = 0.42
W_ARTIST = 0.38
W_DURATION = 0.20

# A re-recording that scores well on text is the single most damaging false
# positive, so the penalty is large enough to push it below any real match.
IMPOSTOR_PENALTY = 0.45
COMPILATION_PENALTY = 0.03
# A live take matches the studio cut perfectly on text, so without a duration to
# separate them it can win outright. Stations play studio versions almost always.
LIVE_PENALTY = 0.30
BOOTLEG_PENALTY = 0.20

LIVE_MARKERS = ("live at", "live in", "live from", "(live", "[live", " - live",
                "live version", "in concert", "unplugged", "bbc session")

# A candidate whose performer has nothing in common with the one the stream
# named is a different recording, however well the title reads.
#
# This became load-bearing when the free-text catalogues were added. MusicBrainz,
# Spotify and Deezer are asked fielded queries, so a result is the service
# asserting that *this artist* recorded *this title*. Apple Music has no fielded
# syntax: it answers keyword relevance and never says "nothing here", so it
# always hands back its closest guess. Every title metric here deliberately
# tolerates a missing subtitle - that is what rescues stream metadata - and the
# same tolerance rates a title *fragment* highly, so "A Song That Does Not Exist
# At All" scored 0.85 against an unrelated track called "Exist at All". With a
# coincidental track length that was enough to float pure noise to the top of
# the review list.
#
# The artist is the only signal that separates those two cases, so it gets a
# floor. This cannot touch anything already being auto-accepted: with artist
# agreement below the threshold the score cannot exceed
# W_TITLE + 0.25*W_ARTIST + W_DURATION = 0.715, well under any usable
# auto-accept setting, so the only songs it moves are ones already destined for
# a human.
ARTIST_MISMATCH_THRESHOLD = 0.25
ARTIST_MISMATCH_PENALTY = 0.30

# Independent databases landing on the same recording is the strongest evidence
# the engine has short of an exact identifier, and it gets stronger the more of
# them agree - but not proportionally. The streaming catalogues ingest from
# overlapping distributors, so Spotify, Deezer and Apple agreeing with each
# other is not four independent opinions; the honest shape is a solid step for
# the second database and diminishing credit after that.
CORROBORATION_BONUS = {2: 0.07, 3: 0.10}
MAX_CORROBORATION_BONUS = 0.12
ISRC_BONUS = 0.12

# Alias kind meaning "this metadata is real music, whatever the filter thinks".
#
# The non-song heuristics have to be aggressive - station imaging shares a lot
# of surface with music, and one jingle in a playlist is worse than one song in
# review - so they will always catch some real songs. A short interlude, a
# soundtrack cue under the minimum length, an artist field the station left as
# "Various". Marking one of those as a song used to be impossible to undo: the
# heuristics are deterministic, so re-running them produced the same verdict
# forever, and the song was stuck.
#
# This rule carries no identity, unlike a confirmation. It says only "do not
# apply the filter to this metadata", and matching then proceeds normally - so
# the song goes to the providers and lands wherever its confidence puts it.
ALIAS_IS_SONG = "song"

# How many ranked candidates a resolve hands back for the review UI. The UI shows
# the top few and reveals the rest on request, so this is the size of the "load
# more" pool rather than the size of the list anyone reads.
MAX_CANDIDATES = 12


@dataclass
class MatchResult:
    status: str                       # matched | review | unmatched | nonsong
    confidence: float = 0.0
    method: str = ""
    best: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    # True when the answer is unknown because a provider was unreachable, rather
    # than because the recording genuinely could not be found.
    retryable: bool = False


# --- scoring -----------------------------------------------------------------

# partial_ratio finds the best-matching window, which is useful for a title that
# carries an extra subtitle but wildly over-scores short strings: "cher" against
# "hedfunk" rates 66.7. Only trust it once both sides are long enough that an
# accidental window match is unlikely.
MIN_PARTIAL_LEN = 8


def _text_score(variants: list[str], candidate: str, allow_partial: bool = True) -> float:
    """Best fuzzy agreement between any plausible spelling and the candidate."""
    cand = norm_key(candidate)
    if not cand:
        return 0.0
    best = 0.0
    for variant in variants:
        v = norm_key(variant)
        if not v:
            continue
        if v == cand:
            return 1.0
        # token_set_ratio tolerates extra words (e.g. a subtitle the stream drops);
        # ratio punishes them. Blending keeps both behaviours in play.
        score = fuzz.token_set_ratio(v, cand) * 0.6 + fuzz.ratio(v, cand) * 0.4
        if allow_partial and min(len(v), len(cand)) >= MIN_PARTIAL_LEN:
            score = max(score, fuzz.partial_ratio(v, cand) * 0.85)
        best = max(best, score / 100.0)
    return min(best, 1.0)


def _artist_score(raw_artist: str, candidate_artist: str) -> float:
    """Artist agreement, insensitive to ordering and collaborator lists."""
    variants = artist_variants(raw_artist) or [raw_artist]
    # Artist names are short and unrelated ones share letters too easily, so the
    # partial-window heuristic is never applied here.
    base = _text_score(variants, candidate_artist, allow_partial=False)

    # "Elfman Danny" vs "Danny Elfman" and "A & B" vs "B & A" both reduce to the
    # same token set, so an exact token-set hit is treated as certainty.
    stream_tokens = artist_tokens(raw_artist)
    cand_tokens = artist_tokens(candidate_artist)
    if stream_tokens and cand_tokens:
        if stream_tokens == cand_tokens:
            return 1.0
        overlap = len(stream_tokens & cand_tokens) / len(stream_tokens | cand_tokens)
        base = max(base, overlap)

        # Same words, different order, as one string ("elfman danny"/"danny elfman").
        joined_stream = " ".join(sorted(norm_key(" ".join(stream_tokens)).split()))
        joined_cand = " ".join(sorted(norm_key(" ".join(cand_tokens)).split()))
        if joined_stream and joined_stream == joined_cand:
            return 1.0

    return base


def _duration_score(stream_duration: int | None, cand_duration: int | None) -> float | None:
    """None means 'no signal' so the weight can be redistributed."""
    if not stream_duration or not cand_duration or stream_duration <= 0 or cand_duration <= 0:
        return None
    delta = abs(stream_duration - cand_duration)
    if delta <= 2:
        return 1.0
    if delta <= 5:
        return 0.88
    if delta <= 10:
        return 0.65
    if delta <= 20:
        return 0.35
    if delta <= 45:
        return 0.12
    return 0.0


def score_candidate(raw_artist: str, raw_title: str, duration: int | None,
                    candidate: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    title_s = _text_score(title_variants(raw_title) or [raw_title], candidate.get("title", ""))
    artist_s = _artist_score(raw_artist, candidate.get("artist", ""))
    duration_s = _duration_score(duration, candidate.get("duration"))

    if duration_s is None:
        # Redistribute the duration weight proportionally across the text signals.
        total = W_TITLE + W_ARTIST
        score = (title_s * W_TITLE + artist_s * W_ARTIST) / total
    else:
        score = title_s * W_TITLE + artist_s * W_ARTIST + duration_s * W_DURATION

    detail = {
        "title": round(title_s, 4),
        "artist": round(artist_s, 4),
        "duration": None if duration_s is None else round(duration_s, 4),
        "penalties": [],
    }

    # Only meaningful when the stream actually named somebody: with no artist to
    # compare against, a low score says nothing about the candidate.
    if norm_key(raw_artist) and artist_s < ARTIST_MISMATCH_THRESHOLD:
        score -= ARTIST_MISMATCH_PENALTY
        detail["penalties"].append("different performer")

    # A tribute/karaoke cut only counts as an impostor if the *stream* was not
    # itself asking for one.
    stream_is_impostor = has_impostor_marker(raw_title, raw_artist)
    if not stream_is_impostor and has_impostor_marker(
        candidate.get("title", ""), candidate.get("artist", ""), candidate.get("album", "")
    ):
        score -= IMPOSTOR_PENALTY
        detail["penalties"].append("re-recording/karaoke")

    if "compilation" in (candidate.get("secondary_types") or []):
        score -= COMPILATION_PENALTY
        detail["penalties"].append("compilation")

    # Only penalise a live take when the stream was not itself announcing one.
    stream_blob = f"{raw_title} {raw_artist}".lower()
    stream_wants_live = any(m in stream_blob for m in LIVE_MARKERS)
    if not stream_wants_live:
        cand_blob = f"{candidate.get('title', '')} {candidate.get('album', '')}".lower()
        if "live" in (candidate.get("secondary_types") or []) or \
                any(m in cand_blob for m in LIVE_MARKERS):
            score -= LIVE_PENALTY
            detail["penalties"].append("live recording")

    if candidate.get("is_bootleg"):
        score -= BOOTLEG_PENALTY
        detail["penalties"].append("bootleg")

    score = max(0.0, min(1.0, score))
    detail["score"] = round(score, 4)
    return score, detail


# --- corroboration -----------------------------------------------------------

def _same_recording(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return (
        norm_key(a.get("title", "")) == norm_key(b.get("title", ""))
        and bool(artist_tokens(a.get("artist", "")) & artist_tokens(b.get("artist", "")))
    )


def _corroboration_bonus(sources: int) -> float:
    """Confidence credit for a recording found in `sources` databases at once."""
    if sources < 2:
        return 0.0
    return CORROBORATION_BONUS.get(sources, MAX_CORROBORATION_BONUS)


# Two recordings of the same length to within this are the same cut, not a
# different edit of it. Wider than the scorer's "identical" band because here we
# already know the artist and title agree.
MERGE_DURATION_TOLERANCE = 8


def _is_duplicate(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Whether two candidates are the same recording seen through two databases."""
    isrc_a, isrc_b = (a.get("isrc") or "").upper(), (b.get("isrc") or "").upper()
    if isrc_a and isrc_b:
        # The recording code is an identity assertion; nothing else can overrule it.
        return isrc_a == isrc_b
    if not _same_recording(a, b):
        return False
    dur_a, dur_b = a.get("duration"), b.get("duration")
    if dur_a and dur_b:
        return abs(dur_a - dur_b) <= MERGE_DURATION_TOLERANCE
    # No length on one side: title and artist agreeing is all the evidence there
    # is, and it is the same evidence the scorer ran on.
    return True


def _absorb(keep: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    """Fold `other` into `keep`, preferring whichever side actually has a field.

    MusicBrainz supplies the canonical identity (MBID, ISRC) and Spotify the
    playable one (URI, artwork). A merged candidate carries both, which is what
    lets a MusicBrainz identification reach a playlist without a second lookup.
    """
    merged = dict(keep)
    for field_ in ("uri", "url", "art_url", "isrc", "album"):
        if not merged.get(field_) and other.get(field_):
            merged[field_] = other[field_]
    for cand in (keep, other):
        if cand.get("source") == "musicbrainz" and cand.get("ext_id"):
            merged["mbid"] = cand["ext_id"]
        if cand.get("source") == "spotify" and cand.get("ext_id"):
            merged["spotify_id"] = cand["ext_id"]
    if merged.get("duration") is None and other.get("duration") is not None:
        merged["duration"] = other["duration"]
    return merged


def _sources_of(cand: dict[str, Any]) -> set[str]:
    """Which databases back this candidate — more than one once it is a merge."""
    recorded = (cand.get("score_detail") or {}).get("sources")
    return {s for s in (recorded or [cand.get("source", "")]) if s}


def merge_candidates(scored: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse duplicate recordings into one ranked row per recording.

    Both providers finding the same track used to produce two rows that were
    scored, ranked and shown separately - which spends two of the review list's
    slots on one answer and buries the genuine alternatives below them. Merging
    them into a single row does three jobs at once: the list shows distinct
    options, the merged row is playable *and* canonically identified, and
    independent agreement between two databases becomes the confidence boost it
    should always have been.

    Input must already carry `score`. Output is sorted best first.
    """
    merged: list[dict[str, Any]] = []
    for cand in sorted(scored, key=lambda c: c.get("score", 0.0), reverse=True):
        for i, existing in enumerate(merged):
            if not _is_duplicate(existing, cand):
                continue
            combined = _absorb(existing, cand)
            detail = dict(combined.get("score_detail") or {})
            # Union both sides: either may already be the result of an earlier
            # merge, and only the union knows how many databases have weighed in.
            detail["sources"] = sorted(_sources_of(existing) | _sources_of(cand))
            # The credit is a property of how many databases back the row, so it
            # is recomputed from the base score rather than added again: a third
            # database agreeing must lift the row from the two-source figure,
            # not stack a second full bonus on top of it. `_absorb` carries
            # `existing`'s score into `combined`, so `existing`'s recorded bonus
            # is exactly what is already baked in there.
            prior = float((existing.get("score_detail") or {}).get("corroboration_bonus") or 0.0)
            bonus = _corroboration_bonus(len(detail["sources"]))
            combined["score"] = min(1.0, combined.get("score", 0.0) - prior + bonus)
            detail["corroboration_bonus"] = bonus
            detail["corroborated"] = len(detail["sources"]) > 1
            # An ISRC match is a property of the recording, not of the row that
            # happened to win the merge, so it survives either way round.
            if any((c.get("score_detail") or {}).get("isrc_verified")
                   for c in (existing, cand)):
                detail["isrc_verified"] = True
            detail["score"] = round(combined["score"], 4)
            combined["score_detail"] = detail
            merged[i] = combined
            break
        else:
            merged.append(cand)

    # Rank by score, then prefer the row a playlist can actually use: of two
    # equally good answers the playable one is strictly the better suggestion.
    merged.sort(key=lambda c: (c.get("score", 0.0), bool(c.get("uri")),
                               c.get("native_score") or 0), reverse=True)
    return merged


def _english_list(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _no_results_reason(answered: list[str], failed: list[str],
                       errors: list[str]) -> str:
    """Why a search came back empty, in terms the review queue can act on.

    "Unmatched" covers two situations that call for opposite responses. If the
    databases answered and none of them held the recording, then either the
    metadata is too mangled to search on or the song was never released
    commercially - and archiving it is a defensible decision. If they never
    answered, nothing has been learned about the song at all and archiving it
    throws away a song that would have matched fine an hour later.

    Naming which of the two happened, and with how many catalogues behind it, is
    the entire point of this string.
    """
    parts: list[str] = []
    if answered:
        parts.append(f"Searched {_english_list(answered)}: no recording found "
                     "under this artist and title.")
    if failed:
        parts.append(f"{_english_list(failed)} could not be reached.")
    if not parts:
        parts.append("No results from any provider.")
    if errors:
        parts.append("(" + "; ".join(errors) + ")")
    return " ".join(parts)


async def _tiebreak_isrc(scored: list[dict[str, Any]]) -> str:
    """A recording code for the strongest candidate that can supply one.

    Deezer returns the ISRC in its search response, so most of the time this
    costs nothing at all. Only when no candidate carries one is MusicBrainz
    asked directly - a second request, and therefore a last resort rather than
    the opening move.

    Spotify candidates are deliberately excluded even though they can carry an
    ISRC, because the caller spends it on `spotify.search_isrc`. A Spotify track
    found by its own recording code is the same track a second time, and paying
    the corroboration of an exact cross-database hit for a round trip that
    learned nothing is how confidence gets inflated for free.
    """
    ranked = [c for c in sorted(scored, key=lambda c: c.get("score", 0.0), reverse=True)
              if c.get("source") != "spotify"]
    for cand in ranked:
        isrc = (cand.get("isrc") or "").strip().upper()
        if isrc:
            return isrc
    for cand in ranked:
        if cand.get("source") == "musicbrainz" and cand.get("ext_id"):
            return await musicbrainz.fetch_isrc(cand["ext_id"])
    return ""


async def resolve(raw_artist: str, raw_title: str, raw_album: str,
                  duration: int | None) -> MatchResult:
    """Resolve one piece of stream metadata to a real recording."""
    min_seconds = db.get_int("min_song_seconds", 45)
    auto_accept = db.get_float("auto_accept_score", 0.92)
    review_floor = db.get_float("review_floor_score", 0.62)

    key_artist = norm_key(raw_artist)
    key_title = norm_key(raw_title)

    # --- Tier 1: learned aliases (checked before the jingle filter so a user
    # can rescue something the heuristics wrongly flagged as imaging).
    alias = db.query_one(
        "SELECT * FROM aliases WHERE key_artist = ? AND key_title = ?",
        (key_artist, key_title),
    )
    # A rule saying "this really is music" carries no identity, so it does not
    # resolve anything - it only disarms the filter below. See ALIAS_IS_SONG.
    force_song = alias is not None and alias["kind"] == ALIAS_IS_SONG
    if alias is not None and not force_song:
        db.execute("UPDATE aliases SET hits = hits + 1 WHERE id = ?", (alias["id"],))
        if alias["kind"] == "nonsong":
            return MatchResult(status="nonsong", method="learned",
                               reason="Previously marked as not-a-song")
        return MatchResult(
            status="matched",
            confidence=1.0,
            method="learned",
            best={
                "source": "alias",
                "ext_id": alias["spotify_id"] or alias["mbid"] or "",
                "artist": alias["match_artist"] or raw_artist,
                "title": alias["match_title"] or raw_title,
                "album": alias["match_album"] or "",
                "duration": alias["duration"],
                "mbid": alias["mbid"],
                "isrc": alias["isrc"],
                "spotify_id": alias["spotify_id"],
                "uri": alias["spotify_uri"],
                "url": alias["spotify_url"],
                "art_url": alias["art_url"],
            },
        )

    # --- Tier 0: non-song filter, unless the user has overruled it here.
    if force_song:
        db.execute("UPDATE aliases SET hits = hits + 1 WHERE id = ?", (alias["id"],))
    else:
        reason = detect_nonsong(raw_artist, raw_title, raw_album, duration, min_seconds)
        if reason:
            return MatchResult(status="nonsong", method="filter", reason=reason)

    # --- Tier 2: provider search. Every enabled catalogue at once - they share
    # no resource, so the wait is the slowest one rather than the sum.
    active = providers.enabled()
    if not active:
        return MatchResult(status="unmatched", method="none",
                           reason="No match providers are enabled or configured.")

    gathered = await asyncio.gather(
        *(p.module.search(raw_artist, raw_title) for p in active),
        return_exceptions=True,
    )

    raw_candidates: list[dict[str, Any]] = []
    answered: list[str] = []
    failed: list[str] = []
    errors: list[str] = []
    for provider, result in zip(active, gathered):
        if isinstance(result, BaseException):
            failed.append(provider.label)
            errors.append(f"{provider.label}: {result}")
            continue
        answered.append(provider.label)
        raw_candidates.extend(result)

    if not raw_candidates:
        # A provider that answered "nothing" and a provider that never answered
        # are opposite outcomes, and only the first is evidence about the song.
        # Saying which is which is the difference between a queue item you can
        # archive with confidence and one you are guessing about, so the verdict
        # names both sides rather than dumping a list of exceptions.
        return MatchResult(
            status="unmatched", method="search",
            reason=_no_results_reason(answered, failed, errors),
            # Every provider we could ask was unreachable: an outage, not a
            # verdict, so the song stays queued instead of being written off.
            retryable=bool(failed) and not answered,
        )

    # --- Tier 3: scoring
    scored: list[dict[str, Any]] = []
    for cand in raw_candidates:
        score, detail = score_candidate(raw_artist, raw_title, duration, cand)
        enriched = dict(cand)
        enriched["score"] = score
        enriched["score_detail"] = detail
        scored.append(enriched)

    # --- Tier 4: corroboration. Merging folds the same recording found in
    # several databases into one row and credits the agreement, so `best` is
    # already the corroborated answer where there is one.
    unique = merge_candidates(scored)
    best = unique[0]
    method = ("corroborated" if best.get("score_detail", {}).get("corroborated")
              else f"search:{best['source']}")

    # ISRC tie-break: only spend the extra requests when the answer is genuinely
    # in doubt, and only when we can act on the result. Runs on the pre-merge
    # list because it needs each provider's own identifiers, which a merge may
    # have folded into a row belonging to a different source.
    if review_floor <= best["score"] < auto_accept and spotify.is_configured():
        isrc = await _tiebreak_isrc(scored)
        if isrc:
            verified: list[dict[str, Any]] = []
            for track in await spotify.search_isrc(isrc):
                track["isrc"] = isrc
                s, detail = score_candidate(raw_artist, raw_title, duration, track)
                # An exact ISRC hit is an identity assertion, not a guess.
                s = min(1.0, max(s, best["score"]) + ISRC_BONUS)
                detail["isrc_verified"] = True
                detail["score"] = round(s, 4)
                track["score"] = s
                track["score_detail"] = detail
                verified.append(track)
            if verified:
                # Re-merge rather than append: an ISRC hit is very often the
                # same recording as the row it is confirming, and `best` must
                # follow the merged row rather than keep pointing at the copy
                # that was folded into it.
                unique = merge_candidates(unique + verified)
                best = unique[0]
                if best.get("score_detail", {}).get("isrc_verified"):
                    method = "isrc"

    confidence = best["score"]
    if confidence >= auto_accept:
        status = "matched"
    elif confidence >= review_floor:
        status = "review"
    else:
        status = "unmatched"

    return MatchResult(
        status=status,
        confidence=confidence,
        method=method,
        best=best,
        candidates=unique[:MAX_CANDIDATES],
        reason="; ".join(errors),
    )


async def enrich_with_spotify(candidate: dict[str, Any], raw_artist: str,
                              raw_title: str, duration: int | None) -> dict[str, Any]:
    """Attach a Spotify track to a non-Spotify match so it can reach a playlist.

    A match identified by MusicBrainz, Deezer or Apple Music is a correct
    identification but not a playable one; Spotify playlists need a URI. When
    the candidate carries an ISRC - which Deezer supplies for free - the lookup
    is exact, so a song Spotify's own text search could not find still lands in
    the playlist.
    """
    if candidate.get("source") == "spotify" or candidate.get("uri"):
        return candidate
    if not spotify.is_configured():
        return candidate

    isrc = candidate.get("isrc") or ""
    try:
        tracks = await spotify.search_isrc(isrc) if isrc else []
        if not tracks:
            tracks = await spotify.search(
                candidate.get("artist") or raw_artist,
                candidate.get("title") or raw_title,
            )
    except spotify.SpotifyThrottled:
        # The identification is already correct; only the playable link is
        # missing. Keep the match and let the sync loop attach the URI once
        # Spotify is answering again, rather than discarding a good match or
        # re-running the whole resolve on every retry.
        return candidate
    if not tracks:
        return candidate

    best_track, best_score = None, 0.0
    for track in tracks:
        s, _ = score_candidate(
            candidate.get("artist") or raw_artist,
            candidate.get("title") or raw_title,
            candidate.get("duration") or duration,
            track,
        )
        if s > best_score:
            best_track, best_score = track, s

    # Only adopt a link we are confident actually refers to the same recording.
    if best_track and best_score >= 0.85:
        merged = dict(candidate)
        merged["spotify_id"] = best_track["ext_id"]
        merged["uri"] = best_track["uri"]
        merged["url"] = best_track["url"]
        merged["art_url"] = candidate.get("art_url") or best_track.get("art_url", "")
        merged["isrc"] = merged.get("isrc") or best_track.get("isrc", "")
        return merged
    return candidate
