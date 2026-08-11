/* Holiday Radio Matcher — application shell.

   Plain ES modules, no build step: what ships in the image is what runs.

   The shell owns four things and nothing else:
     · routing        the hash is the source of truth, so every view is a link
     · dispatch       one delegated listener per event type, resolved against
                      the active view's action map
     · chrome         nav state, worker state, theme, keyboard shortcuts
     · the poll timer which pauses while the tab is hidden

   A view module exports `meta`, `render(host)`, and optionally `actions`,
   `changes` and `onKey`. Adding one is a file and a line in VIEWS. */

import { api } from "./js/api.js";
import { applyParams, parseHash, syncHash } from "./js/router.js";
import { closeModal, openModal, toast, withBusy } from "./js/ui.js";
import { $, $$, debounce, paintIcons } from "./js/util.js";
import { coldProviders, state } from "./js/state.js";

import * as dashboard from "./js/views/dashboard.js";
import * as review from "./js/views/review.js";
import * as library from "./js/views/library.js";
import * as playlists from "./js/views/playlists.js";
import * as stations from "./js/views/stations.js";
import * as settings from "./js/views/settings.js";

const VIEWS = { dashboard, review, library, playlists, stations, settings };

const POLL_MS = 15000;

/* ------------------------------------------------------------------ shell -- */

