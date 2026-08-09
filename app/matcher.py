"""The matching engine.

Resolution runs in tiers, cheapest and most certain first:

  0. Non-song filter      - station IDs and jingles are rejected outright.
  1. Learned alias        - anything the user has confirmed before resolves
                            instantly, offline, at full confidence. Radio
                            rotations repeat heavily, so this tier carries most
                            of the traffic once the queue has been worked once.
  2. Provider search      - MusicBrainz and Spotify queried concurrently across
                            plausible spellings of the artist and title.
  3. Scoring              - each candidate scored on artist, title and duration
                            agreement, with penalties for re-recordings.
  4. Corroboration        - when both providers independently land on the same
                            recording, confidence is raised; when they disagree
                            near the threshold, an ISRC cross-check breaks the tie.

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

from . import db
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

CORROBORATION_BONUS = 0.07
ISRC_BONUS = 0.12


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


def _best_per_source(scored: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for cand in scored:
        src = cand.get("source", "")
        if src not in best or cand["score"] > best[src]["score"]:
            best[src] = cand
    return best


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
    if alias is not None:
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

    # --- Tier 0: non-song filter
    reason = detect_nonsong(raw_artist, raw_title, raw_album, duration, min_seconds)
    if reason:
        return MatchResult(status="nonsong", method="filter", reason=reason)

    # --- Tier 2: provider search (concurrent)
    tasks = []
    if db.get_bool("use_musicbrainz", True):
        tasks.append(("musicbrainz", musicbrainz.search(raw_artist, raw_title)))
    if db.get_bool("use_spotify", True) and spotify.is_configured():
        tasks.append(("spotify", spotify.search(raw_artist, raw_title)))

    if not tasks:
        return MatchResult(status="unmatched", method="none",
                           reason="No match providers are enabled or configured.")

    gathered = await asyncio.gather(*(t[1] for t in tasks), return_exceptions=True)

    raw_candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    for (name, _), result in zip(tasks, gathered):
        if isinstance(result, BaseException):
            errors.append(f"{name}: {result}")
            continue
        raw_candidates.extend(result)

    if not raw_candidates:
        # If every provider we tried errored, this is an outage, not a verdict.
        all_failed = bool(errors) and len(errors) == len(tasks)
        note = "; ".join(errors) if errors else "No results from any provider."
        return MatchResult(status="unmatched", method="search", reason=note,
                           retryable=all_failed)

    # --- Tier 3: scoring
    scored: list[dict[str, Any]] = []
    for cand in raw_candidates:
        score, detail = score_candidate(raw_artist, raw_title, duration, cand)
        enriched = dict(cand)
        enriched["score"] = score
        enriched["score_detail"] = detail
        scored.append(enriched)

    scored.sort(key=lambda c: c["score"], reverse=True)
    best = scored[0]
    method = f"search:{best['source']}"

    # --- Tier 4: corroboration
    per_source = _best_per_source(scored)
    mb_best = per_source.get("musicbrainz")
    sp_best = per_source.get("spotify")

    if mb_best and sp_best and _same_recording(mb_best, sp_best):
        # Two independent databases agreeing is much stronger evidence than
        # either one scoring well alone.
        boost = CORROBORATION_BONUS
        for cand in (mb_best, sp_best):
            cand["score"] = min(1.0, cand["score"] + boost)
            cand["score_detail"]["corroborated"] = True
            cand["score_detail"]["score"] = round(cand["score"], 4)
        scored.sort(key=lambda c: c["score"], reverse=True)
        best = scored[0]
        method = "corroborated"

    # ISRC tie-break: only spend the extra requests when the answer is genuinely
    # in doubt, and only when we can act on the result.
    if (review_floor <= best["score"] < auto_accept and mb_best
            and mb_best.get("ext_id") and spotify.is_configured()):
        isrc = mb_best.get("isrc") or await musicbrainz.fetch_isrc(mb_best["ext_id"])
        if isrc:
            for track in await spotify.search_isrc(isrc):
                track["isrc"] = isrc
                s, detail = score_candidate(raw_artist, raw_title, duration, track)
                # An exact ISRC hit is an identity assertion, not a guess.
                s = min(1.0, max(s, best["score"]) + ISRC_BONUS)
                detail["isrc_verified"] = True
                detail["score"] = round(s, 4)
                track["score"] = s
                track["score_detail"] = detail
                scored.append(track)
            scored.sort(key=lambda c: c["score"], reverse=True)
            if scored[0].get("score_detail", {}).get("isrc_verified"):
                best = scored[0]
                method = "isrc"

    # Deduplicate for display while preserving rank.
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for cand in scored:
        key = (cand.get("source", ""), cand.get("ext_id", ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(cand)

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
        candidates=unique[:10],
        reason="; ".join(errors),
    )


async def enrich_with_spotify(candidate: dict[str, Any], raw_artist: str,
                              raw_title: str, duration: int | None) -> dict[str, Any]:
    """Attach a Spotify track to a non-Spotify match so it can reach a playlist.

    A MusicBrainz-only match is a correct identification but not a playable one;
    Spotify playlists need a URI.
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
