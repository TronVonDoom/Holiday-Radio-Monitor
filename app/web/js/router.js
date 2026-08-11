/* The address bar as the source of truth.

   `#/library?status=archived&q=elfman` — the view name, then whatever that view
   remembers. Reading it back on load is what makes a filtered library a link
   worth sending to yourself, and what makes the browser's Back button undo a
   filter instead of leaving the app.

   Two directions, deliberately asymmetric:
     parse/apply  a real navigation (a nav link, Back) → state → render
     syncHash     a state change made in place → the URL, via replaceState, so
                  it never fires `hashchange` and never loops back round. */

import { state } from "./state.js";

export const VIEW_NAMES = [
  "dashboard", "review", "library", "playlists", "stations", "settings",
];
const DEFAULT_VIEW = "dashboard";

export function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [name, query = ""] = raw.split("?");
  return {
    view: VIEW_NAMES.includes(name) ? name : DEFAULT_VIEW,
    params: new URLSearchParams(query),
  };
}

export function applyParams(view, params) {
  if (view === "library") {
    state.library = {
      status: params.get("status") || "",
      q: params.get("q") || "",
      sort: params.get("sort") || "recent",
      page: Math.max(1, +params.get("page") || 1),
    };
  } else if (view === "settings") {
    state.settingsTab = params.get("tab") || state.settingsTab;
  }
}

/** Write the current view state back into the address bar without navigating. */
export function syncHash() {
  const params = new URLSearchParams();

  if (state.view === "library") {
    const { status, q, sort, page } = state.library;
    if (status) params.set("status", status);
    if (q) params.set("q", q);
    if (sort !== "recent") params.set("sort", sort);
    if (page > 1) params.set("page", String(page));
  } else if (state.view === "settings" && state.settingsTab !== "matching") {
    params.set("tab", state.settingsTab);
  }

  const query = params.toString();
  const hash = `#/${state.view}${query ? `?${query}` : ""}`;
  if (location.hash !== hash) history.replaceState(null, "", hash);
}
