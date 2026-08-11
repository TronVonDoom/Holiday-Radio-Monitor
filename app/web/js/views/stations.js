/* Stations — the streams being read, and the two ways to add another. */

import { api } from "../api.js";
import {
  confirmDialog, emptyState, fillerRows, pager, toast, withBusy,
} from "../ui.js";
import { $, esc, fmtStamp, fmtTime, icon, num, paintIcons } from "../util.js";
import { PAGE, state } from "../state.js";

export const meta = { title: "Stations", sub: "Streams being monitored" };

const HOLIDAYS = [
  ["halloween", "Halloween"], ["christmas", "Christmas"],
  ["winter", "Winter"], ["generic", "Other"],
];

const holidaySelect = (id, current) => `
  <select data-act-change="holiday" data-station="${id}" aria-label="Holiday"
          style="width:auto;min-width:120px;font-size:.82rem;padding:.25rem 1.7rem .25rem .5rem">
    ${HOLIDAYS.map(([v, l]) =>
      `<option value="${v}"${current === v ? " selected" : ""}>${l}</option>`).join("")}
  </select>`;

export async function render(host) {
  const stations = await api("/stations");
  const size = PAGE.stations;
  const pages = Math.max(1, Math.ceil(stations.length / size));
  const page = Math.min(Math.max(1, state.stationsPage), pages);
  state.stationsPage = page;

  const shown = stations.slice((page - 1) * size, page * size);
  const rows = shown.map((s) => `
    <tr data-holiday="${esc(s.holiday)}">
      <td class="squeeze">
        <div class="track"><div class="ph" style="width:32px;height:32px">${icon("radio", 16)}</div>
          <div class="t"><b>${esc(s.name)}</b>
            <span>${esc(s.azuracast_shortcode || s.icy_url || "—")}</span></div></div>
      </td>
      <td class="fit">${holidaySelect(s.id, s.holiday)}</td>
      <td class="num dim">${num(s.plays)}</td>
      <td class="num dim">${num(s.playlist_count)}</td>
      <td class="num nowrap ${s.last_error ? "" : "dim"}">${s.last_error
        ? `<span class="badge unmatched" title="${esc(s.last_error)}">error</span>`
        : `<span title="${esc(fmtStamp(s.last_polled_at))}">${fmtTime(s.last_polled_at)}</span>`}</td>
      <td class="num fit">
        <label class="switch" title="${s.enabled ? "Monitoring" : "Paused"}">
          <span class="sr-only">Monitor ${esc(s.name)}</span>
          <input type="checkbox" data-act-change="toggle" data-station="${s.id}"
                 ${s.enabled ? "checked" : ""}></label>
      </td>
      <td class="num fit">
        <div class="row tight center nowrap" style="justify-content:flex-end">
          <button class="btn sm" data-act="poll" data-station="${s.id}"
                  title="Read this station now"><i class="ic" data-icon="refresh"></i></button>
          <button class="btn danger sm" data-act="remove-station" data-station="${s.id}"
                  data-name="${esc(s.name)}" title="Remove station">
            <i class="ic" data-icon="trash"></i></button>
        </div>
      </td>
    </tr>`).join("");

  host.innerHTML = `
    <div class="card flush">
      <div class="card-head">
        <div><h2>Monitored stations</h2>
          <p class="sub">The poller reads each enabled station on the interval set in Settings.</p></div>
        <span class="badge holiday">${
          num(stations.filter((s) => s.enabled).length)} monitored</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th>Station</th><th>Holiday</th><th class="num">Plays</th>
            <th class="num">Playlist</th><th class="num">Last poll</th>
            <th class="num">On</th><th class="num"></th>
          </tr></thead>
          <tbody>
            ${rows || `<tr><td colspan="7">${emptyState("📡", "No stations configured",
              "Discover a server below, or add a stream by hand.")}</td></tr>`}
            ${fillerRows(shown.length, size, stations.length, 7)}
          </tbody>
        </table>
      </div>
      ${pager("st", { total: stations.length, page, size, unit: "station" })}
    </div>

    <div class="grid duo">
      <div class="card">
        <div class="card-head"><div>
          <h2>Discover from a server</h2>
          <p class="sub">Point this at an AzuraCast server to list every station it hosts,
            then add them in one click.</p></div></div>
        <div class="row">
          <div class="field">
            <label for="disc-url">Server URL</label>
            <input type="text" id="disc-url" value="https://radio1.streamserver.link">
            <span class="hint">The server address, not a stream link — though a stream URL is
              trimmed back to the server automatically.</span>
          </div>
          <button class="btn primary" data-act="discover">
            <i class="ic" data-icon="search"></i>Discover</button>
        </div>
        <div id="disc-results" style="margin-top:1rem"></div>
      </div>

      <div class="card">
        <div class="card-head"><div>
          <h2>Add manually</h2>
          <p class="sub">For any other Icecast or SHOUTcast stream.</p></div></div>
        <div class="form-rows">
          <div class="field"><label for="st-name">Name</label>
            <input type="text" id="st-name" placeholder="Halloween Radio Oldies"></div>
          <div class="row">
            <div class="field"><label for="st-holiday">Holiday</label>
              <select id="st-holiday">${HOLIDAYS.map(([v, l]) =>
                `<option value="${v}">${l}</option>`).join("")}</select></div>
            <div class="field" style="flex:2 1 260px"><label for="st-icy">Stream URL (ICY)</label>
              <input type="text" id="st-icy" placeholder="https://host:8000/mount"></div>
          </div>
          <div class="row tight">
            <button class="btn" data-act="probe">Test</button>
            <button class="btn primary" data-act="add-station">
              <i class="ic" data-icon="plus"></i>Add station</button>
          </div>
          <div id="st-probe-result" class="hint"></div>
        </div>
      </div>
    </div>`;

  paintIcons(host);
}

