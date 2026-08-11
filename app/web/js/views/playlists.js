/* Playlists — what is actually being delivered, per station.

   Tracks are paged on the server. A station that has been running a season
   holds thousands of entries, and fetching all of them to show ten was the
   single most expensive request the interface made. */

import { api, qs } from "../api.js";
import {
  artwork, confirmDialog, emptyState, fillerRows, pager, toast, withBusy,
} from "../ui.js";
import { $, esc, fmtDur, num, paintIcons } from "../util.js";
import { PAGE, state } from "../state.js";

export const meta = {
  title: "Playlists",
  sub: "What is being delivered to Spotify and to disk",
};

async function stationCard(p) {
  const size = PAGE.playlist;
  const page = Math.max(1, state.playlistPages[p.id] || 1);
  const tracks = await api(`/playlists/${p.id}/tracks${qs({ limit: size, offset: (page - 1) * size })}`);

  // A page beyond the end (tracks removed since the last render) falls back.
  const pages = Math.max(1, Math.ceil(tracks.total / size));
  if (page > pages) {
    state.playlistPages[p.id] = pages;
    return stationCard(p);
  }

  const synced = p.spotify_synced || 0;
  const rows = tracks.items.map((t, i) => `
    <tr>
      <td class="num dim mono fit">${num((page - 1) * size + i + 1)}</td>
      <td class="squeeze"><div class="track">${artwork(t.match_art_url || t.art_url)}
        <div class="t"><b>${esc(t.match_title || t.raw_title)}</b>
        <span>${esc(t.match_artist || t.raw_artist)}</span></div></div></td>
      <td class="num dim nowrap">${fmtDur(t.match_duration || t.duration)}</td>
      <td class="num fit">${t.spotify_url
        ? `<a class="btn sm" href="${esc(t.spotify_url)}" target="_blank" rel="noopener"
             title="Open in Spotify"><i class="ic" data-icon="external"></i></a>`
        : `<span class="badge nonsong" title="Identified, but with no playable Spotify link yet">
             no link</span>`}</td>
    </tr>`).join("");

  return `
    <div class="card flush" data-holiday="${esc(p.holiday)}">
      <div class="card-head">
        <div>
          <h2>${esc(p.name)}</h2>
          <p class="sub">${num(tracks.total)} deliverable track${tracks.total === 1 ? "" : "s"} ·
            ${num(synced)} on Spotify${p.spotify_playlist_id ? "" : " · no Spotify playlist yet"}</p>
        </div>
        <div class="card-actions">
          ${p.spotify_playlist_id ? `
            <a class="btn sm" target="_blank" rel="noopener"
               href="https://open.spotify.com/playlist/${esc(p.spotify_playlist_id)}">
              <i class="ic" data-icon="external"></i>Spotify</a>` : ""}
          <a class="btn sm" href="/api/export/${p.id}.json" target="_blank">Export JSON</a>
          ${p.spotify_playlist_id ? `
            <button class="btn sm" data-act="dedupe" data-station="${p.id}"
              title="Remove tracks listed more than once. Deduplicated tracks move to the end.">
              Remove duplicates</button>` : ""}
          <button class="btn primary sm" data-act="sync" data-station="${p.id}">
            <i class="ic" data-icon="upload"></i>Sync now</button>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <tbody>
            ${rows || `<tr><td>${emptyState("🎧", "No matched tracks yet",
              "Songs land here once the matcher is confident about them.")}</td></tr>`}
            ${fillerRows(tracks.items.length, size, tracks.total, 4)}
          </tbody>
        </table>
      </div>
      ${pager(`pl-${p.id}`, { total: tracks.total, page, size, unit: "track" })}
    </div>`;
}

export async function render(host) {
  const lists = await api("/playlists");
  if (!lists.length) {
    host.innerHTML = `<div class="card">${emptyState(
      "🎵", "No playlists yet", "A playlist is created per station as songs are matched.",
      `<a class="btn primary" href="#/stations">Add a station</a>`)}</div>`;
    paintIcons(host);
    return;
  }
  const cards = await Promise.all(lists.map(stationCard));
  host.innerHTML = cards.join("");
  paintIcons(host);
}

/* --------------------------------------------------------------- actions -- */

export const actions = {
  async sync(el, d) {
    try {
      await withBusy(el, "Syncing…", async () => {
        const res = await api(`/playlists/sync?station_id=${d.station}`, { method: "POST" });
        const r = res.results[0];
        const failed = r.spotify && !r.spotify.ok;
        toast(failed ? r.spotify.reason
          : `Synced — ${num(r.spotify?.added ?? 0)} added to Spotify, `
            + `${num(r.m3u?.entries ?? 0)} in the M3U`,
          failed ? "bad" : "ok");
      });
    } catch (e) { toast(e.message, "bad"); }
    return render($("#view-playlists"));
  },

  async dedupe(el, d) {
    // Says what it will do before it does it: the tracks it cleans up lose
    // their place in the playlist, and that is not something to discover after.
    const ok = await confirmDialog({
      title: "Remove duplicate tracks?",
      body: "Each duplicated track is removed and added back once, so those tracks move to "
          + "the <strong>end</strong> of the Spotify playlist. Tracks that appear only once "
          + "are left alone.",
      confirmLabel: "Remove duplicates",
    });
    if (!ok) return;
    try {
      await withBusy(el, "Cleaning…", async () => {
        const res = await api(`/playlists/${d.station}/dedupe`, { method: "POST" });
        toast(res.ok
          ? (res.removed ? `Removed ${num(res.removed)} duplicate track(s)` : "No duplicates found")
          : res.reason, res.ok ? "ok" : "bad");
      });
    } catch (e) { toast(e.message, "bad"); }
    return render($("#view-playlists"));
  },
};
