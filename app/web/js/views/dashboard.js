/* Dashboard — is it working, is anything stuck, and what is on air.

   Refreshes on a timer, so it is deliberately built to be *patched* rather than
   re-rendered: replacing the whole view every fifteen seconds resets the scroll
   position of the activity log, drops the row you were hovering, and re-fetches
   every piece of artwork. Each block below carries a signature of the data it
   was drawn from and is only rebuilt when that signature changes. */

import { api } from "../api.js";
import { artwork, confidenceCell, emptyState, statusBadge, toast } from "../ui.js";
import { $, esc, fmtStamp, fmtTime, fmtWait, num, paintIcons } from "../util.js";
import { coldProviders, providerCalls, state } from "../state.js";

export const meta = {
  title: "Dashboard",
  sub: "Live monitoring across your holiday stations",
};

/* ----------------------------------------------------------------- data -- */

/* One request, not four. Everything here is drawn together and refreshed
   together, so asking for it separately was four round trips and four passes
   through the middleware for one screen.

   Exported because the shell calls it at boot when the dashboard is the landing
   view — otherwise boot would fetch the stats this payload already carries. */
export async function load() {
  const data = await api("/dashboard?recent_limit=25&event_limit=40");
  state.stats = data.stats;
  return { stats: data.stats, np: data.nowplaying, recent: data.recent, events: data.events };
}

/* ---------------------------------------------------------------- tiles -- */

function tiles(stats) {
  const c = stats.counts;
  const resolved = c.matched + c.confirmed;
  const waiting = c.review + c.unmatched;
  const rate = Math.round(stats.match_rate * 100);

  return [
    { key: "matched", label: "Matched", value: resolved,
      foot: `${rate}% of everything resolved`, bar: rate },
    { key: "review", label: "Needs review", value: waiting,
      foot: waiting ? "waiting on you" : "queue is clear", attn: waiting > 0 },
    { key: "queue", label: "In queue", value: c.pending,
      foot: stats.worker.matching ? "matching now…" : "nothing pending" },
    { key: "tracks", label: "Playlist tracks", value: stats.playlist_entries,
      foot: `across ${num(stats.stations)} station${stats.stations === 1 ? "" : "s"}` },
    { key: "rules", label: "Learned rules", value: stats.aliases,
      foot: "matched instantly, forever" },
    { key: "filtered", label: "Filtered", value: c.nonsong,
      foot: "jingles & station IDs" },
  ];
}

const tileHtml = (t) => `
  <div class="card stat${t.attn ? " attn" : ""}" data-tile="${t.key}">
    <div class="label">${esc(t.label)}</div>
    <div class="value">${num(t.value)}</div>
    <div class="foot">${esc(t.foot)}</div>
    ${t.bar !== undefined ? `<div class="bar"><i style="width:${t.bar}%"></i></div>` : ""}
  </div>`;

function patchTiles(stats) {
  for (const t of tiles(stats)) {
    const el = $(`[data-tile="${t.key}"]`);
    if (!el) return false;                       // layout changed; caller repaints
    el.classList.toggle("attn", !!t.attn);
    el.querySelector(".value").textContent = num(t.value);
    el.querySelector(".foot").textContent = t.foot;
    if (t.bar !== undefined) el.querySelector(".bar > i").style.width = `${t.bar}%`;
  }
  return true;
}

/* ------------------------------------------------------------ catalogues -- */

/* A cooldown is the symptom and arrives too late to act on; the send rate is
   the cause, and it is the only number that can be checked against a published
   budget while there is still time to turn something down. So both are here,
   per provider, with the way out of a cooldown attached to the provider that
   is in one. */

/* A raw send count is hard to judge — Spotify's ban arrived with an hourly
   figure that looked unremarkable against every estimate anyone had made of it.
   So where a provider has a sustained ceiling, the count is shown against the
   ceiling it is spending, which is the only form in which it means anything. */
const rateText = (p) => `${p.requests_1m || 0}/min · ${p.requests_1h || 0}${
  p.budget_per_hour ? ` of ${p.budget_per_hour}` : ""}/hr`;

const rateTitle = (p) => p.budget_per_hour
  ? "Requests sent in the last minute, and in the last hour against the hourly budget"
  : "Requests sent in the last minute / hour";

/* What losing this provider actually costs. A cold catalogue costs identification
   and the others carry on; a cold Spotify costs playlist delivery and leaves
   identification untouched, because the match loop never asks it. Saying
   "matching continues on the rest" for Spotify reassures about the wrong thing. */
