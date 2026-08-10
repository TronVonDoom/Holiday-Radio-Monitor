/* Holiday Radio Matcher — UI.
   Plain ES modules, no build step: what ships in the image is what runs. */

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/* ---------- tiny helpers ---------- */

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const fmtTime = (ts) => {
  if (!ts) return "—";
  const d = new Date(ts * 1000), diff = (Date.now() / 1000) - ts;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
};

const fmtDur = (s) => (!s || s < 0) ? "—"
  : `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;

/* A cooldown can run from seconds to hours, and "62373s" is not a length anyone
   reads as most of a day. */
const fmtWait = (seconds) => {
  const s = Math.ceil(seconds || 0);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  const h = Math.floor(s / 3600);
  const m = Math.round((s % 3600) / 60);
  return m ? `${h}h ${m}m` : `${h}h`;
};

/* Providers currently refusing calls, newest roster first. /api/stats carries
   the whole list with its labels, so nothing here needs to know how many
   catalogues there are or what any of them is called — a provider added
   server-side shows up, and can be resumed, without touching this file. */
const coldProviders = (stats) => (stats?.providers || []).filter((p) => p.throttled);

/* How a candidate's `source` key is spelled for a reader. The registry itself
   lives on the server; this is only presentation, and an unknown key falls
   through to itself rather than rendering as blank. */
const SOURCE_LABELS = {
  musicbrainz: "MusicBrainz", spotify: "Spotify", deezer: "Deezer",
  itunes: "Apple Music", alias: "learned",
};
const sourceLabel = (key) => SOURCE_LABELS[key] || key;

const ICONS = {
  grid:   '<path d="M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z"/>',
  check:  '<path d="M20 6L9 17l-5-5"/>',
  disc:   '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.5"/>',
  list:   '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  radio:  '<circle cx="12" cy="12" r="2"/><path d="M4.9 19.1a10 10 0 0 1 0-14.2M19.1 4.9a10 10 0 0 1 0 14.2M7.8 16.2a6 6 0 0 1 0-8.4M16.2 7.8a6 6 0 0 1 0 8.4"/>',
  gear:   '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2V21a2 2 0 1 1-4 0v-.1A1.7 1.7 0 0 0 7 19.4a1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.7 1.7 0 0 0 3 15a1.7 1.7 0 0 0-1.5-1H1a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 3 9a1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 9 3V3a2 2 0 1 1 4 0v.1A1.7 1.7 0 0 0 15 4.6a1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1A1.7 1.7 0 0 0 21 9v0a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  refresh:'<path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/>',
  upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/>',
  music:  '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
};

const icon = (name, size = 16) =>
  `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none"
        stroke="currentColor" stroke-width="1.9" stroke-linecap="round"
        stroke-linejoin="round">${ICONS[name] || ""}</svg>`;

function paintIcons(root = document) {
  $$("i[data-i]", root).forEach((el) => {
    if (!el.dataset.painted) { el.innerHTML = icon(el.dataset.i); el.dataset.painted = "1"; }
  });
}

function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  el.title = "Click to dismiss";
  el.addEventListener("click", () => el.remove());
  $("#toasts").append(el);
  // Failures usually carry an explanation worth reading, so they linger.
  const life = kind === "bad" ? 12000 : 3800;
  setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 250); }, life);
}

/* ---------- api ---------- */

async function api(path, options = {}) {
  const res = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* non-JSON error */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

/* ---------- shared fragments ---------- */

function placeholderEl(cls = "") {
  const el = document.createElement("div");
  el.className = `ph ${cls}`.trim();
  el.innerHTML = icon("music", 18);
  return el;
}

// No inline onerror here: the fallback markup is an SVG full of double quotes,
// which would terminate the attribute and leak the remainder as visible text.
const artwork = (url, cls = "") => url
  ? `<img class="art ${esc(cls)}" src="${esc(url)}" alt="" loading="lazy">`
  : `<div class="ph ${esc(cls)}">${icon("music", 18)}</div>`;

// Image load failures do not bubble, so this listens in the capture phase.
document.addEventListener("error", (ev) => {
  const el = ev.target;
  if (el instanceof HTMLImageElement && el.classList.contains("art")) {
    el.replaceWith(placeholderEl([...el.classList].filter((c) => c !== "art").join(" ")));
  }
}, true);

const statusBadge = (status) => `<span class="badge ${esc(status)}">${esc(status)}</span>`;

function confidenceCell(value) {
  const pct = Math.round((value || 0) * 100);
  const tier = pct >= 92 ? "high" : pct >= 62 ? "mid" : "low";
  return `<span class="conf ${tier}"><span class="meter"><i style="width:${pct}%"></i></span>${pct}%</span>`;
}

const emptyState = (glyph, title, note = "") =>
  `<div class="empty"><div class="big">${glyph}</div><div><strong>${esc(title)}</strong></div>
   ${note ? `<div class="muted" style="margin-top:.35rem">${esc(note)}</div>` : ""}</div>`;

/* ---------- pagination ---------- */

const PAGE = { library: 25, stations: 10, playlist: 10 };

const span = (from, to) => Array.from({ length: to - from + 1 }, (_, i) => from + i);

// Exactly seven slots once there is more than one screenful, so the control
// never changes width as you page through — that width change is what makes a
// paginated table feel like it is shifting under you.
function pageWindow(current, pages) {
  if (pages <= 7) return span(1, pages);
  if (current <= 4) return [...span(1, 5), "…", pages];
  if (current >= pages - 3) return [1, "…", ...span(pages - 4, pages)];
  return [1, "…", current - 1, current, current + 1, "…", pages];
}

/* Renders a stable pagination footer. `key` namespaces the click targets so
   several pagers can coexist on one view. */
function pager(key, { total, page, size, unit = "item" }) {
  const pages = Math.max(1, Math.ceil(total / size));
  const current = Math.min(Math.max(1, page), pages);
  const from = total ? (current - 1) * size + 1 : 0;
  const to = Math.min(current * size, total);

  const btn = (label, target, { on = false, off = false, aria = "" } = {}) =>
    `<button class="pg${on ? " on" : ""}" data-page="${esc(key)}:${target}"
       ${off ? "disabled" : ""} ${aria ? `aria-label="${esc(aria)}"` : ""}>${label}</button>`;

  const numbers = pages > 1
    ? pageWindow(current, pages).map((p) =>
        p === "…" ? `<span class="gap">…</span>` : btn(p, p, { on: p === current })).join("")
    : "";

  return `
    <div class="pager">
      <span class="range">${total
        ? `${from.toLocaleString()}–${to.toLocaleString()} of ${total.toLocaleString()} ${esc(unit)}${total === 1 ? "" : "s"}`
        : `No ${esc(unit)}s`}</span>
      <div class="pg-group">
        ${btn("←", current - 1, { off: current <= 1, aria: "Previous page" })}
        ${numbers}
        ${btn("→", current + 1, { off: current >= pages, aria: "Next page" })}
      </div>
    </div>`;
}

/* Blank rows that hold a short final page at the height of a full one. Only
   used once a table actually spans more than one page — a small table should
   still size itself naturally. */
const fillerRows = (shown, size, total, cols) =>
  total > size && shown < size
    ? Array.from({ length: size - shown },
        () => `<tr class="filler"><td colspan="${cols}"></td></tr>`).join("")
    : "";

/* ---------- state ---------- */

const state = {
  view: "dashboard",
  stats: null,
  reviewIndex: 0,
  reviewQueue: [],
  candidatesShown: 0,     // grows when "show more" is used; reset per card
  searchProviders: null,  // per-provider report from the last manual search
  archivedCount: 0,
  library: { status: "", q: "", sort: "recent", page: 1 },
  stationsPage: 1,
  playlistPages: {},   // station id -> page number
  timer: null,
};

/* ---------- dashboard ---------- */

async function renderDashboard() {
  const [stats, np, recent, events] = await Promise.all([
    api("/stats"), api("/nowplaying"), api("/recent?limit=25"), api("/events?limit=25"),
  ]);
  state.stats = stats;
  applyStats(stats);

  const c = stats.counts;
  const resolved = c.matched + c.confirmed;
  const rate = Math.round(stats.match_rate * 100);
  // Any provider can be in a cooldown; name whichever are. The roster comes
  // from /api/stats rather than from a list held here, so adding a catalogue
  // server-side does not silently leave the dashboard unable to name it.
  const cold = coldProviders(stats);

  const tiles = [
    { label: "Matched", value: resolved, foot: `${rate}% of resolved songs`, bar: rate },
    { label: "Needs review", value: c.review + c.unmatched, foot: "waiting on you" },
    // A provider cooldown is the one thing that makes a busy-looking queue stop
    // draining, so it outranks "matching now…" here — and it comes with the way
    // out, because a cooldown is only a prediction about when the service will
    // answer again and you may know better than it does.
    { label: "In queue", value: c.pending,
      foot: cold.length
        ? cold.map((p) => `${p.label} paused ${fmtWait(p.cooldown_seconds)}`).join(" · ")
        : stats.worker.matching ? "matching now…" : "idle",
      extra: cold.map((p) =>
        `<button class="btn ghost sm" data-resume="${esc(p.key)}">Resume ${esc(p.label)} now</button>`).join("") },
    { label: "Playlist tracks", value: stats.playlist_entries, foot: `${stats.stations} station(s)` },
    { label: "Learned rules", value: stats.aliases, foot: "auto-match forever" },
    { label: "Filtered", value: c.nonsong, foot: "jingles & station IDs" },
  ].map((t) => `
    <div class="card stat">
      <div class="label">${t.label}</div>
      <div class="value">${t.value.toLocaleString()}</div>
      <div class="foot">${esc(t.foot)}</div>
      ${t.extra ? `<div class="acts">${t.extra}</div>` : ""}
      ${t.bar !== undefined ? `<div class="bar"><i style="width:${t.bar}%"></i></div>` : ""}
    </div>`).join("");

  const nowCards = np.length ? np.map((s) => {
    const offline = !s.enabled || s.last_error;
    const label = s.last_error ? "offline" : (s.enabled ? "on air" : "paused");
    return `
      <div class="np ${offline ? "offline" : ""}" data-holiday="${esc(s.holiday)}">
        ${artwork(s.art_url)}
        <div class="meta">
          <div class="st">${esc(s.station)} · ${label}</div>
          <b>${esc(s.raw_title || "—")}</b>
          <small>${esc(s.raw_artist || (s.last_error ? s.last_error.slice(0, 60) : "waiting for metadata"))}</small>
        </div>
        ${s.status ? statusBadge(s.status) : ""}
      </div>`;
  }).join("") : emptyState("📻", "No stations yet", "Add one from the Stations tab.");

  const recentRows = recent.map((r) => `
    <tr>
      <td><div class="track">${artwork(r.art_url)}
        <div class="t"><b>${esc(r.raw_title)}</b><span>${esc(r.raw_artist)}</span></div></div></td>
      <td class="muted">${esc(r.station)}</td>
      <td>${statusBadge(r.status)}</td>
      <td class="num muted">${fmtTime(r.played_at)}</td>
    </tr>`).join("");

  $("#view-dashboard").innerHTML = `
    <div class="grid stats" style="margin-bottom:1rem">${tiles}</div>
    <div class="grid two">
      <div class="grid" style="gap:1rem">
        <div class="card">
          <h2>On air now</h2><p class="sub">Latest track seen on each monitored station</p>
          <div class="np-grid">${nowCards}</div>
        </div>
        <div class="card">
          <h2>Recent plays</h2><p class="sub">Newest first, across all stations</p>
          <div class="table-wrap feed"><table>
            <thead><tr><th>Track</th><th>Station</th><th>Status</th><th class="num">Played</th></tr></thead>
            <tbody>${recentRows || `<tr><td colspan="4" class="muted">Nothing yet — the poller runs every ${state.stats ? "minute" : "45s"}.</td></tr>`}</tbody>
          </table></div>
        </div>
      </div>
      <div class="card">
        <h2>Activity</h2><p class="sub">What the workers have been doing</p>
        <div class="log">${events.map((e) => `
          <div><span class="ts">${fmtTime(e.created_at)}</span>
          <span class="${esc(e.level)}">${esc(e.message)}</span></div>`).join("")
          || `<div class="muted">No activity yet.</div>`}</div>
      </div>
    </div>`;
}

/* ---------- review ---------- */

/* The list shows a handful of strong options rather than everything the
   providers returned. A ranked list is only useful as far down as anyone
   actually reads it, and a dozen rows of near-misses is how the confident answer
   at the top gets second-guessed. The rest are already loaded, so revealing them
   is free. */
const TOP_CANDIDATES = 4;

/* Card details are fetched once per song and kept, so paging back and forth
   through the queue does not re-request what it just had. The neighbours are
   warmed in the background, which is what makes Next feel like a page turn
   rather than a round trip. */
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
   the next card opens in its default shape rather than inheriting the last
   one's. */
function goToCard(index) {
  state.reviewIndex = index;
  state.candidatesShown = TOP_CANDIDATES;
  state.searchProviders = null;
  return renderReviewCard();
}

async function renderReview() {
  // Entering the tab is the one moment a cached card can be out of date: the
  // matcher may have resolved something in the background since it was read.
  detailCache.clear();
  state.candidatesShown = TOP_CANDIDATES;
  state.searchProviders = null;

  // The archive is only a count here; the list itself is the Library filtered to
  // it, which already has search, sorting and paging.
  const [data, archived] = await Promise.all([
    api("/songs?status=review,unmatched&sort=plays&limit=100"),
    api("/songs?status=archived&limit=1"),
  ]);
  state.reviewQueue = data.items;
  state.archivedCount = archived.total;
  if (state.reviewIndex >= data.items.length) state.reviewIndex = 0;

  if (!data.items.length) {
    $("#view-review").innerHTML = `
      ${emptyState("✅", "Review queue is clear",
        "Everything the matcher was unsure about has been resolved.")}
      ${state.archivedCount
        ? `<div class="row" style="justify-content:center">${archiveButton()}</div>` : ""}`;
    return;
  }
  await renderReviewCard();
}

/* The way back to anything set aside. Shown wherever the queue is, so the archive
   never becomes a place things silently disappear into. */
const archiveButton = () => state.archivedCount
  ? `<button class="btn ghost sm" id="rv-archived">Archived (${state.archivedCount})</button>`
  : "";

async function renderReviewCard(prefetched) {
  const song = state.reviewQueue[state.reviewIndex];
  if (!song) return renderReview();

  const detail = prefetched || await songDetail(song.id);
  const s = detail.song;
  const cands = detail.candidates;
  const shown = Math.min(Math.max(state.candidatesShown, TOP_CANDIDATES), cands.length);

  const candRow = (c, i) => {
    const d = c.score_detail || {};
    const sig = [];
    const mark = (v) => v >= 0.9 ? "good" : v >= 0.6 ? "warn" : "bad";
    if (d.artist !== undefined) sig.push(`<span class="sig ${mark(d.artist)}">artist ${Math.round(d.artist * 100)}%</span>`);
    if (d.title !== undefined) sig.push(`<span class="sig ${mark(d.title)}">title ${Math.round(d.title * 100)}%</span>`);
    if (d.duration !== undefined && d.duration !== null) sig.push(`<span class="sig ${mark(d.duration)}">length ${Math.round(d.duration * 100)}%</span>`);
    else sig.push(`<span class="sig">no length</span>`);
    // A merged row was found in several databases, so it names all of them —
    // and how many agreed is the point, because the confidence credit scales
    // with it. "Both" was fine with two providers and is wrong with four.
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
          <div class="sub2">${esc(c.artist)}${c.album ? ` · ${esc(c.album)}` : ""} · ${fmtDur(c.duration)}</div>
          <div class="signals">${sig.join("")}</div>
        </div>
        <div class="act">
          ${confidenceCell(c.score)}
          ${c.url ? `<a class="btn ghost sm" href="${esc(c.url)}" target="_blank" rel="noopener">Open</a>` : ""}
          <button class="btn sm" data-confirm="${c.id}" data-song="${s.id}">Use this</button>
        </div>
      </div>`;
  };

  const hidden = cands.length - shown;
  const candHtml = cands.length ? cands.slice(0, shown).map(candRow).join("") + (hidden > 0
    ? `<button class="btn ghost sm" id="rv-more" style="width:100%;margin-top:.2rem">
         Show ${hidden} weaker match${hidden === 1 ? "" : "es"}</button>`
    : "") : `<div class="muted" style="padding:.5rem 0">
      No candidates were found. Correct the artist or title above and search again,
      archive it for later, or mark it as not-a-song.</div>`;

  // Which providers actually answered the last manual search. Without it a
  // provider sitting in a cooldown reads as a search that simply found less.
  const providerNote = state.searchProviders?.length
    ? `<p class="sub">${state.searchProviders.map((p) => p.ok
        ? `${esc(p.name)}: ${p.count} result${p.count === 1 ? "" : "s"}`
        : `<span class="warn-text">${esc(p.name)}: ${esc(p.detail || "unavailable")}</span>`
      ).join(" · ")}</p>`
    : "";

  $("#view-review").innerHTML = `
    <div class="review-head">
      <div class="muted">Item ${state.reviewIndex + 1} of ${state.reviewQueue.length}</div>
      <div class="row" style="gap:.4rem">
        ${archiveButton()}
        <button class="btn ghost sm" id="rv-prev">← Previous</button>
        <button class="btn ghost sm" id="rv-next">Next →</button>
      </div>
    </div>

    <div class="stream-src">
      <div class="lbl">Heard on the stream</div>
      <h3>${esc(s.raw_title)}</h3>
      <div class="m">
        <span><strong>${esc(s.raw_artist || "unknown artist")}</strong></span>
        <span>${fmtDur(s.duration)}</span>
        <span>played ${s.play_count}×</span>
        <span>${detail.stations.map((st) => esc(st.name)).join(", ")}</span>
        ${statusBadge(s.status)}
      </div>
      ${s.nonsong_reason ? `<div class="muted" style="margin-top:.4rem">${esc(s.nonsong_reason)}</div>` : ""}
    </div>

    <div class="card" style="margin-bottom:1rem">
      <h2>Search</h2>
      <p class="sub">Prefilled with what the stream said. Correct either field and press
        Enter to search every enabled catalogue at once — results replace the list below.</p>
      <div class="row">
        <div class="field"><label>Artist</label>
          <input type="text" id="ms-artist" value="${esc(s.raw_artist)}"></div>
        <div class="field"><label>Title</label>
          <input type="text" id="ms-title" value="${esc(s.raw_title)}"></div>
        <button class="btn" id="ms-go" data-song="${s.id}">Search</button>
      </div>
      <div class="row" style="margin-top:.9rem; gap:.4rem">
        <button class="btn ghost sm" data-rematch="${s.id}">Run auto-match again</button>
        <button class="btn ghost sm" data-archive="${s.id}">Archive for later</button>
        <button class="btn bad sm" data-nonsong="${s.id}">Not a song (jingle / ID)</button>
      </div>
    </div>

    <div class="card" id="rv-matches">
      <h2>Suggested matches</h2>
      <p class="sub">Ranked by artist, title and track-length agreement. Confirming also teaches the matcher, so this track resolves instantly next time.</p>
      ${providerNote}
      ${candHtml}
    </div>`;

  prefetchNeighbours();
}

