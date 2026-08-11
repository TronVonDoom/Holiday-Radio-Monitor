/* Formatting, escaping and icons — the pieces every view needs and none of
   them owns. Nothing here touches the network or the DOM outside of painting
   icons, so it stays safe to import from anywhere. */

export const $  = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

/** Escape a value for interpolation into a template-literal HTML string. */
export const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESCAPES[c]);

export const num = (v) => Number(v || 0).toLocaleString();

export function fmtTime(ts) {
  if (!ts) return "—";
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** Absolute timestamp, for the title attribute behind a relative one. */
export const fmtStamp = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "");

export const fmtDur = (s) =>
  !s || s < 0 ? "—" : `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;

/* A cooldown can run from seconds to hours, and "62373s" is not a length anyone
   reads as most of a day. */
export function fmtWait(seconds) {
  const s = Math.ceil(seconds || 0);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  const h = Math.floor(s / 3600);
  const m = Math.round((s % 3600) / 60);
  return m ? `${h}h ${m}m` : `${h}h`;
}

export const plural = (n, word, suffix = "s") => `${num(n)} ${word}${n === 1 ? "" : suffix}`;

export function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

/* How a candidate's `source` key is spelled for a reader. The registry itself
   lives on the server; this is only presentation, and an unknown key falls
   through to itself rather than rendering as blank. */
const SOURCE_LABELS = {
  musicbrainz: "MusicBrainz", spotify: "Spotify", deezer: "Deezer",
  itunes: "Apple Music", alias: "learned", manual: "manual",
};
export const sourceLabel = (key) => SOURCE_LABELS[key] || key;

/* Statuses as a person would write them, casing included. Doing it here rather
   than with `text-transform: capitalize` is what keeps "not a song" from being
   rendered as the title-cased "Not A Song". */
const STATUS_LABELS = {
  matched: "Matched", confirmed: "Confirmed", review: "In review",
  unmatched: "No match", pending: "Pending", archived: "Archived",
  nonsong: "Not a song",
};
export const statusLabel = (status) => STATUS_LABELS[status] || status;

/* ---------------------------------------------------------------- icons -- */

/* Inline so the UI needs no icon font and no second request. All drawn on the
   same 24×24 grid with the same stroke, which is what keeps them looking like
   one set rather than four sets. */
const ICONS = {
  grid:    '<path d="M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z"/>',
  check:   '<path d="M20 6L9 17l-5-5"/>',
  disc:    '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.5"/>',
  list:    '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  radio:   '<circle cx="12" cy="12" r="2"/><path d="M4.9 19.1a10 10 0 0 1 0-14.2M19.1 4.9a10 10 0 0 1 0 14.2M7.8 16.2a6 6 0 0 1 0-8.4M16.2 7.8a6 6 0 0 1 0 8.4"/>',
  gear:    '<circle cx="12" cy="12" r="3"/><path d="M19.6 14.5a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-2.87 1.2V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-2.94-1.14l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.7 1.7 0 0 0 3.1 14H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.14-2.94l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.7 1.7 0 0 0 10 3.1V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 2.94 1.14l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.7 1.7 0 0 0 20.9 10H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.3.5z"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.6-6.4L21 8"/><path d="M21 3v5h-5"/>',
  upload:  '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/>',
  music:   '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
  menu:    '<path d="M3 6h18M3 12h18M3 18h18"/>',
  panel:   '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
  moon:    '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
  sun:     '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  auto:    '<rect x="2" y="4" width="20" height="14" rx="2"/><path d="M8 21h8M12 18v3"/>',
  search:  '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
  x:       '<path d="M18 6L6 18M6 6l12 12"/>',
  external:'<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><path d="M15 3h6v6M10 14L21 3"/>',
  trash:   '<path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>',
  plus:    '<path d="M12 5v14M5 12h14"/>',
  alert:   '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>',
  archive: '<rect x="2" y="4" width="20" height="5" rx="1"/><path d="M4 9v10a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9M10 13h4"/>',
  wand:    '<path d="M15 4V2M15 16v-2M8 9h2M20 9h2M17.8 11.8l1.4 1.4M17.8 6.2l1.4-1.4M3 21l9-9"/>',
  bolt:    '<path d="M13 2L3 14h9l-1 8 10-12h-9z"/>',
  clock:   '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  link:    '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
  filter:  '<path d="M22 3H2l8 9.5V19l4 2v-8.5z"/>',
};

export const icon = (name, size = 16) =>
  `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor"
        stroke-width="1.85" stroke-linecap="round" stroke-linejoin="round"
        aria-hidden="true" focusable="false">${ICONS[name] || ""}</svg>`;

/** Fill every `<i class="ic" data-icon="…">` that has not been filled yet. */
export function paintIcons(root = document) {
  $$("i[data-icon]", root).forEach((el) => {
    if (el.dataset.painted === el.dataset.icon) return;
    el.innerHTML = icon(el.dataset.icon, +el.dataset.size || 16);
    el.dataset.painted = el.dataset.icon;
  });
}
