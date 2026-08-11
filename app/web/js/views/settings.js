/* Settings — grouped into sections rather than stacked into one long column.

   Every input carries `data-setting="<key>"`, so saving is a sweep of the view
   rather than a hand-written object that quietly forgets a field the day one is
   added. All sections stay in the DOM and are only hidden, which means switching
   tabs never discards an edit and Save always sees every field. */

import { api } from "../api.js";
import { syncHash } from "../router.js";
import { confirmDialog, toast, withBusy } from "../ui.js";
import { $, $$, esc, paintIcons } from "../util.js";
import { state } from "../state.js";

export const meta = {
  title: "Settings",
  sub: "Matching thresholds, catalogues, delivery and your learned rules",
};

const TABS = [
  ["matching", "Matching"],
  ["delivery", "Polling & delivery"],
  ["pacing", "Catalogue pacing"],
  ["spotify", "Spotify"],
  ["rules", "Learned rules"],
];

/* Per-provider pacing. Keys, labels and copy in one table, because the four
   differ only in their numbers and their reasons. */
const PACING = [
  {
    key: "musicbrainz", label: "MusicBrainz",
    rate: "Minimum gap between requests. MusicBrainz allows about one per second.",
    cool: "Pause after a 503/429. It never says how long to wait, so this escalates to 3×, 10× and 30×.",
  },
  {
    key: "spotify", label: "Spotify",
    rate: "Minimum gap. The quota is a rolling window, so it is the burst that gets refused, not the daily total.",
    cool: "Only a floor — Spotify states its own wait and that is honoured in full when it is longer. Escalates to 2×, 4×, 8×.",
  },
  {
    key: "deezer", label: "Deezer",
    rate: "Minimum gap. Deezer allows roughly 50 requests every 5 seconds.",
    cool: "Its quota window is only five seconds wide, so a long pause would be dead time. Escalates to 2×, 4×, 8×.",
  },
  {
    key: "itunes", label: "Apple Music",
    rate: "Minimum gap. The store allows about 20 requests a minute per IP.",
    cool: "Pause after a 403. Apple names no delay either, so this escalates to 3×, 10× and 30×.",
  },
];

const CATALOGUES = [
  ["use_musicbrainz", "MusicBrainz"], ["use_spotify", "Spotify"],
  ["use_deezer", "Deezer"], ["use_itunes", "Apple Music"],
];

/* ------------------------------------------------------------- fragments -- */

const numberField = (key, label, hint, value, attrs = "") => `
  <div class="field">
    <label for="s-${key}">${esc(label)}</label>
    <input type="number" id="s-${key}" data-setting="${key}" value="${esc(value)}" ${attrs}>
    ${hint ? `<span class="hint">${hint}</span>` : ""}
  </div>`;

const switchRow = (key, label, note, on) => `
  <div class="switch-row">
    <div class="copy"><b>${esc(label)}</b>${note ? `<span class="hint">${note}</span>` : ""}</div>
    <label class="switch"><span class="sr-only">${esc(label)}</span>
      <input type="checkbox" data-setting="${key}" ${on ? "checked" : ""}></label>
  </div>`;

const section = (id, inner) =>
  `<div class="grid" data-section="${id}" ${state.settingsTab === id ? "" : "hidden"}>${inner}</div>`;

/* ------------------------------------------------------------- rendering -- */

