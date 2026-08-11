/* The one place the browser talks to the server.

   Every failure arrives as an Error carrying the API's own `detail` string,
   because that text is written for the user — "Spotify is paused for 8s" is a
   better toast than "Bad Gateway". */

export async function api(path, options = {}) {
  const res = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText || `Request failed (${res.status})`;
    try { detail = (await res.json()).detail || detail; } catch { /* non-JSON error */ }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

/** Build a query string from an object, dropping empty values. */
export function qs(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === "" || value === null || value === undefined) continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}
