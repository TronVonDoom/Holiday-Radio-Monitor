/* Review queue — one card at a time, built to be worked through quickly.

   This is the screen a user spends real time in, so it is keyboard-first:
   ← / → move, 1–9 confirm that candidate, a archives, x marks imaging, e jumps
   to the search fields. The card details are cached and the neighbours warmed,
   which is what makes Next feel like a page turn rather than a round trip. */

import { api } from "../api.js";
import {
  artwork, confidenceCell, confirmDialog, emptyState, statusBadge, toast, withBusy,
} from "../ui.js";
import { $, $$, esc, fmtDur, num, paintIcons, sourceLabel } from "../util.js";
import { state } from "../state.js";

export const meta = {
  title: "Review queue",
  sub: "Confirm the matches the engine was not sure about",
};

/* The list shows a handful of strong options rather than everything the
   providers returned. A ranked list is only useful as far down as anyone
   actually reads it, and a dozen rows of near-misses is how the confident
   answer at the top gets second-guessed. The rest are already loaded, so
   revealing them is free. */
const TOP_CANDIDATES = 4;

const detailCache = new Map();

function songDetail(id) {
  if (!detailCache.has(id)) {
    // A rejected promise must not stay in the cache, or one transient failure
    // becomes a permanently broken card.
    detailCache.set(id, api(`/songs/${id}`).catch((err) => {
      detailCache.delete(id);
      throw err;
    }));
  }
  return detailCache.get(id);
}

function prefetchNeighbours() {
  const q = state.reviewQueue;
  if (q.length < 2) return;
  for (const offset of [1, -1]) {
    const song = q[(state.reviewIndex + offset + q.length) % q.length];
    if (song && !detailCache.has(song.id)) songDetail(song.id).catch(() => {});
  }
}

/* Move to another item in the queue. Both per-card view settings reset here so
   the next card opens in its default shape rather than inheriting the last. */
function goToCard(index) {
  state.reviewIndex = index;
  state.candidatesShown = TOP_CANDIDATES;
  state.searchProviders = null;
  return renderCard();
}

/* ------------------------------------------------------------- rendering -- */

export async function render(host) {
  // Entering the tab is the one moment a cached card can be out of date: the
  // matcher may have resolved something in the background since it was read.
  detailCache.clear();
  state.candidatesShown = TOP_CANDIDATES;
  state.searchProviders = null;

  // The archive is only a count here; the list itself is the Library filtered
  // to it, which already has search, sorting and paging.
  const [data, archived] = await Promise.all([
    api("/songs?status=review,unmatched&sort=plays&limit=100"),
    api("/songs?status=archived&limit=1"),
  ]);
  state.reviewQueue = data.items;
  state.archivedCount = archived.total;
  if (state.reviewIndex >= data.items.length) state.reviewIndex = 0;

  if (!data.items.length) {
    host.innerHTML = `<div class="card">${emptyState(
      "✅", "Review queue is clear",
      "Everything the matcher was unsure about has been resolved.",
      state.archivedCount
        ? `<button class="btn" data-act="open-archive">View ${num(state.archivedCount)} archived</button>`
        : `<a class="btn" href="#/library">Browse the library</a>`,
    )}</div>`;
    paintIcons(host);
    return;
  }
  await renderCard();
}

const signalMark = (v) => (v >= 0.9 ? "good" : v >= 0.6 ? "warn" : "bad");

function candidateRow(c, i, songId) {
  const d = c.score_detail || {};
  const sig = [];
  const pct = (v) => Math.round(v * 100);

  if (d.artist !== undefined) sig.push(`<span class="sig ${signalMark(d.artist)}">artist ${pct(d.artist)}%</span>`);
  if (d.title !== undefined) sig.push(`<span class="sig ${signalMark(d.title)}">title ${pct(d.title)}%</span>`);
  if (d.duration !== undefined && d.duration !== null) {
    sig.push(`<span class="sig ${signalMark(d.duration)}">length ${pct(d.duration)}%</span>`);
  } else {
    sig.push(`<span class="sig">no length</span>`);
  }
  // A merged row was found in several databases, so it names all of them — and
  // how many agreed is the point, because the confidence credit scales with it.
  const sources = d.sources?.length ? d.sources : [c.source];
  if (d.corroborated) sig.push(`<span class="sig good">${sources.length} databases agree</span>`);
  if (d.isrc_verified) sig.push(`<span class="sig good">ISRC verified</span>`);
  (d.penalties || []).forEach((p) => sig.push(`<span class="sig bad">${esc(p)}</span>`));
  sig.push(`<span class="sig">${esc(sources.map(sourceLabel).join(" + "))}</span>`);

  return `
    <div class="cand ${i === 0 ? "best" : ""}">
      ${artwork(c.art_url)}
      <div class="info">
        <b>${esc(c.title)}</b>
        <div class="line2">${esc(c.artist)}${c.album ? ` · ${esc(c.album)}` : ""} · ${fmtDur(c.duration)}</div>
        <div class="signals">${sig.join("")}</div>
      </div>
      <div class="act">
        ${confidenceCell(c.score)}
        ${c.url ? `<a class="btn sm" href="${esc(c.url)}" target="_blank" rel="noopener"
             title="Open on ${esc(sourceLabel(c.source))}"><i class="ic" data-icon="external"></i></a>` : ""}
        <button class="btn primary sm" data-act="confirm" data-cand="${c.id}" data-song="${songId}">
          Use this${i < 9 ? ` <span class="kbd">${i + 1}</span>` : ""}</button>
      </div>
    </div>`;
}

