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
  $("#toasts").append(el);
  setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 250); }, 3800);
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

const artwork = (url, cls = "") => url
  ? `<img class="${cls}" src="${esc(url)}" alt="" loading="lazy"
        onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'ph ${cls}',innerHTML:'${icon("music", 18).replace(/'/g, "&#39;")}'}))">`
  : `<div class="ph ${cls}">${icon("music", 18)}</div>`;

const statusBadge = (status) => `<span class="badge ${esc(status)}">${esc(status)}</span>`;

function confidenceCell(value) {
  const pct = Math.round((value || 0) * 100);
  const tier = pct >= 92 ? "high" : pct >= 62 ? "mid" : "low";
  return `<span class="conf ${tier}"><span class="meter"><i style="width:${pct}%"></i></span>${pct}%</span>`;
}

const emptyState = (glyph, title, note = "") =>
  `<div class="empty"><div class="big">${glyph}</div><div><strong>${esc(title)}</strong></div>
   ${note ? `<div class="muted" style="margin-top:.35rem">${esc(note)}</div>` : ""}</div>`;

/* ---------- state ---------- */

const state = {
  view: "dashboard",
  stats: null,
  reviewIndex: 0,
  reviewQueue: [],
  library: { status: "", q: "", sort: "recent", offset: 0 },
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

  const tiles = [
    { label: "Matched", value: resolved, foot: `${rate}% of resolved songs`, bar: rate },
    { label: "Needs review", value: c.review + c.unmatched, foot: "waiting on you" },
    { label: "In queue", value: c.pending, foot: stats.worker.matching ? "matching now…" : "idle" },
    { label: "Playlist tracks", value: stats.playlist_entries, foot: `${stats.stations} station(s)` },
    { label: "Learned rules", value: stats.aliases, foot: "auto-match forever" },
    { label: "Filtered", value: c.nonsong, foot: "jingles & station IDs" },
  ].map((t) => `
    <div class="card stat">
      <div class="label">${t.label}</div>
      <div class="value">${t.value.toLocaleString()}</div>
      <div class="foot">${esc(t.foot)}</div>
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
          <div class="table-wrap"><table>
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

async function renderReview() {
  const data = await api("/songs?status=review,unmatched&sort=plays&limit=100");
  state.reviewQueue = data.items;
  if (state.reviewIndex >= data.items.length) state.reviewIndex = 0;

  if (!data.items.length) {
    $("#view-review").innerHTML = emptyState("✅", "Review queue is clear",
      "Everything the matcher was unsure about has been resolved.");
    return;
  }
  await renderReviewCard();
}

async function renderReviewCard() {
  const song = state.reviewQueue[state.reviewIndex];
  if (!song) return renderReview();

  const detail = await api(`/songs/${song.id}`);
  const s = detail.song;
  const cands = detail.candidates;

  const candHtml = cands.length ? cands.map((c, i) => {
    const d = c.score_detail || {};
    const sig = [];
    const mark = (v) => v >= 0.9 ? "good" : v >= 0.6 ? "warn" : "bad";
    if (d.artist !== undefined) sig.push(`<span class="sig ${mark(d.artist)}">artist ${Math.round(d.artist * 100)}%</span>`);
    if (d.title !== undefined) sig.push(`<span class="sig ${mark(d.title)}">title ${Math.round(d.title * 100)}%</span>`);
    if (d.duration !== undefined && d.duration !== null) sig.push(`<span class="sig ${mark(d.duration)}">length ${Math.round(d.duration * 100)}%</span>`);
    else sig.push(`<span class="sig">no length</span>`);
    if (d.corroborated) sig.push(`<span class="sig good">both databases agree</span>`);
    if (d.isrc_verified) sig.push(`<span class="sig good">ISRC verified</span>`);
    (d.penalties || []).forEach((p) => sig.push(`<span class="sig bad">${esc(p)}</span>`));
    sig.push(`<span class="sig">${esc(c.source)}</span>`);

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
  }).join("") : `<div class="muted" style="padding:.5rem 0">
      No candidates were found. Try a manual search below, or mark it as not-a-song.</div>`;

  $("#view-review").innerHTML = `
    <div class="review-head">
      <div class="muted">Item ${state.reviewIndex + 1} of ${state.reviewQueue.length}</div>
      <div class="row" style="gap:.4rem">
        <button class="btn ghost sm" id="rv-prev">← Previous</button>
        <button class="btn ghost sm" id="rv-skip">Skip →</button>
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
      <h2>Suggested matches</h2>
      <p class="sub">Ranked by artist, title and track-length agreement. Confirming also teaches the matcher, so this track resolves instantly next time.</p>
      ${candHtml}
    </div>

    <div class="card">
      <h2>Search manually</h2>
      <p class="sub">Correct the artist or title and search MusicBrainz and Spotify again.</p>
      <div class="row">
        <div class="field"><label>Artist</label>
          <input type="text" id="ms-artist" value="${esc(s.raw_artist)}"></div>
        <div class="field"><label>Title</label>
          <input type="text" id="ms-title" value="${esc(s.raw_title)}"></div>
        <button class="btn" id="ms-go" data-song="${s.id}">Search</button>
      </div>
      <div class="row" style="margin-top:.9rem; gap:.4rem">
        <button class="btn ghost sm" data-rematch="${s.id}">Run auto-match again</button>
        <button class="btn bad sm" data-nonsong="${s.id}">Not a song (jingle / ID)</button>
        <button class="btn bad sm" data-reject="${s.id}">Skip permanently</button>
      </div>
    </div>`;
}

/* ---------- library ---------- */

async function renderLibrary() {
  const { status, q, sort, offset } = state.library;
  const params = new URLSearchParams({ sort, limit: "50", offset: String(offset) });
  if (status) params.set("status", status);
  if (q) params.set("q", q);
  const data = await api(`/songs?${params}`);

  const chips = ["", "matched", "confirmed", "review", "unmatched", "pending", "nonsong"]
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
      <td class="num">${r.spotify_url ? `<a href="${esc(r.spotify_url)}" target="_blank" rel="noopener">Spotify</a>` : ""}</td>
    </tr>`).join("");

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
      <span class="muted">${data.total.toLocaleString()} song(s)</span>
    </div>
    <div class="card"><div class="table-wrap"><table>
      <thead><tr><th>Matched as</th><th>Stream metadata</th><th>Status</th>
        <th>Confidence</th><th class="num">Plays</th><th class="num">Last seen</th><th></th></tr></thead>
      <tbody>${rows || `<tr><td colspan="7">${emptyState("🔍", "Nothing here yet")}</td></tr>`}</tbody>
    </table></div>
    ${data.total > 50 ? `<div class="row" style="margin-top:.8rem;justify-content:center">
      <button class="btn ghost sm" id="lib-prev" ${offset === 0 ? "disabled" : ""}>← Previous</button>
      <span class="muted">${offset + 1}–${Math.min(offset + 50, data.total)}</span>
      <button class="btn ghost sm" id="lib-next" ${offset + 50 >= data.total ? "disabled" : ""}>Next →</button>
    </div>` : ""}
    </div>`;
}

/* ---------- playlists ---------- */

async function renderPlaylists() {
  const lists = await api("/playlists");
  const cards = await Promise.all(lists.map(async (p) => {
    const tracks = await api(`/playlists/${p.id}/tracks`);
    const rows = tracks.slice(0, 12).map((t) => `
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
            <button class="btn sm" data-sync="${p.id}">Sync now</button>
          </div>
        </div>
        <div class="table-wrap"><table><tbody>${rows || `<tr><td class="muted">No matched tracks yet.</td></tr>`}</tbody></table></div>
        ${p.entries > 12 ? `<div class="muted" style="margin-top:.5rem">+ ${p.entries - 12} more</div>` : ""}
      </div>`;
  }));

  $("#view-playlists").innerHTML = `<div class="grid" style="gap:1rem">
    ${cards.join("") || emptyState("🎵", "No playlists yet")}</div>`;
}

/* ---------- stations ---------- */

async function renderStations() {
  const stations = await api("/stations");
  const rows = stations.map((s) => `
    <tr data-holiday="${esc(s.holiday)}">
      <td><b>${esc(s.name)}</b><div class="muted">${esc(s.azuracast_shortcode || s.icy_url || "")}</div></td>
      <td><span class="badge holiday">${esc(s.holiday)}</span></td>
      <td class="num muted">${s.plays}</td>
      <td class="num muted">${s.playlist_count}</td>
      <td class="muted">${s.last_error
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
          <th class="num">Playlist</th><th>Last poll</th><th class="num">On</th><th></th></tr></thead>
        <tbody>${rows || `<tr><td colspan="7">${emptyState("📡", "No stations configured")}</td></tr>`}</tbody>
      </table></div>
    </div>

    <div class="grid two">
      <div class="card">
        <h2>Discover from a server</h2>
        <p class="sub">Point this at an AzuraCast server to list every station it hosts, then add them in one click.</p>
        <div class="row">
          <div class="field"><label>Server URL</label>
            <input type="text" id="disc-url" value="https://radio1.streamserver.link"></div>
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
  const redirect = (await api("/spotify/redirect-uri")).redirect_uri;

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
          <div class="row">
            <label class="switch"><input type="checkbox" id="s-mb" ${v.use_musicbrainz === "1" ? "checked" : ""}> Use MusicBrainz</label>
            <label class="switch"><input type="checkbox" id="s-sp" ${v.use_spotify === "1" ? "checked" : ""}> Use Spotify search</label>
          </div>
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
          <div class="field"><label>Redirect URI (paste this into your Spotify app)</label>
            <input type="text" id="s-redir" value="${esc(redirect)}" readonly onclick="this.select()"></div>
          <div class="row" style="margin-top:.4rem">
            ${sp.linked
              ? `<span class="badge matched">Connected as ${esc(sp.user || "user")}</span>
                 <button class="btn ghost sm" id="sp-unlink">Disconnect</button>`
              : `<button class="btn" id="sp-link" ${sp.configured ? "" : "disabled"}>Connect Spotify account</button>`}
          </div>
          ${!sp.configured ? `<div class="muted" style="margin-top:.5rem">
            Create a free app at <a href="https://developer.spotify.com/dashboard" target="_blank" rel="noopener">developer.spotify.com</a>,
            then paste the ID and secret above and save.</div>` : ""}
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
  $("#alias-list").innerHTML = aliases.length ? aliases.map((a) => `
    <div><span class="ts">${a.hits}×</span>
    <span>${esc(a.key_artist)} — ${esc(a.key_title)} →
    ${a.kind === "nonsong" ? "<em>not a song</em>" : esc(`${a.match_artist} — ${a.match_title}`)}</span>
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
  worker.lastElementChild.textContent = stats.worker.matching ? "Matching…"
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
  const t = ev.target.closest("[data-view],[data-confirm],[data-reject],[data-nonsong],[data-rematch]," +
    "[data-sync],[data-del-station],[data-del-alias],[data-filter],[data-add-station]," +
    "#btn-refresh,#btn-sync,#rv-prev,#rv-skip,#ms-go,#disc-go,#st-add,#st-probe,#s-save," +
    "#sp-link,#sp-unlink,#lib-prev,#lib-next");
  if (!t) return;

  const d = t.dataset;

  if (d.view) return show(d.view);

  if (t.id === "btn-refresh") { await show(state.view); return toast("Refreshed"); }

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
      state.reviewQueue.splice(state.reviewIndex, 1);
      await (state.reviewQueue.length ? renderReviewCard() : renderReview());
    } catch (e) { toast(e.message, "bad"); t.disabled = false; }
    return;
  }

  if (d.nonsong || d.reject) {
    const id = d.nonsong || d.reject;
    const path = d.nonsong ? "nonsong" : "reject";
    try {
      await api(`/songs/${id}/${path}?remember=true`, { method: "POST" });
      toast(d.nonsong ? "Marked as station imaging" : "Skipped permanently", "ok");
      state.reviewQueue.splice(state.reviewIndex, 1);
      await (state.reviewQueue.length ? renderReviewCard() : renderReview());
    } catch (e) { toast(e.message, "bad"); }
    return;
  }

  if (d.rematch) {
    t.disabled = true; t.textContent = "Searching…";
    try { await api(`/songs/${d.rematch}/rematch`, { method: "POST" }); await renderReviewCard(); }
    catch (e) { toast(e.message, "bad"); }
    return;
  }

  if (t.id === "ms-go") {
    t.disabled = true; t.textContent = "Searching…";
    try {
      await api(`/songs/${d.song}/search`, {
        method: "POST",
        body: { artist: $("#ms-artist").value, title: $("#ms-title").value },
      });
      await renderReviewCard();
    } catch (e) { toast(e.message, "bad"); t.disabled = false; t.textContent = "Search"; }
    return;
  }

  if (t.id === "rv-prev") {
    state.reviewIndex = Math.max(0, state.reviewIndex - 1);
    return renderReviewCard();
  }
  if (t.id === "rv-skip") {
    state.reviewIndex = (state.reviewIndex + 1) % state.reviewQueue.length;
    return renderReviewCard();
  }

  if (d.filter !== undefined) {
    state.library.status = d.filter; state.library.offset = 0;
    return renderLibrary();
  }
  if (t.id === "lib-prev") { state.library.offset = Math.max(0, state.library.offset - 50); return renderLibrary(); }
  if (t.id === "lib-next") { state.library.offset += 50; return renderLibrary(); }

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
      const res = await api("/stations/discover", { method: "POST", body: { base_url: $("#disc-url").value } });
      $("#disc-results").innerHTML = res.stations.map((s) => `
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

  if (t.id === "sp-link") {
    try {
      const { url } = await api("/spotify/login");
      location.href = url;
    } catch (e) { toast(e.message, "bad"); }
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
      spotify_market: $("#s-market").value.toUpperCase(),
      use_musicbrainz: $("#s-mb").checked ? "1" : "0",
      use_spotify: $("#s-sp").checked ? "1" : "0",
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
  if (el.id === "lib-sort") { state.library.sort = el.value; state.library.offset = 0; renderLibrary(); }
});

let searchTimer;
document.addEventListener("input", (ev) => {
  if (ev.target.id !== "lib-q") return;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.library.q = ev.target.value; state.library.offset = 0;
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