export const coldNote = (cold) => {
  if (!cold.length) return "All answering. Matching uses whichever agree.";
  const names = cold.map((p) => p.label).join(" & ");
  return cold.some((p) => p.background !== false)
    ? `${names} paused — matching continues on the rest`
    : `${names} paused — delivery is waiting; matching is unaffected`;
};

const providerRow = (p) => {
  const cls = !p.enabled || !p.configured ? "off" : p.throttled ? "paused" : "live";
  const stateText = !p.enabled ? "switched off"
    : !p.configured ? "needs credentials"
    : p.throttled ? `paused ${fmtWait(p.cooldown_seconds)}`
    // Spotify sits in this list but is not searched like the rest of it, and a
    // bare "ready" invites the reader to assume otherwise.
    : p.background === false ? "ready · delivery & manual search"
    : "ready";
  return `
    <div class="provider ${cls}" data-provider="${esc(p.key)}">
      <span class="dot" aria-hidden="true"></span>
      <div>
        <div class="name">${esc(p.label)}</div>
        <div class="state">${esc(stateText)}</div>
      </div>
      <div class="row tight center">
        <span class="rate" title="${esc(rateTitle(p))}">${esc(rateText(p))}</span>
        ${p.throttled
          ? `<button class="btn sm" data-act="resume" data-provider="${esc(p.key)}">Resume</button>`
          : ""}
      </div>
    </div>`;
};

const providerSig = (stats) => (stats.providers || [])
  .map((p) => `${p.key}:${p.enabled}:${p.configured}:${p.throttled}`).join("|");

function catalogueCard(stats) {
  const cold = coldProviders(stats);
  return `
    <div class="card" id="dash-providers" data-sig="${esc(providerSig(stats))}">
      <div class="card-head">
        <div>
          <h2>Catalogues</h2>
          <p class="sub">${esc(coldNote(cold))}</p>
        </div>
        <span class="badge ${cold.length ? "review" : "ok"}"
              title="Requests sent to all catalogues in the last minute">${
          providerCalls(stats)} req/min</span>
      </div>
      <div class="providers">${(stats.providers || []).map(providerRow).join("")}</div>
    </div>`;
}

function patchCatalogues(stats) {
  const host = $("#dash-providers");
  if (!host) return false;
  const sig = providerSig(stats);
  if (host.dataset.sig !== sig) {
    host.outerHTML = catalogueCard(stats);
    paintIcons($("#view-dashboard"));
    return true;
  }
  // Same roster and same breaker states: only the counters moved.
  const cold = coldProviders(stats);
  host.querySelector(".card-head .sub").textContent = coldNote(cold);
  host.querySelector(".card-head .badge").textContent = `${providerCalls(stats)} req/min`;
  for (const p of stats.providers || []) {
    const row = host.querySelector(`[data-provider="${p.key}"]`);
    if (!row) continue;
    row.querySelector(".rate").textContent = rateText(p);
    if (p.throttled) row.querySelector(".state").textContent = `paused ${fmtWait(p.cooldown_seconds)}`;
  }
  return true;
}

/* ----------------------------------------------------------- on air now -- */

const npSig = (np) => np
  .map((s) => `${s.station_id}:${s.song_id}:${s.enabled}:${s.status || ""}:${s.last_error || ""}`)
  .join("|");

function npCards(np) {
  if (!np.length) {
    return emptyState("📻", "No stations yet", "Add one to start monitoring.",
      `<a class="btn primary" href="#/stations">Add a station</a>`);
  }
  return `<div class="np-grid">${np.map((s) => {
    const cls = s.last_error ? "offline" : s.enabled ? "" : "paused";
    const label = s.last_error ? "offline" : s.enabled ? "on air" : "paused";
    const line2 = s.raw_artist
      || (s.last_error ? s.last_error.slice(0, 70) : "waiting for metadata");
    return `
      <div class="np ${cls}" data-holiday="${esc(s.holiday)}">
        ${artwork(s.art_url)}
        <div class="meta">
          <div class="st"><span class="dot"></span><span>${esc(s.station)} · ${label}</span></div>
          <b>${esc(s.raw_title || "—")}</b>
          <small>${esc(line2)}</small>
        </div>
        ${s.status ? statusBadge(s.status) : ""}
      </div>`;
  }).join("")}</div>`;
}

/* ---------------------------------------------------------- recent plays -- */

const recentSig = (recent) => recent.map((r) => `${r.song_id}:${r.played_at}:${r.status}`).join("|");

