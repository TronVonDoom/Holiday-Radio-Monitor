/* Library — every song ever seen, and the place a wrong verdict is undone.

   The filter, the search text, the sort and the page all live in the URL, so a
   view worth coming back to is a link rather than four clicks. */

import { api, qs } from "../api.js";
import { syncHash } from "../router.js";
import {
  artwork, confidenceCell, emptyState, fillerRows, pager, statusBadge, toast, withBusy,
} from "../ui.js";
import { $, esc, fmtStamp, fmtTime, num, paintIcons, statusLabel } from "../util.js";
import { PAGE, state } from "../state.js";

export const meta = { title: "Library", sub: "Every song seen on the streams" };

const FILTERS = ["", "matched", "confirmed", "review", "unmatched", "archived", "pending", "nonsong"];

/* Notes that only make sense while a particular filter is on. They explain a
   status that is easy to misread as a dead end, and both of them are. */
const NOTES = {
  archived: `Set aside from the review queue, untouched and out of the way.
    <strong>Restore</strong> puts one back exactly where you left it.`,
  nonsong: `Jingles, station IDs and anything too short to be a song. The filter has to be
    aggressive, so it does catch real music sometimes — a soundtrack cue under the minimum
    length, or a blank artist field. <strong>It's a song</strong> sends one back through
    matching and stops the filter catching it again. Hover the button for why it was
    filtered; if the reason is the length, the minimum is in Settings.`,
};

function chips() {
  const counts = state.stats?.counts;
  return FILTERS.map((s) => {
    const n = s ? counts?.[s] : state.stats?.total_songs;
    return `<button class="chip" data-act="filter" data-filter="${esc(s)}"
      aria-pressed="${state.library.status === s}">${esc(s ? statusLabel(s) : "All")}${
      n === undefined ? "" : `<span class="n">${num(n)}</span>`}</button>`;
  }).join("");
}

function rowActions(r) {
  if (r.status === "archived") {
    return `<button class="btn sm" data-act="unarchive" data-song="${r.id}">Restore</button>`;
  }
  if (r.status === "nonsong") {
    return `<button class="btn sm" data-act="issong" data-song="${r.id}"
      title="${esc(r.nonsong_reason || "Filtered as station imaging")}">It's a song</button>`;
  }
  if (r.spotify_url) {
    return `<a class="btn sm" href="${esc(r.spotify_url)}" target="_blank" rel="noopener"
      title="Open in Spotify"><i class="ic" data-icon="external"></i></a>`;
  }
  return "";
}

export async function render(host) {
  const { status, q, sort } = state.library;
  const size = PAGE.library;
  const query = { sort, limit: size, offset: (state.library.page - 1) * size, status, q };
  let data = await api(`/songs${qs(query)}`);

  // Deleting or reclassifying songs can strand the view past the last page.
  if (!data.items.length && data.total) {
    state.library.page = Math.max(1, Math.ceil(data.total / size));
    data = await api(`/songs${qs({ ...query, offset: (state.library.page - 1) * size })}`);
  }

  const rows = data.items.map((r) => `
    <tr>
      <td class="squeeze"><div class="track">${artwork(r.match_art_url || r.art_url)}
        <div class="t"><b>${esc(r.match_title || r.raw_title)}</b>
        <span>${esc(r.match_artist || r.raw_artist)}</span></div></div></td>
      <td class="dim truncate">${esc(r.raw_artist)} — ${esc(r.raw_title)}</td>
      <td>${statusBadge(r.status)}</td>
      <td>${confidenceCell(r.confidence)}</td>
      <td class="num dim">${num(r.play_count)}</td>
      <td class="num dim nowrap" title="${esc(fmtStamp(r.last_seen_at))}">${fmtTime(r.last_seen_at)}</td>
      <td class="num fit">${rowActions(r)}</td>
    </tr>`).join("");

  const note = NOTES[status]
    ? `<div class="card" style="padding:.85rem 1.1rem"><p class="sub">${NOTES[status]}</p></div>`
    : "";

  host.innerHTML = `
    <div class="toolbar">
      <div class="chips">${chips()}</div>
      <!-- Grouped so search and sort wrap together rather than one at a time. -->
      <div class="toolbar-end">
        <div class="search">
          <i class="ic" data-icon="search"></i>
          <label class="sr-only" for="lib-q">Search the library</label>
          <input type="search" id="lib-q" placeholder="Search artist or title…" value="${esc(q)}">
        </div>
        <label class="sr-only" for="lib-sort">Sort by</label>
        <select id="lib-sort">
          ${[["recent", "Most recent"], ["plays", "Most played"],
             ["confidence", "Lowest confidence"], ["artist", "Artist A–Z"]]
            .map(([v, l]) => `<option value="${v}"${sort === v ? " selected" : ""}>${l}</option>`).join("")}
        </select>
      </div>
    </div>
    ${note}
    <div class="card flush">
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th>Matched as</th><th>Stream metadata</th><th>Status</th><th>Confidence</th>
            <th class="num">Plays</th><th class="num">Last seen</th><th class="num"></th>
          </tr></thead>
          <tbody>
            ${rows || `<tr><td colspan="7">${emptyState("🔍", "Nothing here yet",
              q ? `No song matches “${q}”.` : "Songs appear as the poller sees them.")}</td></tr>`}
            ${fillerRows(data.items.length, size, data.total, 7)}
          </tbody>
        </table>
      </div>
      ${pager("lib", { total: data.total, page: state.library.page, size, unit: "song" })}
    </div>`;

  paintIcons(host);
}

/* --------------------------------------------------------------- actions -- */

export const actions = {
  filter(el, d) {
    state.library.status = d.filter;
    state.library.page = 1;
    syncHash();
    return render($("#view-library"));
  },

  async unarchive(el, d) {
    try {
      await withBusy(el, "…", () => api(`/songs/${d.song}/unarchive`, { method: "POST" }));
      state.archivedCount = Math.max(0, state.archivedCount - 1);
      toast("Back in the review queue", "ok");
      return render($("#view-library"));
    } catch (e) { toast(e.message, "bad"); }
  },

  async issong(el, d) {
    try {
      // Re-matching runs inline, so the row comes back with a real verdict
      // rather than sitting as "pending" until the background loop reaches it.
      await withBusy(el, "Matching…", async () => {
        const res = await api(`/songs/${d.song}/is-song`, { method: "POST" });
        const where = {
          matched: "matched automatically", confirmed: "matched automatically",
          review: "sent to review", unmatched: "no match found — it is in the queue",
        };
        toast(`${where[res.song?.status] || res.song?.status || "requeued"}`
          + " · the filter will not catch it again", "ok");
      });
    } catch (e) { toast(e.message, "bad"); }
    return render($("#view-library"));
  },
};