export async function render(host) {
  const [data, redirect] = await Promise.all([
    api("/settings"), api("/spotify/redirect-uri"),
  ]);
  const v = data.values;
  const sp = data.spotify;
  state.settingsDirty = false;

  const matching = section("matching", `
    <div class="card">
      <div class="card-head"><div>
        <h2>Confidence thresholds</h2>
        <p class="sub">A higher auto-accept means fewer mistakes and more manual review.</p>
      </div></div>
      <div class="form-grid">
        ${numberField("auto_accept_score", "Auto-accept at or above",
          "0.92 is a good balance. Raise it if you ever see a wrong match reach a playlist.",
          v.auto_accept_score, 'step="0.01" min="0.5" max="1"')}
        ${numberField("review_floor_score", "Send to review at or above",
          "Below this, a song is listed as unmatched rather than queued with candidates.",
          v.review_floor_score, 'step="0.01" min="0" max="1"')}
        ${numberField("min_song_seconds", "Minimum song length (seconds)",
          "Anything shorter is treated as a jingle or station ID.", v.min_song_seconds, 'min="0"')}
      </div>
    </div>

    <div class="card">
      <div class="card-head"><div>
        <h2>Catalogues to search</h2>
        <p class="sub">MusicBrainz identifies and Spotify makes it playable. Deezer and Apple Music
          are there for the songs neither of those two carries — they need no account, and turning
          them off only costs coverage. Agreement between catalogues raises confidence, so more of
          them on means fewer songs stuck in review.</p>
      </div></div>
      <div class="grid duo">
        ${CATALOGUES.map(([key, label]) =>
          switchRow(key, label, "", v[key] === "1")).join("")}
      </div>
    </div>`);

  const delivery = section("delivery", `
    <div class="card">
      <div class="card-head"><div>
        <h2>Polling</h2>
        <p class="sub">How often each station is read.</p></div></div>
      <div class="form-grid">
        ${numberField("poll_interval_seconds", "Poll interval (seconds)",
          "AzuraCast play history is 15 deep, so 45–120s misses nothing.",
          v.poll_interval_seconds, 'min="10"')}
        <div class="field">
          <label for="s-spotify_market">Spotify market</label>
          <input type="text" id="s-spotify_market" data-setting="spotify_market"
                 data-transform="upper" maxlength="2" value="${esc(v.spotify_market)}">
          <span class="hint">Two-letter country code. Affects which release a search returns.</span>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><div>
        <h2>Delivery</h2>
        <p class="sub">Where matched songs are written.</p></div></div>
      <div class="grid duo">
        ${switchRow("m3u_enabled", "Write .m3u8 files",
          "One extended M3U per station, rewritten atomically.", v.m3u_enabled === "1")}
        ${switchRow("spotify_sync_enabled", "Sync to Spotify",
          "One private playlist per station. Needs the account link.", v.spotify_sync_enabled === "1")}
      </div>
      <p class="hint" style="margin-top:1rem">
        Playlists folder: <code>${esc(data.playlist_dir.path)}</code>
        ${data.playlist_dir.writable
          ? `<span style="color:var(--ok)"> · writable</span>`
          : `<span style="color:var(--bad)"> · not writable: ${esc(data.playlist_dir.error)}</span>`}
      </p>
      <p class="hint">Database and settings: <code>${esc(data.config_dir)}</code></p>
    </div>`);

  const pacing = section("pacing", `
    <div class="card">
      <div class="card-head"><div>
        <h2>Staying inside the providers' limits</h2>
        <p class="sub">These have sensible defaults and are worth touching only if a service is
          persistently unhappy with you. Raising a <em>cooldown</em> is the right response to
          persistent throttling; lowering a <em>rate limit</em> is not, and will get you blocked —
          for Spotify that means the whole application, not just this machine.</p>
      </div></div>
      <div class="grid">
        ${PACING.map((p) => `
          <div class="form-grid" style="border-top:1px solid var(--border-soft);padding-top:1rem">
            ${numberField(`${p.key}_rate_limit_seconds`, `${p.label} — rate limit (s)`,
              p.rate, v[`${p.key}_rate_limit_seconds`], 'step="0.1" min="0"')}
            ${numberField(`${p.key}_cooldown_seconds`, `${p.label} — cooldown (s)`,
              p.cool, v[`${p.key}_cooldown_seconds`], 'min="1"')}
          </div>`).join("")}
      </div>
    </div>`);

  const spotify = section("spotify", `
    <div class="card">
      <div class="card-head"><div>
        <h2>Spotify credentials</h2>
        <p class="sub">Search works with just the ID and secret. Creating playlists needs the
          account link as well.</p></div></div>
      <div class="form-rows">
        <div class="field"><label for="s-spotify_client_id">Client ID</label>
          <input type="text" id="s-spotify_client_id" data-setting="spotify_client_id"
                 value="${esc(v.spotify_client_id)}" autocomplete="off"></div>
        <div class="field"><label for="s-spotify_client_secret">Client secret</label>
          <input type="password" id="s-spotify_client_secret" data-setting="spotify_client_secret"
                 value="${esc(v.spotify_client_secret)}" autocomplete="off"></div>
        <div class="field">
          <label for="s-spotify_redirect_uri">Redirect URI — must match your Spotify app exactly</label>
          <input type="text" id="s-spotify_redirect_uri" data-setting="spotify_redirect_uri"
                 value="${esc(redirect.redirect_uri)}" onclick="this.select()">
          ${redirect.warning
            ? `<span class="hint warn">⚠ ${esc(redirect.warning)}</span>
               <button class="btn sm" data-act="use-loopback" style="margin-top:.4rem;align-self:start">
                 Use ${esc(redirect.suggested_loopback)}</button>`
            : `<span class="hint ok">✓ Spotify accepts this form.</span>`}
        </div>
      </div>

      <div class="row tight center" style="margin-top:1.25rem">
        ${sp.linked
          ? `<span class="badge matched">Connected as ${esc(sp.user || "user")}</span>
             <button class="btn" data-act="diagnose">Test Spotify access</button>
             <button class="btn" data-act="unlink">Disconnect</button>`
          : `<button class="btn primary" data-act="link" ${sp.configured ? "" : "disabled"}>
               <i class="ic" data-icon="link"></i>Connect Spotify account</button>`}
      </div>

      ${sp.missing_scopes?.length ? `<p class="hint warn" style="margin-top:.75rem">
        ⚠ This link is missing ${esc(sp.missing_scopes.join(", "))}. Disconnect and reconnect
        to grant it.</p>` : ""}
      <div id="sp-diag"></div>

      ${!sp.configured ? `<p class="hint" style="margin-top:.75rem">
        Create a free app at <a href="https://developer.spotify.com/dashboard" target="_blank"
        rel="noopener">developer.spotify.com</a> with <strong>Web API</strong> ticked, then paste
        the ID and secret above and save.</p>` : ""}

      ${!sp.linked && sp.configured ? `
        <div style="margin-top:1.25rem;padding-top:1.25rem;border-top:1px solid var(--border-soft)">
          <div class="field">
            <label for="sp-paste">Finish linking by pasting the URL you landed on</label>
            <p class="hint" style="margin:0 0 .5rem">After approving, your browser lands on the
              redirect address. If that page fails to load, that is expected — copy the whole
              address from the bar and paste it here.</p>
            <div class="row tight">
              <input type="text" id="sp-paste" style="flex:1 1 300px"
                     placeholder="http://127.0.0.1:8080/api/spotify/callback?code=…">
              <button class="btn primary" data-act="exchange">Finish</button>
            </div>
          </div>
        </div>` : ""}
    </div>`);

  const rules = section("rules", `
    <div class="card">
      <div class="card-head"><div>
        <h2>Learned rules</h2>
        <p class="sub">Every confirmation you make becomes a permanent rule, so that metadata
          resolves instantly and offline forever after. Deleting one sends the song back through
          matching next time it airs.</p></div></div>
      <div id="alias-list" class="rules"><span class="spinner"></span></div>
    </div>`);

  host.innerHTML = `
    <div class="segmented" role="tablist" aria-label="Settings sections">
      ${TABS.map(([id, label]) => `
        <button role="tab" data-act="tab" data-tab="${id}"
          aria-selected="${state.settingsTab === id}">${label}</button>`).join("")}
    </div>
    ${matching}${delivery}${pacing}${spotify}${rules}
    <div class="save-bar" id="save-bar">
      <span class="status">All changes saved</span>
      <div class="row tight">
        <button class="btn" data-act="revert">Revert</button>
        <button class="btn primary" data-act="save">Save settings</button>
      </div>
    </div>`;

  paintIcons(host);
  loadAliases();
}