function setChrome(view) {
  const { title, sub } = VIEWS[view].meta;
  $("#view-title").textContent = title;
  $("#view-sub").textContent = sub;
  document.title = `${title} · Holiday Radio Matcher`;
  $$("#nav .nav-item").forEach((a) => {
    if (a.dataset.view === view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
}

async function show(view, { force = false, data = null } = {}) {
  const host = $(`#view-${view}`);
  const changed = state.view !== view;
  state.view = view;

  setChrome(view);
  $$(".view").forEach((v) => v.classList.toggle("hidden", v !== host));
  closeNav();

  if (!host.innerHTML.trim() || changed || force) {
    if (!host.innerHTML.trim()) host.innerHTML = `<div class="empty"><span class="spinner"></span></div>`;
    try {
      await VIEWS[view].render(host, data ? { data } : undefined);
    } catch (err) {
      host.innerHTML = `<div class="card"><div class="empty">
        <div class="glyph">⚠️</div><strong>Could not load this view</strong>
        <p>${err.message}</p></div></div>`;
    }
  }
  paintIcons(host);
  syncHash();
}

/** Re-render whatever is showing, from scratch. */
const reload = () => show(state.view, { force: true });

/* ------------------------------------------------------------ live stats -- */

/* The one piece of chrome that updates without a render: the queue badge, the
   worker light and the version. */
function applyStats(stats) {
  state.stats = stats;

  const waiting = stats.counts.review + stats.counts.unmatched;
  const pill = $("#pill-review");
  pill.textContent = waiting > 999 ? "999+" : waiting;
  pill.dataset.zero = waiting === 0 ? "1" : "0";

  const cold = coldProviders(stats).map((p) => p.label);
  const worker = $("#worker-state");
  const busy = stats.worker.polling || stats.worker.matching;
  worker.classList.toggle("busy", !!busy && !cold.length);
  worker.classList.toggle("cold", cold.length > 0);
  worker.querySelector(".label").textContent = cold.length ? `${cold.join(" & ")} paused`
    : stats.worker.matching ? "Matching…"
    : stats.worker.polling ? "Polling…" : "Idle";

  $("#app-version").textContent = `v${stats.version}`;
  applyHoliday(stats);
}

/* The app wears whichever holiday it is mostly monitoring. The tally rides along
   with the stats it is already fetching — it is read from the stations
   themselves rather than configured anywhere, so adding a Christmas network is
   all it takes to turn the interface red and green, and re-labelling a station
   is enough to change it back. */
function applyHoliday(stats) {
  const top = Object.entries(stats.holidays || {}).sort((a, b) => b[1] - a[1])[0];
  if (top) document.documentElement.dataset.holiday = top[0];
}

/* Nothing is being looked at while the tab is hidden, so nothing is spent on
   it — the poll resumes the moment it comes back into view. */
async function poll() {
  if (document.hidden) return;
  try {
    if (state.view === "dashboard") {
      await dashboard.render($("#view-dashboard"), { quiet: true });
      applyStats(state.stats);
    } else {
      applyStats(await api("/stats"));
    }
  } catch { /* transient poll failure; the next tick retries */ }
}

/* --------------------------------------------------------------- chrome -- */

const THEMES = ["system", "light", "dark"];
const THEME_ICON = { system: "auto", light: "sun", dark: "moon" };

function applyTheme(pref) {
  const root = document.documentElement;
  root.dataset.themePref = pref;
  root.dataset.theme = pref === "system"
    ? (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark")
    : pref;
  const button = $("#btn-theme");
  button.querySelector("i").dataset.icon = THEME_ICON[pref];
  button.title = `Theme: ${pref}`;
  paintIcons(button);
  try { localStorage.setItem("hrm-theme", pref); } catch { /* private mode */ }
}

matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
  if (document.documentElement.dataset.themePref === "system") applyTheme("system");
});

const openNav = () => {
  document.documentElement.dataset.nav = "open";
  $(".nav-open")?.setAttribute("aria-expanded", "true");
};
const closeNav = () => {
  delete document.documentElement.dataset.nav;
  $(".nav-open")?.setAttribute("aria-expanded", "false");
};

const SHORTCUTS = [
  ["Navigation", [
    ["g then d", "Dashboard"], ["g then r", "Review queue"], ["g then l", "Library"],
    ["g then p", "Playlists"], ["g then t", "Stations"], ["g then s", "Settings"],
  ]],
  ["Anywhere", [
    ["r", "Refresh this view"], ["/", "Focus the search box"],
    ["?", "This list"], ["Esc", "Close a dialog or the menu"],
  ]],
  ["Review queue", review.shortcuts],
];

function showShortcuts() {
  openModal(`
    <div class="modal-body">
      <h2>Keyboard shortcuts</h2>
      <div class="shortcuts" style="margin-top:1rem">
        ${SHORTCUTS.map(([group, rows]) => `
          <section>
            <h3>${group}</h3>
            <dl>${rows.map(([keys, what]) => `
              <dt>${keys.split(" then ").map((k) => `<span class="kbd">${k}</span>`).join(" ")}</dt>
              <dd>${what}</dd>`).join("")}</dl>
          </section>`).join("")}
      </div>
    </div>
    <div class="modal-foot"><button class="btn primary" data-act="close-modal">Close</button></div>`);
}

/* --------------------------------------------------------- shell actions -- */

const shellActions = {
  refresh: async (el) => {
    el?.classList.add("spinning");
    await reload();
    // The dashboard's own payload carries the stats; every other view would
    // otherwise leave the queue badge, the worker light and the holiday accent
    // sitting at whatever the last poll tick said.
    if (state.view !== "dashboard") {
      try { applyStats(await api("/stats")); } catch { /* the view still redrew */ }
    }
    el?.classList.remove("spinning");
    toast("Refreshed");
  },

  theme: () => {
    const next = THEMES[(THEMES.indexOf(document.documentElement.dataset.themePref) + 1) % THEMES.length];
    applyTheme(next);
  },

  rail: () => {
    const root = document.documentElement;
    if (root.dataset.rail === "1") delete root.dataset.rail;
    else root.dataset.rail = "1";
    try { localStorage.setItem("hrm-rail", root.dataset.rail === "1" ? "1" : "0"); } catch { /* */ }
  },

  "nav-open": openNav,
  "nav-close": closeNav,
  shortcuts: showShortcuts,
  "close-modal": () => closeModal(),

  async "sync-all"(el) {
    try {
      await withBusy(el, "Syncing…", async () => {
        const res = await api("/playlists/sync", { method: "POST" });
        const added = res.results.reduce((n, r) => n + (r.spotify?.added || 0), 0);
        const problem = res.results.find((r) => r.spotify && !r.spotify.ok);
        toast(problem ? problem.spotify.reason : `Sync complete — ${added} track(s) added`,
          problem ? "bad" : "ok");
      });
    } catch (e) { toast(e.message, "bad"); }
  },

  /* Pagination spans three views, so the key says which one it belongs to. */
  page(el, d) {
    const target = Math.max(1, +d.target);
    if (d.key === "lib") {
      state.library.page = target;
      syncHash();
      return library.render($("#view-library"));
    }
    if (d.key === "st") {
      state.stationsPage = target;
      return stations.render($("#view-stations"));
    }
    if (d.key.startsWith("pl-")) {
      state.playlistPages[d.key.slice(3)] = target;
      return playlists.render($("#view-playlists"));
    }
  },
};

/* -------------------------------------------------------------- dispatch -- */

document.addEventListener("click", async (ev) => {
  const el = ev.target.closest("[data-act]");
  if (!el) return;

  const name = el.dataset.act;
  const handler = shellActions[name] || VIEWS[state.view]?.actions?.[name];
  if (!handler) return;

  ev.preventDefault();
  try {
    await handler(el, el.dataset);
  } catch (err) {
    toast(err.message || String(err), "bad");
  }
});

document.addEventListener("change", async (ev) => {
  const el = ev.target;

  if (el.dataset.actChange) {
    const handler = VIEWS[state.view]?.changes?.[el.dataset.actChange];
    if (handler) await handler(el, el.dataset);
    return;
  }
  if (el.id === "lib-sort") {
    state.library.sort = el.value;
    state.library.page = 1;
    syncHash();
    library.render($("#view-library"));
    return;
  }
  if (el.dataset.setting) settings.markDirty();
});

/* Typing re-renders the whole table, which destroys the box being typed into —
   so the caret is put back where it was. */
const searchLibrary = debounce((value) => {
  if (state.view !== "library") return;
  state.library.q = value;
  state.library.page = 1;
  syncHash();
  library.render($("#view-library")).then(() => {
    const box = $("#lib-q");
    if (!box) return;
    box.focus();
    box.setSelectionRange(box.value.length, box.value.length);
  });
}, 300);

document.addEventListener("input", (ev) => {
  if (ev.target.id === "lib-q") return searchLibrary(ev.target.value);
  if (ev.target.dataset.setting) settings.markDirty();
});

/* Correcting a field and pressing Enter is the natural way to run the review
   search; reaching for the button after typing is not. */
let pendingGoto = false;

document.addEventListener("keydown", (ev) => {
  const el = ev.target;
  const typing = el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement
    || el instanceof HTMLSelectElement;

  if (ev.key === "Enter" && (el.id === "ms-artist" || el.id === "ms-title")) {
    ev.preventDefault();
    const button = $('#view-review [data-act="search"]');
    if (button && !button.disabled) review.runSearch(button, +button.dataset.song);
    return;
  }
  if (ev.key === "Escape") {
    if (document.documentElement.dataset.nav === "open") closeNav();
    if (typing) el.blur();
    return;
  }
  if (typing || ev.metaKey || ev.ctrlKey || ev.altKey || $("#modal").open) return;

  // `g` then a letter, the two-key jump every dashboard eventually grows.
  if (pendingGoto) {
    const target = { d: "dashboard", r: "review", l: "library", p: "playlists",
                     t: "stations", s: "settings" }[ev.key];
    pendingGoto = false;
    if (target) { ev.preventDefault(); location.hash = `#/${target}`; }
    return;
  }
  if (ev.key === "g") { pendingGoto = true; setTimeout(() => { pendingGoto = false; }, 1200); return; }

  if (VIEWS[state.view]?.onKey?.(ev)) { ev.preventDefault(); return; }

  if (ev.key === "?") { ev.preventDefault(); showShortcuts(); }
  else if (ev.key === "r") { ev.preventDefault(); shellActions.refresh($("#btn-refresh")); }
  // Hidden views keep their DOM, so a bare #lib-q lookup would happily focus a
  // search box on a tab nobody is looking at.
  else if (ev.key === "/" && state.view === "library") {
    const box = $("#lib-q");
    if (box) { ev.preventDefault(); box.focus(); box.select(); }
  }
});

window.addEventListener("hashchange", () => {
  const { view, params } = parseHash();
  applyParams(view, params);
  show(view, { force: true });
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) poll();
});

/* ------------------------------------------------------------------ boot -- */

(async function boot() {
  paintIcons();
  applyTheme(document.documentElement.dataset.themePref || "system");

  const params = new URLSearchParams(location.search);
  if (params.get("spotify") === "linked") toast("Spotify connected", "ok");
  if (params.get("spotify") === "error") toast(`Spotify link failed: ${params.get("reason")}`, "bad");
  if (params.has("spotify")) history.replaceState({}, "", location.pathname + location.hash);

  const route = parseHash();
  applyParams(route.view, route.params);

  // Landing on the dashboard, its own payload already carries the stats the
  // chrome needs — so boot is one request, not two.
  let seed = null;
  try {
    seed = route.view === "dashboard" ? await dashboard.load() : { stats: await api("/stats") };
    applyStats(seed.stats);
  } catch (e) {
    seed = null;
    toast(`Could not reach the server: ${e.message}`, "bad");
  }

  await show(route.view, { force: true, data: seed?.np ? seed : null });
  state.timer = setInterval(poll, POLL_MS);
})();
