"""Text normalization, query expansion and non-song detection.

Everything here is driven by real metadata observed on the Halloween Radio
network, e.g.:

    artist="Elfman Danny"      title="Tales from the Crypt (Main Title)"
    artist="JINGLE"            title="Halloweenradio.net 20-1"      (16s)
    artist="Disney Sing-Along" title="Poor unfortunate souls ~ The Little Mermaid"
    artist="Sheb Wooley"       title="The purple people eater"
    album ="www.halloweenradio.net"

Three jobs:
  1. Produce a stable comparison key (`norm_key`) that ignores case, accents,
     punctuation and decoration.
  2. Expand messy metadata into a small set of plausible *query variants*, so a
     reversed name or a decorated title still finds the right record.
  3. Recognise station IDs and jingles so they never reach a playlist.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

# --- non-song detection ------------------------------------------------------

# Artist fields that are never a real performer.
NONSONG_ARTISTS = {
    "jingle", "jingles", "id", "ids", "station id", "stationid", "promo", "promos",
    "sweeper", "sweepers", "liner", "liners", "bumper", "advert", "advertisement",
    "commercial", "commercials", "spot", "imaging", "voiceover", "vo", "intro",
    "outro", "unknown", "n/a", "na", "none", "-", "various", "radio",
}

# Substrings that mark a title as station imaging rather than music.
NONSONG_TITLE_MARKERS = (
    "halloweenradio.net", "halloweenradio", "christmasradio", "radio.net",
    "station id", "stationid", "jingle", "sweeper", "commercial break",
    "advertisement", "you are listening to", "www.",
)

# "20-1", "ID 04", "SW 12" style imaging filenames.
IMAGING_PATTERNS = (
    re.compile(r"^\s*\d+\s*[-_]\s*\d+\s*$"),
    re.compile(r"^\s*(id|sw|jng|jingle|promo|liner)\s*[-_ ]?\s*\d+\s*$", re.I),
)

# Album values that are watermarks, not albums.
JUNK_ALBUM_PATTERNS = (
    re.compile(r"^\s*www\.", re.I),
    re.compile(r"radio\.net\s*$", re.I),
    re.compile(r"^\s*(unknown|various|va|n/?a|none|-)\s*$", re.I),
    re.compile(r"^\s*https?://", re.I),
)

# Candidate results carrying these are re-recordings masquerading as the original.
# NOTE: plain "instrumental" is deliberately NOT here — Halloween Radio runs a
# dedicated Instrumental station where those are the legitimate versions.
IMPOSTOR_MARKERS = (
    "karaoke", "tribute", "made famous by", "in the style of",
    "as made popular by", "originally performed by", "cover version",
    "backing track", "sound-a-like", "soundalike", "workout mix", "8-bit",
    "lullaby version", "made popular by",
)

# Decoration stripped from titles when building a looser query variant.
DECORATION_RE = re.compile(
    r"""\s*[\(\[]\s*(
        remaster(ed)?(\s*\d{4})? | \d{4}\s*remaster(ed)? |
        single\s*version | album\s*version | radio\s*(edit|version|mix) |
        original\s*(mix|version|recording) | mono | stereo | explicit | clean |
        bonus\s*track | deluxe | re-?recorded(\s*version)? | digitally\s*remastered
    )\s*[\)\]]""",
    re.I | re.X,
)

FEAT_RE = re.compile(
    r"\s*[\(\[]?\s*\b(feat|ft|featuring|with)\b\.?\s+[^\)\]]*[\)\]]?\s*$", re.I
)

# "Poor unfortunate souls ~ The Little Mermaid" -> source after the tilde.
TILDE_SPLIT_RE = re.compile(r"\s*[~｜|]\s*")

PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
SPACE_RE = re.compile(r"\s+")

ARTIST_SPLIT_RE = re.compile(r"\s*(?:&|,|/|\+|\band\b|\bwith\b|\bfeat\.?\b|\bft\.?\b|\bx\b)\s*", re.I)


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def norm_key(text: str) -> str:
    """Aggressive comparison key: accent-free, punctuation-free, lowercase."""
    if not text:
        return ""
    text = strip_accents(text).lower()
    text = text.replace("&", " and ")
    text = PUNCT_RE.sub(" ", text)
    text = SPACE_RE.sub(" ", text).strip()
    # Leading articles carry no identifying information and are inconsistently
    # present in stream metadata ("purple people eater" vs "the purple people eater").
    for article in ("the ", "a ", "an "):
        if text.startswith(article):
            text = text[len(article):]
            break
    return text


def is_junk_album(album: str) -> bool:
    if not album or not album.strip():
        return True
    return any(p.search(album) for p in JUNK_ALBUM_PATTERNS)


def clean_title(raw: str) -> str:
    """Human-facing title with obvious decoration removed."""
    title = (raw or "").strip()
    title = DECORATION_RE.sub("", title)
    title = re.sub(r"\s*-\s*(remaster(ed)?(\s*\d{4})?|single version|radio edit)\s*$", "", title, flags=re.I)
    return SPACE_RE.sub(" ", title).strip(" -–—")


def title_variants(raw: str) -> list[str]:
    """Plausible search forms for a title, best guess first."""
    base = clean_title(raw)
    out: list[str] = []

    def add(value: str) -> None:
        value = SPACE_RE.sub(" ", (value or "").strip(" -–—~|"))
        if value and value.lower() not in {v.lower() for v in out}:
            out.append(value)

    add(base)
    # "Song ~ Source" -> the part before the separator is the real title.
    if TILDE_SPLIT_RE.search(base):
        add(TILDE_SPLIT_RE.split(base)[0])
    add(FEAT_RE.sub("", base))
    # Drop any remaining parenthetical, e.g. "(Main Title)".
    stripped = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*", " ", base)
    add(stripped)
    return out[:4]


def _looks_like_person(tokens: list[str]) -> bool:
    return len(tokens) == 2 and all(t[:1].isalpha() for t in tokens)


def clean_artist(raw: str) -> str:
    artist = (raw or "").strip()
    artist = FEAT_RE.sub("", artist)
    return SPACE_RE.sub(" ", artist).strip(" ,-")


def artist_variants(raw: str) -> list[str]:
    """Plausible search forms for an artist, best guess first.

    Handles the network's habit of storing performers surname-first
    ("Elfman Danny"), plus the classic "Lastname, Firstname" form. Because we
    cannot tell which order is correct from the string alone, we emit both and
    let the scorer decide which one the database agrees with.
    """
    base = clean_artist(raw)
    out: list[str] = []

    def add(value: str) -> None:
        value = SPACE_RE.sub(" ", (value or "").strip(" ,-"))
        if value and value.lower() not in {v.lower() for v in out}:
            out.append(value)

    add(base)

    # "Elfman, Danny" -> "Danny Elfman"
    if "," in base and len(base.split(",")) == 2:
        last, first = (p.strip() for p in base.split(","))
        if last and first:
            add(f"{first} {last}")

    # "Elfman Danny" -> "Danny Elfman". Only for a bare two-token name with no
    # collaboration separators, otherwise we would mangle real band names.
    tokens = base.split()
    if _looks_like_person(tokens) and not ARTIST_SPLIT_RE.search(base):
        add(f"{tokens[1]} {tokens[0]}")

    # Primary performer only, for "A & B" / "A feat. B" collaborations.
    if ARTIST_SPLIT_RE.search(base):
        add(ARTIST_SPLIT_RE.split(base)[0])

    return out[:4]


def artist_tokens(raw: str) -> set[str]:
    """Set of individual performer names, for order-insensitive comparison."""
    parts = [norm_key(p) for p in ARTIST_SPLIT_RE.split(clean_artist(raw))]
    return {p for p in parts if p}


def has_impostor_marker(*fields: str) -> bool:
    blob = " ".join(f.lower() for f in fields if f)
    return any(marker in blob for marker in IMPOSTOR_MARKERS)


def detect_nonsong(artist: str, title: str, album: str, duration: int | None,
                   min_seconds: int) -> str | None:
    """Return a human-readable reason if this metadata is not a real song."""
    a_norm = norm_key(artist)
    a_raw = (artist or "").strip().lower()
    t_raw = (title or "").strip().lower()

    if not t_raw and not a_raw:
        return "Empty metadata"
    if a_raw in NONSONG_ARTISTS or a_norm in NONSONG_ARTISTS:
        return f"Artist field is station imaging ({artist!r})"
    if any(marker in t_raw for marker in NONSONG_TITLE_MARKERS):
        return "Title contains station branding"
    if any(p.match(title or "") for p in IMAGING_PATTERNS):
        return "Title looks like an imaging filename"
    if duration is not None and 0 < duration < min_seconds:
        return f"Too short to be a song ({duration}s < {min_seconds}s)"
    if not t_raw:
        return "Missing title"
    return None


def fingerprint(artist: str, title: str, external_id: str = "") -> str:
    """Stable identity for a piece of stream metadata.

    Prefers the provider's own hash when available, otherwise derives one from
    the normalized artist/title so the same song reported with slightly
    different casing collapses to a single row.
    """
    if external_id:
        return f"ext:{external_id}"
    key = f"{norm_key(artist)}\x1f{norm_key(title)}"
    return "key:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]


def titlecase_display(text: str) -> str:
    """Tidy up shouty or all-lowercase metadata for display only."""
    stripped = (text or "").strip()
    if not stripped:
        return ""
    letters = [c for c in stripped if c.isalpha()]
    if not letters:
        return stripped
    upper_ratio = sum(c.isupper() for c in letters) / len(letters)
    if upper_ratio > 0.85 or upper_ratio <= 0.10:
        small = {"a", "an", "the", "of", "in", "on", "at", "to", "for", "and",
                 "or", "but", "with", "from", "by", "vs"}
        words = stripped.split()
        out = []
        for i, w in enumerate(words):
            lw = w.lower()
            out.append(lw if (0 < i < len(words) - 1 and lw in small) else lw.capitalize())
        return " ".join(out)
    return stripped
