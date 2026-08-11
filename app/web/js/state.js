/* Everything the interface remembers between renders.

   Views are re-rendered from scratch rather than diffed, so anything that must
   survive a render — which page you were on, which filter chip is lit, which
   card of the queue you are working — lives here rather than in the DOM. */

export const state = {
  view: "dashboard",
  stats: null,

  // review
  reviewIndex: 0,
  reviewQueue: [],
  candidatesShown: 0,     // grows when "show more" is used; reset per card
  searchProviders: null,  // per-provider report from the last manual search
  archivedCount: 0,

  // library
  library: { status: "", q: "", sort: "recent", page: 1 },

  // stations / playlists
  stationsPage: 1,
  playlistPages: {},      // station id -> page number

  // settings
  settingsTab: "matching",
  settingsDirty: false,

  timer: null,
};

/** How many rows each paginated table shows. */
export const PAGE = { library: 25, stations: 10, playlist: 10 };

/* Providers currently refusing calls. /api/stats carries the whole roster with
   its labels, so nothing here needs to know how many catalogues there are or
   what any of them is called — a provider added server-side shows up, and can
   be resumed, without touching the interface. */
export const coldProviders = (stats) =>
  (stats?.providers || []).filter((p) => p.throttled);

/* Outbound request rate, summed. Every provider reports requests_1m alongside
   its breaker state, so this needs no roster of its own either. */
export const providerCalls = (stats) =>
  (stats?.providers || []).reduce((n, p) => n + (p.requests_1m || 0), 0);