/* --------------------------------------------------------------- actions -- */

/* Guess a holiday from the station's own name. Wrong guesses are one dropdown
   away on the row above, which is cheaper than asking on every add. */
function guessHoliday(name) {
  const n = name.toLowerCase();
  if (n.includes("christmas") || n.includes("xmas") || n.includes("holiday")) return "christmas";
  if (n.includes("halloween") || n.includes("horror")) return "halloween";
  if (n.includes("snow") || n.includes("winter")) return "winter";
  return "generic";
}

export const actions = {
  async discover(el) {
    const box = $("#disc-results");
    try {
      await withBusy(el, "Looking…", async () => {
        const typed = $("#disc-url").value.trim();
        const res = await api("/stations/discover", { method: "POST", body: { base_url: typed } });
        // Say so when the pasted address was trimmed back to the server root.
        const note = res.base && res.base !== typed.replace(/\/+$/, "")
          ? `<p class="hint" style="margin-bottom:.6rem">Found ${res.count} station(s) on
             <code>${esc(res.base)}</code></p>` : "";
        box.innerHTML = note + (res.stations.map((s) => `
          <div class="cand">
            <div class="ph">${icon("radio", 18)}</div>
            <div class="info"><b>${esc(s.name)}</b>
              <div class="line2">${esc(s.description || s.shortcode)}</div></div>
            <div class="act">${s.already_added
              ? `<span class="badge matched">added</span>`
              : `<button class="btn primary sm" data-act="add-discovered" data-payload='${
                  esc(JSON.stringify({
                    name: s.name, azuracast_base: s.azuracast_base,
                    azuracast_shortcode: s.shortcode, icy_url: s.listen_url,
                  }))}'>Add</button>`}</div>
          </div>`).join("") || `<p class="hint">No stations found.</p>`);
      });
    } catch (e) {
      box.innerHTML = "";
      toast(e.message, "bad");
    }
  },

  async "add-discovered"(el, d) {
    const payload = JSON.parse(d.payload);
    payload.holiday = guessHoliday(payload.name);
    try {
      await api("/stations", { method: "POST", body: payload });
      toast(`Added ${payload.name}`, "ok");
      el.outerHTML = `<span class="badge matched">added</span>`;
    } catch (e) { toast(e.message, "bad"); }
  },

  async probe(el) {
    const box = $("#st-probe-result");
    box.innerHTML = `<span class="spinner"></span> Reading the stream…`;
    try {
      const res = await api("/stations/probe", {
        method: "POST", body: { icy_url: $("#st-icy").value },
      });
      box.className = res.ok ? "hint ok" : "hint warn";
      box.innerHTML = res.ok
        ? `✓ ${esc(res.source)} — now playing:
           <strong>${esc(res.now_playing.artist)} — ${esc(res.now_playing.title)}</strong>`
        : `✕ ${esc(res.error)}`;
    } catch (e) {
      box.className = "hint warn";
      box.textContent = `✕ ${e.message}`;
    }
  },

  async "add-station"(el) {
    const name = $("#st-name").value.trim();
    if (!name) return toast("Give the station a name first", "bad");
    try {
      await withBusy(el, "Adding…", () => api("/stations", {
        method: "POST",
        body: { name, holiday: $("#st-holiday").value, icy_url: $("#st-icy").value.trim() },
      }));
      toast("Station added", "ok");
      return render($("#view-stations"));
    } catch (e) { toast(e.message, "bad"); }
  },

  async poll(el, d) {
    try {
      await withBusy(el, "", () => api(`/stations/${d.station}/poll`, { method: "POST" }));
      toast("Station polled", "ok");
      return render($("#view-stations"));
    } catch (e) { toast(e.message, "bad"); }
  },

  async "remove-station"(el, d) {
    const ok = await confirmDialog({
      title: `Remove ${d.name}?`,
      body: "Its play history and playlist entries go with it. The Spotify playlist itself is "
          + "left alone.",
      confirmLabel: "Remove station", danger: true,
    });
    if (!ok) return;
    try {
      await api(`/stations/${d.station}`, { method: "DELETE" });
      toast("Station removed");
      return render($("#view-stations"));
    } catch (e) { toast(e.message, "bad"); }
  },
};

/* Change events, for the controls that act the moment they are touched. */
export const changes = {
  async toggle(el, d) {
    try {
      await api(`/stations/${d.station}`, { method: "PATCH", body: { enabled: el.checked } });
      toast(el.checked ? "Station enabled" : "Station paused");
    } catch (e) {
      el.checked = !el.checked;
      toast(e.message, "bad");
    }
  },

  async holiday(el, d) {
    try {
      await api(`/stations/${d.station}`, { method: "PATCH", body: { holiday: el.value } });
      el.closest("tr").dataset.holiday = el.value;
      toast("Holiday updated");
    } catch (e) { toast(e.message, "bad"); }
  },
};