async function loadAliases() {
  const box = $("#alias-list");
  if (!box) return;
  try {
    const aliases = await api("/aliases?limit=60");
    // A 'song' rule carries no identity — it only stops the non-song filter
    // catching this metadata — so there is no match to print on the right of it.
    const target = (a) => a.kind === "nonsong" ? "<em>not a song</em>"
      : a.kind === "song" ? "<em>always treated as music</em>"
      : esc(`${a.match_artist} — ${a.match_title}`);
    box.innerHTML = aliases.length ? aliases.map((a) => `
      <div class="rule">
        <span class="hits">${a.hits}×</span>
        <span class="body">${esc(a.key_artist)} — ${esc(a.key_title)} → ${target(a)}</span>
        <button class="icon-btn plain sm" data-act="delete-rule" data-rule="${a.id}"
                aria-label="Delete rule"><i class="ic" data-icon="x"></i></button>
      </div>`).join("")
      : `<p class="hint">No rules yet. They appear as you confirm matches.</p>`;
    paintIcons(box);
  } catch (e) {
    box.innerHTML = `<p class="hint warn">${esc(e.message)}</p>`;
  }
}

/* Marks the save bar so a page of edits is never silently abandoned. */
export function markDirty() {
  state.settingsDirty = true;
  const bar = $("#save-bar");
  if (!bar) return;
  bar.classList.add("dirty");
  bar.querySelector(".status").textContent = "Unsaved changes";
}