/* A search rewrites the card from the top, which would leave its own results off
   screen — so bring them back into view. */
const showMatches = () =>
  $("#rv-matches")?.scrollIntoView({ behavior: "smooth", block: "start" });

/* Both search endpoints return the whole refreshed card, so the result is shown
   from the response the search already produced rather than by asking for the
   same song again. */
async function showSearchResult(songId, detail) {
  detailCache.set(songId, Promise.resolve(detail));
  state.searchProviders = detail.providers || null;
  state.candidatesShown = TOP_CANDIDATES;
  await renderReviewCard(detail);
  showMatches();
}

/* Drop the item just decided on and land on whatever takes its place — the queue
   closes up behind it, so that is the next item without any navigation. */
function removeCurrentCard(songId) {
  detailCache.delete(songId);
  state.reviewQueue.splice(state.reviewIndex, 1);
  if (!state.reviewQueue.length) return renderReview();
  return goToCard(Math.min(state.reviewIndex, state.reviewQueue.length - 1));
}

async function runManualSearch(button, songId) {
  const artist = $("#ms-artist").value;
  const title = $("#ms-title").value;
  button.disabled = true;
  button.textContent = "Searching…";
  try {
    await showSearchResult(songId, await api(`/songs/${songId}/search`, {
      method: "POST", body: { artist, title },
    }));
  } catch (e) {
    toast(e.message, "bad");
    button.disabled = false;
    button.textContent = "Search";
  }
}