export async function renderCard(prefetched) {
  const host = $("#view-review");
  const song = state.reviewQueue[state.reviewIndex];
  if (!song) return render(host);

  const detail = prefetched || await songDetail(song.id);
  const s = detail.song;
  const cands = detail.candidates;
  const shown = Math.min(Math.max(state.candidatesShown, TOP_CANDIDATES), cands.length);
  const hidden = cands.length - shown;
  const position = state.reviewIndex + 1;
  const total = state.reviewQueue.length;

  const candHtml = cands.length
    ? cands.slice(0, shown).map((c, i) => candidateRow(c, i, s.id)).join("")
      + (hidden > 0
        ? `<button class="btn block sm" data-act="show-more" style="margin-top:.5rem">
             Show ${hidden} weaker match${hidden === 1 ? "" : "es"}</button>`
        : "")
    : `<div class="dim" style="padding:.25rem 0">No candidates were found. Correct the artist
        or title above and search again, archive it for later, or mark it as not-a-song.</div>`;

  // Which providers actually answered the last manual search. Without it a
  // provider sitting in a cooldown reads as a search that simply found less.
  const providerNote = state.searchProviders?.length
    ? `<p class="sub" style="margin-bottom:.75rem">${state.searchProviders.map((p) => p.ok
        ? `${esc(p.name)}: ${p.count} result${p.count === 1 ? "" : "s"}`
        : `<span style="color:var(--warn)">${esc(p.name)}: ${esc(p.detail || "unavailable")}</span>`
      ).join(" · ")}</p>`
    : "";

  host.innerHTML = `
    <div class="review-bar">
      <div class="review-progress">
        <div class="pos">Item ${num(position)} of ${num(total)}</div>
        <div class="track-bar"><i style="width:${(position / total) * 100}%"></i></div>
      </div>
      <div class="row tight center">
        ${state.archivedCount
          ? `<button class="btn sm" data-act="open-archive">
               <i class="ic" data-icon="archive"></i>Archived (${num(state.archivedCount)})</button>`
          : ""}
        <button class="btn sm" data-act="prev" title="Previous (←)">‹ Previous</button>
        <button class="btn sm" data-act="next" title="Next (→)">Next ›</button>
      </div>
    </div>

    <div class="heard">
      <div class="lbl">Heard on the stream</div>
      <h3>${esc(s.raw_title)}</h3>
      <div class="facts">
        <span><strong>${esc(s.raw_artist || "unknown artist")}</strong></span>
        <span>${fmtDur(s.duration)}</span>
        <span>played ${num(s.play_count)}×</span>
        <span>${detail.stations.map((st) => esc(st.name)).join(", ")}</span>
        ${statusBadge(s.status)}
      </div>
      ${s.nonsong_reason ? `<p class="hint" style="margin-top:.5rem">${esc(s.nonsong_reason)}</p>` : ""}
    </div>

    <div class="card">
      <div class="card-head">
        <div><h2>Search the catalogues</h2>
          <p class="sub">Prefilled with what the stream said. Correct either field and press
            Enter to search every enabled catalogue at once.</p></div>
      </div>
      <div class="row">
        <div class="field"><label for="ms-artist">Artist</label>
          <input type="text" id="ms-artist" value="${esc(s.raw_artist)}" autocomplete="off"></div>
        <div class="field"><label for="ms-title">Title</label>
          <input type="text" id="ms-title" value="${esc(s.raw_title)}" autocomplete="off"></div>
        <button class="btn primary" data-act="search" data-song="${s.id}">
          <i class="ic" data-icon="search"></i>Search</button>
      </div>
      <div class="row tight" style="margin-top:1rem">
        <button class="btn sm" data-act="rematch" data-song="${s.id}">
          <i class="ic" data-icon="wand"></i>Run auto-match again</button>
        <button class="btn sm" data-act="archive" data-song="${s.id}" title="Archive (a)">
          <i class="ic" data-icon="archive"></i>Archive for later</button>
        <button class="btn danger sm" data-act="nonsong" data-song="${s.id}" title="Not a song (x)">
          Not a song (jingle / ID)</button>
      </div>
    </div>

    <div class="card" id="rv-matches">
      <div class="card-head">
        <div><h2>Suggested matches</h2>
          <p class="sub">Ranked by artist, title and track-length agreement. Confirming also
            teaches the matcher, so this track resolves instantly next time.</p></div>
      </div>
      ${providerNote}
      ${candHtml}
    </div>`;

  paintIcons(host);
  prefetchNeighbours();
}