/* --------------------------------------------------------------- actions -- */

export const actions = {
  tab(el, d) {
    state.settingsTab = d.tab;
    syncHash();
    $$("#view-settings [role=tab]").forEach((t) =>
      t.setAttribute("aria-selected", String(t.dataset.tab === d.tab)));
    $$("#view-settings [data-section]").forEach((s) =>
      s.toggleAttribute("hidden", s.dataset.section !== d.tab));
  },

  async save(el) {
    const values = {};
    for (const input of $$("#view-settings [data-setting]")) {
      const key = input.dataset.setting;
      if (input.type === "checkbox") values[key] = input.checked ? "1" : "0";
      else if (input.dataset.transform === "upper") values[key] = input.value.trim().toUpperCase();
      else values[key] = input.value.trim();
    }
    try {
      await withBusy(el, "Saving…", () => api("/settings", { method: "PUT", body: { values } }));
      toast("Settings saved", "ok");
      return render($("#view-settings"));
    } catch (e) { toast(e.message, "bad"); }
  },

  revert: () => render($("#view-settings")),

  async "use-loopback"() {
    const { suggested_loopback } = await api("/spotify/redirect-uri");
    await api("/settings", {
      method: "PUT", body: { values: { spotify_redirect_uri: suggested_loopback } },
    });
    toast("Redirect URI set — add the same value in your Spotify app", "ok");
    return render($("#view-settings"));
  },

  async link() {
    try {
      const { url, warning } = await api("/spotify/login");
      if (warning) {
        const go = await confirmDialog({
          title: "Spotify may refuse this redirect",
          body: esc(warning), confirmLabel: "Open Spotify anyway",
        });
        if (!go) return;
      }
      // Opened in a new tab so this page stays put to receive the pasted URL.
      window.open(url, "_blank", "noopener");
      toast("Approve in the new tab, then paste the address you land on", "ok");
    } catch (e) { toast(e.message, "bad"); }
  },

  async exchange(el) {
    try {
      await withBusy(el, "Linking…", async () => {
        const res = await api("/spotify/exchange", {
          method: "POST", body: { value: $("#sp-paste").value },
        });
        toast(`Spotify connected as ${res.user || "user"}`, "ok");
      });
      return render($("#view-settings"));
    } catch (e) { toast(e.message, "bad"); }
  },

  async unlink(el) {
    const ok = await confirmDialog({
      title: "Disconnect Spotify?",
      body: "Playlists already on Spotify are left alone. Matching keeps working, but new "
          + "tracks stop being delivered until you reconnect.",
      confirmLabel: "Disconnect", danger: true,
    });
    if (!ok) return;
    await api("/spotify/unlink", { method: "POST" });
    toast("Spotify disconnected");
    return render($("#view-settings"));
  },

  async diagnose(el) {
    const box = $("#sp-diag");
    box.innerHTML = `<div class="diag"><p class="hint"><span class="spinner"></span>
      Checking — this creates and removes one temporary playlist.</p></div>`;
    try {
      await withBusy(el, "Checking…", async () => {
        const r = await api("/spotify/diagnose", { method: "POST" });
        box.innerHTML = `<div class="diag">
          ${r.checks.map((c) => `
            <div class="chk ${c.ok ? "pass" : "fail"}">
              <span class="mark">${c.ok ? "✓" : "✕"}</span>
              <span>${esc(c.name)}${c.detail ? `<span class="detail"> — ${esc(c.detail)}</span>` : ""}</span>
            </div>`).join("")}
          <div class="verdict ${r.ok ? "pass" : "fail"}">${esc(r.hint)}</div>
        </div>`;
      });
    } catch (e) {
      box.innerHTML = `<div class="diag"><div class="verdict fail">${esc(e.message)}</div></div>`;
    }
  },

  async "delete-rule"(el, d) {
    try {
      await api(`/aliases/${d.rule}`, { method: "DELETE" });
      el.closest(".rule")?.remove();
    } catch (e) { toast(e.message, "bad"); }
  },
};