/* ---------- library ---------- */

async function renderLibrary() {
  const { status, q, sort } = state.library;
  const size = PAGE.library;
  const offset = (state.library.page - 1) * size;
  const params = new URLSearchParams({ sort, limit: String(size), offset: String(offset) });
  if (status) params.set("status", status);
  if (q) params.set("q", q);
  let data = await api(`/songs?${params}`);

  // Deleting or reclassifying songs can strand the view past the last page.
  if (!data.items.length && data.total) {
    state.library.page = Math.max(1, Math.ceil(data.total / size));
    params.set("offset", String((state.library.page - 1) * size));
    data = await api(`/songs?${params}`);
  }

  const chips = ["", "matched", "confirmed", "review", "unmatched", "archived", "pending", "nonsong"]
    .map((s) => `<button class="chip ${state.library.status === s ? "on" : ""}" data-filter="${s}">
        ${s || "All"}</button>`).join("");

  const rows = data.items.map((r) => `
    <tr>
      <td><div class="track">${artwork(r.match_art_url || r.art_url)}
        <div class="t"><b>${esc(r.match_title || r.raw_title)}</b>
        <span>${esc(r.match_artist || r.raw_artist)}</span></div></div></td>
      <td class="muted">${esc(r.raw_artist)} — ${esc(r.raw_title)}</td>
      <td>${statusBadge(r.status)}</td>
      <td>${confidenceCell(r.confidence)}</td>
      <td class="num muted">${r.play_count}</td>
      <td class="num muted">${fmtTime(r.last_seen_at)}</td>
      <td class="num">${r.status === "archived"
        ? `<button class="btn ghost sm" data-unarchive="${r.id}">Restore</button>`
        : r.status === "nonsong"
        ? `<button class="btn ghost sm" data-issong="${r.id}"
             title="${esc(r.nonsong_reason || "Filtered as station imaging")}">It's a song</button>`
        : r.spotify_url ? `<a href="${esc(r.spotify_url)}" target="_blank" rel="noopener">Spotify</a>` : ""}</td>
    </tr>`).join("");

  const archiveNote = state.library.status === "archived"
    ? `<p class="sub" style="margin:-.2rem 0 .7rem">Set aside from the review queue,
       untouched and out of the way. <strong>Restore</strong> puts one back where you
       left it.</p>`
    : state.library.status === "nonsong"
    ? `<p class="sub" style="margin:-.2rem 0 .7rem">Jingles, station IDs and anything
       too short to be a song. The filter has to be aggressive, so it does catch real
       music sometimes — a soundtrack cue under the minimum length, or a blank artist
       field. <strong>It's a song</strong> sends one back through matching and stops
       the filter catching it again. Hover the button for why it was filtered; if the
       reason is the length, the minimum is in Settings.</p>`
    : "";

  $("#view-library").innerHTML = `
    <div class="toolbar">
      <div class="chips">${chips}</div>
      <input type="text" id="lib-q" placeholder="Search artist or title…" value="${esc(q)}" style="max-width:260px">
      <select id="lib-sort" style="max-width:170px">
        <option value="recent"${sort === "recent" ? " selected" : ""}>Most recent</option>
        <option value="plays"${sort === "plays" ? " selected" : ""}>Most played</option>
        <option value="confidence"${sort === "confidence" ? " selected" : ""}>Lowest confidence</option>
        <option value="artist"${sort === "artist" ? " selected" : ""}>Artist A–Z</option>
      </select>
    </div>
    ${archiveNote}
    <div class="card"><div class="table-wrap"><table>
      <thead><tr><th>Matched as</th><th>Stream metadata</th><th>Status</th>
        <th>Confidence</th><th class="num">Plays</th><th class="num">Last seen</th><th class="num"></th></tr></thead>
      <tbody>${rows || `<tr><td colspan="7">${emptyState("🔍", "Nothing here yet")}</td></tr>`}
        ${fillerRows(data.items.length, size, data.total, 7)}</tbody>
    </table></div>
    ${pager("lib", { total: data.total, page: state.library.page, size, unit: "song" })}
    </div>`;
}

/* ---------- playlists ---------- */

async function renderPlaylists() {
  const lists = await api("/playlists");
  const size = PAGE.playlist;

  const cards = await Promise.all(lists.map(async (p) => {
    const tracks = await api(`/playlists/${p.id}/tracks`);
    const pages = Math.max(1, Math.ceil(tracks.length / size));
    const page = Math.min(Math.max(1, state.playlistPages[p.id] || 1), pages);
    state.playlistPages[p.id] = page;

    const shown = tracks.slice((page - 1) * size, page * size);
    const rows = shown.map((t) => `
      <tr><td><div class="track">${artwork(t.match_art_url || t.art_url)}
        <div class="t"><b>${esc(t.match_title || t.raw_title)}</b>
        <span>${esc(t.match_artist || t.raw_artist)}</span></div></div></td>
        <td class="num muted">${fmtDur(t.match_duration || t.duration)}</td>
        <td class="num">${t.spotify_url ? `<a href="${esc(t.spotify_url)}" target="_blank" rel="noopener">↗</a>` : `<span class="muted">no link</span>`}</td>
      </tr>`).join("");

    return `
      <div class="card" data-holiday="${esc(p.holiday)}">
        <div class="row" style="justify-content:space-between;align-items:center;margin-bottom:.6rem">
          <div><h2>${esc(p.name)}</h2>
            <p class="sub" style="margin:0">${p.entries} track(s) · ${p.spotify_synced || 0} on Spotify</p></div>
          <div class="row" style="gap:.4rem">
            ${p.spotify_playlist_id ? `<a class="btn ghost sm" target="_blank" rel="noopener"
              href="https://open.spotify.com/playlist/${esc(p.spotify_playlist_id)}">Open in Spotify</a>` : ""}
            <a class="btn ghost sm" href="/api/export/${p.id}.json" target="_blank">Export JSON</a>
            ${p.spotify_playlist_id ? `<button class="btn ghost sm" data-dedupe="${p.id}"
              title="Remove tracks listed more than once. Deduplicated tracks move to the end of the playlist.">Remove duplicates</button>` : ""}
            <button class="btn sm" data-sync="${p.id}">Sync now</button>
          </div>
        </div>
        <div class="table-wrap"><table>
          <tbody>${rows || `<tr><td class="muted">No matched tracks yet.</td></tr>`}
            ${fillerRows(shown.length, size, tracks.length, 3)}</tbody>
        </table></div>
        ${pager(`pl-${p.id}`, { total: tracks.length, page, size, unit: "track" })}
      </div>`;
  }));

  $("#view-playlists").innerHTML = `<div class="grid" style="gap:1rem">
    ${cards.join("") || emptyState("🎵", "No playlists yet")}</div>`;
}

/* ---------- stations ---------- */

async function renderStations() {
  const stations = await api("/stations");
  const size = PAGE.stations;
  const pages = Math.max(1, Math.ceil(stations.length / size));
  const page = Math.min(Math.max(1, state.stationsPage), pages);
  state.stationsPage = page;

  const shown = stations.slice((page - 1) * size, page * size);
  const rows = shown.map((s) => `
    <tr data-holiday="${esc(s.holiday)}">
      <td><b>${esc(s.name)}</b><div class="muted">${esc(s.azuracast_shortcode || s.icy_url || "")}</div></td>
      <td><span class="badge holiday">${esc(s.holiday)}</span></td>
      <td class="num muted">${s.plays}</td>
      <td class="num muted">${s.playlist_count}</td>
      <td class="num muted">${s.last_error
        ? `<span style="color:var(--bad)">${esc(s.last_error.slice(0, 48))}</span>`
        : fmtTime(s.last_polled_at)}</td>
      <td class="num">
        <label class="switch" title="Enabled">
          <input type="checkbox" data-toggle="${s.id}" ${s.enabled ? "checked" : ""}></label></td>
      <td class="num"><button class="btn bad sm" data-del-station="${s.id}">Remove</button></td>
    </tr>`).join("");

  $("#view-stations").innerHTML = `
    <div class="card" style="margin-bottom:1rem">
      <h2>Monitored stations</h2><p class="sub">The poller reads each enabled station on the interval set in Settings.</p>
      <div class="table-wrap"><table>
        <thead><tr><th>Station</th><th>Holiday</th><th class="num">Plays</th>
          <th class="num">Playlist</th><th class="num">Last poll</th>
          <th class="num">On</th><th class="num"></th></tr></thead>
        <tbody>${rows || `<tr><td colspan="7">${emptyState("📡", "No stations configured")}</td></tr>`}
          ${fillerRows(shown.length, size, stations.length, 7)}</tbody>
      </table></div>
      ${pager("st", { total: stations.length, page, size, unit: "station" })}
    </div>

    <div class="grid two">
      <div class="card">
        <h2>Discover from a server</h2>
        <p class="sub">Point this at an AzuraCast server to list every station it hosts, then add them in one click.</p>
        <div class="row">
          <div class="field"><label>Server URL</label>
            <input type="text" id="disc-url" value="https://radio1.streamserver.link">
            <span class="hint">The server address, not a stream link — though a
              stream URL is trimmed back to the server automatically.</span></div>
          <button class="btn" id="disc-go">Discover</button>
        </div>
        <div id="disc-results" style="margin-top:.9rem"></div>
      </div>

      <div class="card">
        <h2>Add manually</h2>
        <p class="sub">For any other Icecast or SHOUTcast stream.</p>
        <div class="field"><label>Name</label><input type="text" id="st-name" placeholder="Halloween Radio Oldies"></div>
        <div class="field"><label>Holiday</label>
          <select id="st-holiday">
            <option value="halloween">Halloween</option>
            <option value="christmas">Christmas</option>
            <option value="winter">Winter</option>
            <option value="generic">Other</option>
          </select></div>
        <div class="field"><label>Stream URL (ICY)</label>
          <input type="text" id="st-icy" placeholder="https://host:8000/mount"></div>
        <div class="row">
          <button class="btn ghost" id="st-probe">Test</button>
          <button class="btn" id="st-add">Add station</button>
        </div>
        <div id="st-probe-result" class="muted" style="margin-top:.6rem"></div>
      </div>
    </div>`;
}

/* ---------- settings ---------- */

async function renderSettings() {
  const data = await api("/settings");
  const v = data.values;
  const sp = data.spotify;
  const redirect = await api("/spotify/redirect-uri");

  $("#view-settings").innerHTML = `
    <div class="grid two">
      <div class="grid" style="gap:1rem">
        <div class="card">
          <h2>Matching</h2>
          <p class="sub">Higher auto-accept means fewer mistakes but more manual review.</p>
          <div class="row">
            <div class="field"><label>Auto-accept at or above</label>
              <input type="number" step="0.01" min="0.5" max="1" id="s-auto" value="${esc(v.auto_accept_score)}">
              <span class="hint">0.92 is a good balance. Raise it if you see any wrong matches.</span></div>
            <div class="field"><label>Send to review at or above</label>
              <input type="number" step="0.01" min="0" max="1" id="s-floor" value="${esc(v.review_floor_score)}">
              <span class="hint">Below this, a song is listed as unmatched.</span></div>
          </div>
          <div class="field"><label>Minimum song length (seconds)</label>
            <input type="number" id="s-minlen" value="${esc(v.min_song_seconds)}">
            <span class="hint">Anything shorter is treated as a jingle or station ID.</span></div>
          <div class="field"><label>Catalogues to search</label>
            <div class="row">
              <label class="switch"><input type="checkbox" id="s-mb" ${v.use_musicbrainz === "1" ? "checked" : ""}> MusicBrainz</label>
              <label class="switch"><input type="checkbox" id="s-sp" ${v.use_spotify === "1" ? "checked" : ""}> Spotify</label>
              <label class="switch"><input type="checkbox" id="s-dz" ${v.use_deezer === "1" ? "checked" : ""}> Deezer</label>
              <label class="switch"><input type="checkbox" id="s-it" ${v.use_itunes === "1" ? "checked" : ""}> Apple Music</label>
            </div>
            <span class="hint">MusicBrainz identifies and Spotify makes it playable.
              Deezer and Apple Music are there for the songs neither of those two
              carries — they need no account, and turning them off only costs you
              coverage. Agreement between catalogues raises confidence, so more of
              them on means fewer songs stuck in review.</span></div>
        </div>

        <div class="card">
          <h2>Polling &amp; delivery</h2>
          <div class="row">
            <div class="field"><label>Poll interval (seconds)</label>
              <input type="number" id="s-poll" value="${esc(v.poll_interval_seconds)}">
              <span class="hint">Play history is 15 deep, so 45–120s misses nothing.</span></div>
            <div class="field"><label>Spotify market</label>
              <input type="text" id="s-market" value="${esc(v.spotify_market)}" maxlength="2"></div>
          </div>
          <div class="row">
            <div class="field"><label>MusicBrainz cooldown (seconds)</label>
              <input type="number" id="s-mbcool" value="${esc(v.musicbrainz_cooldown_seconds)}">
              <span class="hint">How long to stop calling MusicBrainz after it asks us to
                slow down. It never says for how long, so this escalates to 3×, 10× and
                30× if throttling continues; matching falls back to Spotify meanwhile.</span></div>
            <div class="field"><label>Spotify cooldown (seconds)</label>
              <input type="number" id="s-spcool" value="${esc(v.spotify_cooldown_seconds)}">
              <span class="hint">Only a floor: Spotify states its own wait and that is
                honoured in full when it is longer. Escalates to 2×, 4× and 8× while
                throttling continues.</span></div>
          </div>
          <div class="row">
            <label class="switch"><input type="checkbox" id="s-m3u" ${v.m3u_enabled === "1" ? "checked" : ""}> Write .m3u8 files</label>
            <label class="switch"><input type="checkbox" id="s-spsync" ${v.spotify_sync_enabled === "1" ? "checked" : ""}> Sync to Spotify</label>
          </div>
          <div class="muted" style="margin-top:.6rem">
            Playlists folder: <code>${esc(data.playlist_dir.path)}</code>
            ${data.playlist_dir.writable ? `<span style="color:var(--ok)"> · writable</span>`
              : `<span style="color:var(--bad)"> · not writable: ${esc(data.playlist_dir.error)}</span>`}
          </div>
        </div>
      </div>

      <div class="grid" style="gap:1rem">
        <div class="card">
          <h2>Spotify</h2>
          <p class="sub">Search works with just the ID and secret. Creating playlists needs the account link.</p>
          <div class="field"><label>Client ID</label>
            <input type="text" id="s-cid" value="${esc(v.spotify_client_id)}"></div>
          <div class="field"><label>Client secret</label>
            <input type="password" id="s-csec" value="${esc(v.spotify_client_secret)}"></div>
          <div class="field"><label>Redirect URI — must match your Spotify app exactly</label>
            <input type="text" id="s-redir" value="${esc(redirect.redirect_uri)}" onclick="this.select()">
            ${redirect.warning
              ? `<span class="hint" style="color:var(--warn)">⚠ ${esc(redirect.warning)}</span>
                 <button class="btn ghost sm" id="sp-use-loopback" style="margin-top:.4rem;align-self:start">
                   Use ${esc(redirect.suggested_loopback)}</button>`
              : `<span class="hint" style="color:var(--ok)">✓ Spotify accepts this form.</span>`}
          </div>
          <div class="row" style="margin-top:.4rem">
            ${sp.linked
              ? `<span class="badge matched">Connected as ${esc(sp.user || "user")}</span>
                 <button class="btn ghost sm" id="sp-diagnose">Test Spotify access</button>
                 <button class="btn ghost sm" id="sp-unlink">Disconnect</button>`
              : `<button class="btn" id="sp-link" ${sp.configured ? "" : "disabled"}>Connect Spotify account</button>`}
          </div>
          ${sp.missing_scopes?.length ? `<div class="muted" style="margin-top:.5rem;color:var(--warn)">
            ⚠ This link is missing ${esc(sp.missing_scopes.join(", "))}. Disconnect and
            reconnect to grant it.</div>` : ""}
          <div id="sp-diag"></div>
          ${!sp.configured ? `<div class="muted" style="margin-top:.5rem">
            Create a free app at <a href="https://developer.spotify.com/dashboard" target="_blank" rel="noopener">developer.spotify.com</a>,
            then paste the ID and secret above and save.</div>` : ""}
          ${!sp.linked && sp.configured ? `
          <div style="margin-top:.9rem;padding-top:.9rem;border-top:1px solid var(--line)">
            <label style="font-size:.8rem;color:var(--text-dim);font-weight:600">
              Finish linking by pasting the URL you landed on</label>
            <p class="hint" style="margin:.25rem 0 .5rem">
              After approving, your browser lands on the redirect address. If the page
              fails to load, that is expected — copy the whole address from the bar and
              paste it here.</p>
            <div class="row">
              <div class="field" style="margin-bottom:0">
                <input type="text" id="sp-paste" placeholder="http://127.0.0.1:8686/api/spotify/callback?code=..."></div>
              <button class="btn" id="sp-exchange">Finish</button>
            </div>
          </div>` : ""}
        </div>

        <div class="card">
          <h2>Learned rules</h2>
          <p class="sub">Every confirmation you make becomes a permanent rule.</p>
          <div id="alias-list" class="log"></div>
        </div>
      </div>
    </div>
    <div class="row" style="margin-top:1rem"><button class="btn" id="s-save">Save settings</button></div>`;

  const aliases = await api("/aliases?limit=40");
  // A 'song' rule carries no identity — it only stops the non-song filter
  // catching this metadata — so there is no match to print on the right of it.
  const ruleTarget = (a) => a.kind === "nonsong" ? "<em>not a song</em>"
    : a.kind === "song" ? "<em>always treated as music</em>"
    : esc(`${a.match_artist} — ${a.match_title}`);
  $("#alias-list").innerHTML = aliases.length ? aliases.map((a) => `
    <div><span class="ts">${a.hits}×</span>
    <span>${esc(a.key_artist)} — ${esc(a.key_title)} → ${ruleTarget(a)}</span>
    <button class="btn bad sm" style="margin-left:auto" data-del-alias="${a.id}">×</button></div>`).join("")
    : `<div class="muted">No rules yet. They appear as you confirm matches.</div>`;
}

/* ---------- shell ---------- */

const VIEWS = {
  dashboard: { title: "Dashboard", sub: "Live monitoring across your holiday stations", render: renderDashboard },
  review:    { title: "Review queue", sub: "Confirm the matches the engine was not sure about", render: renderReview },
  library:   { title: "Library", sub: "Every song seen on the streams", render: renderLibrary },
  playlists: { title: "Playlists", sub: "What is being delivered to Spotify and disk", render: renderPlaylists },
  stations:  { title: "Stations", sub: "Streams being monitored", render: renderStations },
  settings:  { title: "Settings", sub: "Matching thresholds, providers and delivery", render: renderSettings },
};

function applyStats(stats) {
  const pending = stats.counts.review + stats.counts.unmatched;
  const pill = $("#pill-review");
  pill.textContent = pending;
  pill.dataset.zero = pending === 0 ? "1" : "0";
  const worker = $("#worker-state");
  const busy = stats.worker.polling || stats.worker.matching;
  worker.classList.toggle("busy", !!busy);
  const paused = coldProviders(stats).map((p) => p.label);
  worker.lastElementChild.textContent = paused.length ? `${paused.join(" & ")} paused`
    : stats.worker.matching ? "Matching…"
    : stats.worker.polling ? "Polling…" : "Idle";
  $("#app-version").textContent = `v${stats.version}`;
}

async function show(view) {
  state.view = view;
  $$("#nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.add("hidden"));
  const host = $(`#view-${view}`);
  host.classList.remove("hidden");
  $("#view-title").textContent = VIEWS[view].title;
  $("#view-sub").textContent = VIEWS[view].sub;
  if (!host.innerHTML.trim()) host.innerHTML = `<div class="empty"><span class="spinner"></span></div>`;
  try {
    await VIEWS[view].render();
  } catch (err) {
    host.innerHTML = emptyState("⚠️", "Could not load this view", err.message);
  }
  paintIcons();
}