const recentTable = (recent) => `
  <div class="table-wrap feed">
    <table>
      <thead><tr>
        <th>Track</th><th>Station</th><th>Status</th>
        <th class="num">Confidence</th><th class="num">Played</th>
      </tr></thead>
      <tbody>${recent.map((r) => `
        <tr>
          <td class="squeeze"><div class="track">${artwork(r.art_url)}
            <div class="t"><b>${esc(r.match_title || r.raw_title)}</b>
            <span>${esc(r.match_artist || r.raw_artist)}</span></div></div></td>
          <td class="dim truncate">${esc(r.station)}</td>
          <td>${statusBadge(r.status)}</td>
          <td class="num">${confidenceCell(r.confidence)}</td>
          <td class="num dim nowrap" title="${esc(fmtStamp(r.played_at))}">${fmtTime(r.played_at)}</td>
        </tr>`).join("") || `
        <tr><td colspan="5" class="dim">Nothing yet — the poller reads each station on the
          interval set in Settings.</td></tr>`}
      </tbody>
    </table>
  </div>`;

/* -------------------------------------------------------------- activity -- */

const eventSig = (events) => `${events[0]?.id ?? 0}:${events.length}`;

const activityLog = (events) => events.length
  ? events.map((e) => `
      <div class="log-line ${esc(e.level)}">
        <span class="ts" title="${esc(fmtStamp(e.created_at))}">${fmtTime(e.created_at)}</span>
        <span>${esc(e.message)}</span>
      </div>`).join("")
  : `<div class="dim">No activity yet.</div>`;

/* -------------------------------------------------------------- painting -- */

function paintFull(host, { stats, np, recent, events }) {
  host.innerHTML = `
    <div class="grid stats">${tiles(stats).map(tileHtml).join("")}</div>

    <div class="grid split">
      <div class="card" id="dash-np" data-sig="${esc(npSig(np))}">
        <div class="card-head">
          <div><h2>On air now</h2>
            <p class="sub">The latest track seen on each monitored station</p></div>
          <a class="btn sm" href="#/stations">Manage</a>
        </div>
        ${npCards(np)}
      </div>
      ${catalogueCard(stats)}
    </div>

    <div class="grid split">
      <div class="card flush" id="dash-recent" data-sig="${esc(recentSig(recent))}">
        <div class="card-head">
          <div><h2>Recent plays</h2><p class="sub">Newest first, across every station</p></div>
          <a class="btn sm" href="#/library">Open library</a>
        </div>
        ${recentTable(recent)}
      </div>

      <div class="card" id="dash-activity" data-sig="${esc(eventSig(events))}">
        <div class="card-head"><div>
          <h2>Activity</h2><p class="sub">What the workers have been doing</p></div></div>
        <div class="log">${activityLog(events)}</div>
      </div>
    </div>`;
}

/** Patch in place. Returns false when the shape changed and a repaint is due. */
function patch(host, { stats, np, recent, events }) {
  if (!host.firstElementChild) return false;
  if (!patchTiles(stats)) return false;
  if (!patchCatalogues(stats)) return false;

  const npHost = $("#dash-np");
  if (npHost && npHost.dataset.sig !== npSig(np)) {
    npHost.dataset.sig = npSig(np);
    npHost.querySelector(".np-grid, .empty").outerHTML = npCards(np);
  }

  const recentHost = $("#dash-recent");
  if (recentHost && recentHost.dataset.sig !== recentSig(recent)) {
    recentHost.dataset.sig = recentSig(recent);
    const wrap = recentHost.querySelector(".table-wrap");
    const scroll = wrap?.scrollTop ?? 0;
    wrap.outerHTML = recentTable(recent);
    recentHost.querySelector(".table-wrap").scrollTop = scroll;
  }

  const logHost = $("#dash-activity");
  if (logHost && logHost.dataset.sig !== eventSig(events)) {
    logHost.dataset.sig = eventSig(events);
    const log = logHost.querySelector(".log");
    // A log the user has scrolled back through should not jump on a new line.
    const pinned = log.scrollTop < 8;
    log.innerHTML = activityLog(events);
    if (!pinned) log.scrollTop = 0;
  }
  return true;
}

export async function render(host, { quiet = false, data = null } = {}) {
  const payload = data || await load();
  if (!quiet || !patch(host, payload)) paintFull(host, payload);
  paintIcons(host);
}

/* --------------------------------------------------------------- actions -- */

export const actions = {
  async resume(el, d) {
    el.disabled = true;
    try {
      const res = await api(`/providers/${d.provider}/resume`, { method: "POST" });
      toast(`Cooldown cleared — ${fmtWait(res.skipped_seconds)} of waiting skipped. `
        + "If the service is still throttling us, the next refusal opens a new pause.", "ok");
      await render($("#view-dashboard"));
    } catch (e) {
      toast(e.message, "bad");
      el.disabled = false;
    }
  },
};