/* A search rewrites the card from the top, which would leave its own results
   off screen — so bring them back into view. */
const showMatches = () =>
  $("#rv-matches")?.scrollIntoView({ behavior: "smooth", block: "start" });

/* Both search endpoints return the whole refreshed card, so the result is shown
   from the response the search already produced rather than by asking for the
   same song again. */
async function showSearchResult(songId, detail) {
  detailCache.set(songId, Promise.resolve(detail));
  state.searchProviders = detail.providers || null;
  state.candidatesShown = TOP_CANDIDATES;
  await renderCard(detail);
  showMatches();
}

/* Drop the item just decided on and land on whatever takes its place — the
   queue closes up behind it, so that is the next item without any navigation. */
function removeCurrentCard(songId) {
  detailCache.delete(songId);
  state.reviewQueue.splice(state.reviewIndex, 1);
  if (!state.reviewQueue.length) return render($("#view-review"));
  return goToCard(Math.min(state.reviewIndex, state.reviewQueue.length - 1));
}

export async function runSearch(button, songId) {
  const artist = $("#ms-artist").value;
  const title = $("#ms-title").value;
  try {
    await withBusy(button, "Searching…", async () => {
      const detail = await api(`/songs/${songId}/search`, {
        method: "POST", body: { artist, title },
      });
      await showSearchResult(songId, detail);
    });
  } catch (e) {
    toast(e.message, "bad");
  }
}

/* --------------------------------------------------------------- actions -- */

export const actions = {
  prev: () => goToCard(Math.max(0, state.reviewIndex - 1)),
  next: () => goToCard((state.reviewIndex + 1) % state.reviewQueue.length),

  // Everything below the cut is already loaded, so this is a re-render, not a
  // fetch.
  "show-more": () => {
    state.candidatesShown = Infinity;
    return renderCard();
  },

  // A real navigation, so the router reads the filter out of the hash and Back
  // returns to the queue.
  "open-archive": () => { location.hash = "#/library?status=archived"; },

  search: (el, d) => runSearch(el, +d.song),

  async confirm(el, d) {
    el.disabled = true;
    try {
      await api(`/songs/${d.song}/confirm`, {
        method: "POST", body: { candidate_id: +d.cand },
      });
      toast("Confirmed and remembered", "ok");
      await removeCurrentCard(+d.song);
    } catch (e) {
      toast(e.message, "bad");
      el.disabled = false;
    }
  },

  async archive(el, d) {
    try {
      await api(`/songs/${d.song}/archive`, { method: "POST" });
      state.archivedCount += 1;
      toast("Archived — restore it any time from the Library", "ok");
      await removeCurrentCard(+d.song);
    } catch (e) { toast(e.message, "bad"); }
  },

  async nonsong(el, d) {
    const ok = await confirmDialog({
      title: "Mark as station imaging?",
      body: "It leaves the queue and never reaches a playlist. A rule is written so the "
          + "same metadata is filtered automatically in future — reversible from "
          + "<strong>Library → nonsong</strong>.",
      confirmLabel: "Mark as imaging", danger: true,
    });
    if (!ok) return;
    try {
      await api(`/songs/${d.song}/nonsong?remember=true`, { method: "POST" });
      toast("Marked as station imaging", "ok");
      await removeCurrentCard(+d.song);
    } catch (e) { toast(e.message, "bad"); }
  },

  async rematch(el, d) {
    try {
      await withBusy(el, "Matching…", async () => {
        // Both endpoints answer with the refreshed card, so re-rendering from
        // the response costs no second round trip.
        await showSearchResult(+d.song, await api(`/songs/${d.song}/rematch`, { method: "POST" }));
      });
    } catch (e) { toast(e.message, "bad"); }
  },
};

/* ------------------------------------------------------------- shortcuts -- */

export const shortcuts = [
  ["← / →", "Previous / next item"],
  ["1 – 9", "Confirm that candidate"],
  ["a", "Archive for later"],
  ["x", "Mark as station imaging"],
  ["e", "Edit the search fields"],
  ["Enter", "Search (from a search field)"],
];

/** Handled only while the Review tab is showing, and never while typing. */
export function onKey(ev) {
  if (!state.reviewQueue.length) return false;
  const key = ev.key;

  if (key === "ArrowLeft")  { actions.prev(); return true; }
  if (key === "ArrowRight") { actions.next(); return true; }
  if (key === "e") { $("#ms-artist")?.focus(); $("#ms-artist")?.select(); return true; }

  const song = state.reviewQueue[state.reviewIndex];
  if (!song) return false;

  if (key === "a") { actions.archive(null, { song: song.id }); return true; }
  if (key === "x") { actions.nonsong(null, { song: song.id }); return true; }

  if (/^[1-9]$/.test(key)) {
    const button = $$('#rv-matches [data-act="confirm"]')[+key - 1];
    if (button) { button.click(); return true; }
  }
  return false;
}