async function refresh() {
  try {
    if (state.view === "dashboard") await renderDashboard();
    else applyStats(await api("/stats"));
  } catch { /* transient poll failure; the next tick retries */ }
}

/* ---------- events ---------- */

document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("[data-view],[data-confirm],[data-archive],[data-unarchive]," +
    "[data-nonsong],[data-issong],[data-rematch],[data-resume]," +
    "[data-sync],[data-dedupe],[data-del-station],[data-del-alias],[data-filter],[data-add-station],[data-page]," +
    "#btn-refresh,#btn-sync,#rv-prev,#rv-next,#rv-more,#rv-archived,#ms-go,#disc-go,#st-add,#st-probe,#s-save," +
    "#sp-link,#sp-unlink,#sp-exchange,#sp-use-loopback,#sp-diagnose");
  if (!t) return;

  const d = t.dataset;

  if (d.view) return show(d.view);

  if (d.page) {
    const [key, page] = d.page.split(":");
    const target = Math.max(1, +page);
    if (key === "lib") { state.library.page = target; return renderLibrary(); }
    if (key === "st")  { state.stationsPage = target; return renderStations(); }
    if (key.startsWith("pl-")) {
      state.playlistPages[key.slice(3)] = target;
      return renderPlaylists();
    }
    return;
  }

  if (t.id === "btn-refresh") { await show(state.view); return toast("Refreshed"); }

  if (d.resume) {
    t.disabled = true;
    try {
      const res = await api(`/providers/${d.resume}/resume`, { method: "POST" });
      toast(`Cooldown cleared — ${fmtWait(res.skipped_seconds)} of waiting skipped. `
        + "If it is still throttled, the next refusal opens a new pause.", "ok");
      return renderDashboard();
    } catch (e) { toast(e.message, "bad"); t.disabled = false; }
    return;
  }

  if (t.id === "btn-sync") {
    t.disabled = true;
    try {
      const res = await api("/playlists/sync", { method: "POST" });
      const added = res.results.reduce((n, r) => n + (r.spotify?.added || 0), 0);
      const problem = res.results.find((r) => r.spotify && !r.spotify.ok);
      toast(problem ? problem.spotify.reason : `Sync complete — ${added} track(s) added`,
        problem ? "bad" : "ok");
    } catch (e) { toast(e.message, "bad"); } finally { t.disabled = false; }
    return;
  }

  if (d.confirm) {
    t.disabled = true;
    try {
      await api(`/songs/${d.song}/confirm`, { method: "POST", body: { candidate_id: +d.confirm } });
      toast("Confirmed and remembered", "ok");
      await removeCurrentCard(+d.song);
    } catch (e) { toast(e.message, "bad"); t.disabled = false; }
    return;
  }

  if (d.nonsong) {
    try {
      await api(`/songs/${d.nonsong}/nonsong?remember=true`, { method: "POST" });
      toast("Marked as station imaging", "ok");
      await removeCurrentCard(+d.nonsong);
    } catch (e) { toast(e.message, "bad"); }
    return;
  }

  if (d.archive) {
    try {
      await api(`/songs/${d.archive}/archive`, { method: "POST" });
      state.archivedCount += 1;
      toast("Archived — restore it any time from the Library", "ok");
      await removeCurrentCard(+d.archive);
    } catch (e) { toast(e.message, "bad"); }
    return;
  }

  if (d.unarchive) {
    t.disabled = true;
    try {
      await api(`/songs/${d.unarchive}/unarchive`, { method: "POST" });
      state.archivedCount = Math.max(0, state.archivedCount - 1);
      toast("Back in the review queue", "ok");
      return renderLibrary();
    } catch (e) { toast(e.message, "bad"); t.disabled = false; }
    return;
  }

  if (d.issong) {
    t.disabled = true; t.textContent = "Matching…";
    try {
      // Re-matching runs inline, so the row comes back with a real verdict
      // rather than sitting as "pending" until the background loop reaches it.
      const song = await api(`/songs/${d.issong}/is-song`, { method: "POST" });
      const where = { matched: "matched automatically", confirmed: "matched automatically",
                      review: "sent to review", unmatched: "no match found — it is in the queue" };
      toast(`${where[song.song?.status] || song.song?.status || "requeued"} · the filter will not catch it again`, "ok");
    } catch (e) { toast(e.message, "bad"); }
    return renderLibrary();
  }

  if (t.id === "rv-archived") {
    state.library = { status: "archived", q: "", sort: "recent", page: 1 };
    return show("library");
  }

  if (d.rematch) {
    t.disabled = true; t.textContent = "Searching…";
    try {
      // Both endpoints answer with the refreshed card, so re-rendering from the
      // response costs no second round trip.
      await showSearchResult(+d.rematch,
        await api(`/songs/${d.rematch}/rematch`, { method: "POST" }));
    } catch (e) { toast(e.message, "bad"); t.disabled = false; t.textContent = "Run auto-match again"; }
    return;
  }

  if (t.id === "ms-go") return runManualSearch(t, +d.song);

  if (t.id === "rv-prev") return goToCard(Math.max(0, state.reviewIndex - 1));
  if (t.id === "rv-next") {
    return goToCard((state.reviewIndex + 1) % state.reviewQueue.length);
  }

  // Everything below the cut is already loaded, so this is a re-render, not a
  // fetch.
  if (t.id === "rv-more") {
    state.candidatesShown = Infinity;
    return renderReviewCard();
  }

  if (d.filter !== undefined) {
    state.library.status = d.filter; state.library.page = 1;
    return renderLibrary();
  }

  if (d.sync) {
    t.disabled = true; t.textContent = "Syncing…";
    try {
      const res = await api(`/playlists/sync?station_id=${d.sync}`, { method: "POST" });
      const r = res.results[0];
      toast(r.spotify && !r.spotify.ok ? r.spotify.reason
        : `Synced — ${r.spotify?.added ?? 0} added to Spotify, ${r.m3u?.entries ?? 0} in the M3U`,
        r.spotify && !r.spotify.ok ? "bad" : "ok");
    } catch (e) { toast(e.message, "bad"); }
    return renderPlaylists();
  }

  if (d.dedupe) {
    // Says what it will do before it does it: the tracks it cleans up lose
    // their place in the playlist, and that is not something to discover after.
    if (!confirm("Remove tracks that appear more than once in this Spotify playlist?\n\n"
      + "Each duplicated track is removed and added back once, so those tracks move "
      + "to the end of the playlist. Tracks that appear only once are left alone.")) return;
    t.disabled = true; t.textContent = "Cleaning…";
    try {
      const res = await api(`/playlists/${d.dedupe}/dedupe`, { method: "POST" });
      toast(res.ok
        ? (res.removed ? `Removed ${res.removed} duplicate track(s)` : "No duplicates found")
        : res.reason, res.ok ? "ok" : "bad");
    } catch (e) { toast(e.message, "bad"); }
    return renderPlaylists();
  }

  if (d.delStation) {
    if (!confirm("Remove this station? Its play history and playlist entries go with it.")) return;
    await api(`/stations/${d.delStation}`, { method: "DELETE" });
    toast("Station removed"); return renderStations();
  }

  if (d.delAlias) {
    await api(`/aliases/${d.delAlias}`, { method: "DELETE" });
    return renderSettings();
  }

  if (t.id === "disc-go") {
    t.disabled = true; t.textContent = "Looking…";
    try {
      const typed = $("#disc-url").value.trim();
      const res = await api("/stations/discover", { method: "POST", body: { base_url: typed } });
      // Say so when the pasted address was trimmed back to the server root.
      const note = res.base && res.base !== typed.replace(/\/+$/, "")
        ? `<div class="muted" style="margin-bottom:.6rem">Found ${res.count} station(s) on
           <code>${esc(res.base)}</code></div>` : "";
      $("#disc-results").innerHTML = note + res.stations.map((s) => `
        <div class="cand">
          <div class="ph">${icon("radio", 18)}</div>
          <div class="info"><b>${esc(s.name)}</b>
            <div class="sub2">${esc(s.description || s.shortcode)}</div></div>
          <div class="act">${s.already_added
            ? `<span class="badge matched">added</span>`
            : `<button class="btn sm" data-add-station='${esc(JSON.stringify({
                name: s.name, azuracast_base: s.azuracast_base,
                azuracast_shortcode: s.shortcode, icy_url: s.listen_url }))}'>Add</button>`}
          </div></div>`).join("") || `<div class="muted">No stations found.</div>`;
    } catch (e) { toast(e.message, "bad"); }
    finally { t.disabled = false; t.textContent = "Discover"; }
    return;
  }

  if (d.addStation) {
    const payload = JSON.parse(d.addStation);
    const name = payload.name.toLowerCase();
    payload.holiday = name.includes("christmas") || name.includes("snow") ? "christmas"
      : name.includes("halloween") ? "halloween" : "generic";
    try {
      await api("/stations", { method: "POST", body: payload });
      toast(`Added ${payload.name}`, "ok");
      t.outerHTML = `<span class="badge matched">added</span>`;
    } catch (e) { toast(e.message, "bad"); }
    return;
  }

  if (t.id === "st-probe") {
    const box = $("#st-probe-result");
    box.innerHTML = `<span class="spinner"></span>`;
    try {
      const res = await api("/stations/probe", { method: "POST", body: { icy_url: $("#st-icy").value } });
      box.innerHTML = res.ok
        ? `<span style="color:var(--ok)">✓ ${esc(res.source)} — now playing:
           <strong>${esc(res.now_playing.artist)} — ${esc(res.now_playing.title)}</strong></span>`
        : `<span style="color:var(--bad)">✕ ${esc(res.error)}</span>`;
    } catch (e) { box.innerHTML = `<span style="color:var(--bad)">${esc(e.message)}</span>`; }
    return;
  }

  if (t.id === "st-add") {
    try {
      await api("/stations", { method: "POST", body: {
        name: $("#st-name").value, holiday: $("#st-holiday").value, icy_url: $("#st-icy").value } });
      toast("Station added", "ok"); return renderStations();
    } catch (e) { toast(e.message, "bad"); }
    return;
  }

  if (t.id === "sp-use-loopback") {
    const { suggested_loopback } = await api("/spotify/redirect-uri");
    await api("/settings", { method: "PUT", body: { values: { spotify_redirect_uri: suggested_loopback } } });
    toast("Redirect URI set — add the same value in your Spotify app", "ok");
    return renderSettings();
  }

  if (t.id === "sp-link") {
    try {
      const { url, warning } = await api("/spotify/login");
      if (warning && !confirm(`${warning}\n\nOpen Spotify anyway?`)) return;
      // Opened in a new tab so this page stays put to receive the pasted URL.
      window.open(url, "_blank", "noopener");
      toast("Approve in the new tab, then paste the address you land on", "ok");
    } catch (e) { toast(e.message, "bad"); }
    return;
  }

  if (t.id === "sp-exchange") {
    t.disabled = true; t.textContent = "Linking…";
    try {
      const res = await api("/spotify/exchange", { method: "POST", body: { value: $("#sp-paste").value } });
      toast(`Spotify connected as ${res.user || "user"}`, "ok");
      return renderSettings();
    } catch (e) { toast(e.message, "bad"); t.disabled = false; t.textContent = "Finish"; }
    return;
  }
  if (t.id === "sp-diagnose") {
    const box = $("#sp-diag");
    t.disabled = true;
    box.innerHTML = `<div class="diag"><div class="muted"><span class="spinner"></span>
      Checking — this creates and removes one temporary playlist.</div></div>`;
    try {
      const r = await api("/spotify/diagnose", { method: "POST" });
      box.innerHTML = `<div class="diag">
        ${r.checks.map((c) => `
          <div class="chk ${c.ok ? "pass" : "fail"}">
            <span class="mark">${c.ok ? "✓" : "✕"}</span>
            <span>${esc(c.name)}${c.detail ? `<span class="detail"> — ${esc(c.detail)}</span>` : ""}</span>
          </div>`).join("")}
        <div class="verdict ${r.ok ? "pass" : "fail"}">${esc(r.hint)}</div>
      </div>`;
    } catch (e) {
      box.innerHTML = `<div class="diag"><div class="verdict fail">${esc(e.message)}</div></div>`;
    } finally { t.disabled = false; }
    return;
  }

  if (t.id === "sp-unlink") {
    await api("/spotify/unlink", { method: "POST" });
    toast("Spotify disconnected"); return renderSettings();
  }

  if (t.id === "s-save") {
    t.disabled = true;
    const values = {
      auto_accept_score: $("#s-auto").value,
      review_floor_score: $("#s-floor").value,
      min_song_seconds: $("#s-minlen").value,
      poll_interval_seconds: $("#s-poll").value,
      musicbrainz_cooldown_seconds: $("#s-mbcool").value,
      spotify_cooldown_seconds: $("#s-spcool").value,
      spotify_market: $("#s-market").value.toUpperCase(),
      use_musicbrainz: $("#s-mb").checked ? "1" : "0",
      use_spotify: $("#s-sp").checked ? "1" : "0",
      use_deezer: $("#s-dz").checked ? "1" : "0",
      use_itunes: $("#s-it").checked ? "1" : "0",
      m3u_enabled: $("#s-m3u").checked ? "1" : "0",
      spotify_sync_enabled: $("#s-spsync").checked ? "1" : "0",
      spotify_client_id: $("#s-cid").value.trim(),
      spotify_client_secret: $("#s-csec").value.trim(),
      spotify_redirect_uri: $("#s-redir").value.trim(),
    };
    try { await api("/settings", { method: "PUT", body: { values } }); toast("Settings saved", "ok"); await renderSettings(); }
    catch (e) { toast(e.message, "bad"); } finally { t.disabled = false; }
    return;
  }
});

document.addEventListener("change", async (ev) => {
  const el = ev.target;
  if (el.dataset.toggle) {
    await api(`/stations/${el.dataset.toggle}`, { method: "PATCH", body: { enabled: el.checked } });
    toast(el.checked ? "Station enabled" : "Station paused");
  }
  if (el.id === "lib-sort") { state.library.sort = el.value; state.library.page = 1; renderLibrary(); }
});

/* Correcting a field and pressing Enter is the natural way to run the review
   search; reaching for the button after typing is not. */
document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Enter") return;
  if (ev.target.id !== "ms-artist" && ev.target.id !== "ms-title") return;
  ev.preventDefault();
  const button = $("#ms-go");
  if (button && !button.disabled) runManualSearch(button, +button.dataset.song);
});

let searchTimer;
document.addEventListener("input", (ev) => {
  if (ev.target.id !== "lib-q") return;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.library.q = ev.target.value; state.library.page = 1;
    renderLibrary().then(() => $("#lib-q")?.focus());
  }, 320);
});

/* ---------- boot ---------- */

(async function boot() {
  paintIcons();
  const params = new URLSearchParams(location.search);
  if (params.get("spotify") === "linked") toast("Spotify connected", "ok");
  if (params.get("spotify") === "error") toast(`Spotify link failed: ${params.get("reason")}`, "bad");
  if (params.has("spotify")) history.replaceState({}, "", location.pathname);

  await show("dashboard");
  state.timer = setInterval(refresh, 15000);
})();
