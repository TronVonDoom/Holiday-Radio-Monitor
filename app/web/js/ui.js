/* Shared interface pieces: notifications, the modal, and the small HTML
   fragments every view builds its rows out of. Keeping them here is what makes
   a badge in the Library and a badge in the Review queue the same badge. */

import { $, esc, icon, num, paintIcons, statusLabel } from "./util.js";

/* --------------------------------------------------------------- toasts -- */

const TOAST_LIFE = { bad: 12000, ok: 4200, "": 4200 };

export function toast(message, kind = "") {
  const host = $("#toasts");
  if (!host) return;

  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML =
    `<i class="ic" data-icon="${kind === "bad" ? "alert" : kind === "ok" ? "check" : "bolt"}"></i>` +
    `<span class="msg"></span>` +
    `<button class="icon-btn plain sm" aria-label="Dismiss"><i class="ic" data-icon="x"></i></button>`;
  el.querySelector(".msg").textContent = message;
  paintIcons(el);

  const dismiss = () => {
    if (!el.isConnected) return;
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 220);
  };
  el.addEventListener("click", dismiss);
  host.append(el);

  // Failures usually carry an explanation worth reading, so they linger.
  setTimeout(dismiss, TOAST_LIFE[kind] ?? TOAST_LIFE[""]);
  return el;
}

/* ---------------------------------------------------------------- modal -- */

/* A native <dialog>, so Escape, the focus trap and the backdrop are the
   browser's job rather than three more things to get subtly wrong. */

export function openModal(html, { onOpen } = {}) {
  const dialog = $("#modal");
  dialog.innerHTML = html;
  paintIcons(dialog);
  if (!dialog.open) dialog.showModal();
  onOpen?.(dialog);
  return dialog;
}

export function closeModal(value = "") {
  const dialog = $("#modal");
  if (dialog?.open) dialog.close(value);
}

/** Replacement for window.confirm that can explain itself in more than a line. */
export function confirmDialog({
  title, body = "", confirmLabel = "Confirm", cancelLabel = "Cancel", danger = false,
}) {
  return new Promise((resolve) => {
    const dialog = openModal(`
      <form method="dialog" class="modal-body">
        <h2>${esc(title)}</h2>
        ${body ? `<p style="margin-top:.5rem">${body}</p>` : ""}
      </form>
      <div class="modal-foot">
        <button class="btn" value="cancel" data-close>${esc(cancelLabel)}</button>
        <button class="btn ${danger ? "danger" : "primary"}" value="ok" data-ok>${esc(confirmLabel)}</button>
      </div>`);

    dialog.querySelector("[data-ok]").focus();
    const finish = (ok) => {
      dialog.removeEventListener("close", onClose);
      resolve(ok);
    };
    const onClose = () => finish(dialog.returnValue === "ok");
    dialog.addEventListener("close", onClose);
    dialog.querySelector("[data-ok]").onclick = () => dialog.close("ok");
    dialog.querySelector("[data-close]").onclick = () => dialog.close("cancel");
  });
}

/* ------------------------------------------------------------ fragments -- */

const placeholder = (cls = "") => `<div class="ph ${esc(cls)}">${icon("music", 18)}</div>`;

/* No inline onerror here: the fallback markup is an SVG full of double quotes,
   which would terminate the attribute and leak the remainder as visible text.
   The capture listener below covers it instead — image load failures do not
   bubble, so nothing else would hear them. */
export const artwork = (url, cls = "") =>
  url ? `<img class="art ${esc(cls)}" src="${esc(url)}" alt="" loading="lazy" decoding="async">`
      : placeholder(cls);

document.addEventListener("error", (ev) => {
  const el = ev.target;
  if (el instanceof HTMLImageElement && el.classList.contains("art")) {
    const keep = [...el.classList].filter((c) => c !== "art").join(" ");
    el.outerHTML = placeholder(keep);
  }
}, true);

export const statusBadge = (status) =>
  `<span class="badge cap ${esc(status)}">${esc(statusLabel(status))}</span>`;

export function confidenceCell(value) {
  const pct = Math.round((value || 0) * 100);
  const tier = pct >= 92 ? "high" : pct >= 62 ? "mid" : "low";
  return `<span class="conf ${tier}" title="Confidence ${pct}%">
    <span class="meter"><i style="width:${pct}%"></i></span>${pct}%</span>`;
}

export function emptyState(glyph, title, note = "", actions = "") {
  return `<div class="empty">
    <div class="glyph" aria-hidden="true">${glyph}</div>
    <strong>${esc(title)}</strong>
    ${note ? `<p>${esc(note)}</p>` : ""}
    ${actions ? `<div class="actions">${actions}</div>` : ""}
  </div>`;
}

/** Placeholder shown while a view's first fetch is in flight. */
export const skeleton = (rows = 3) => `
  <div class="skeleton">
    <div class="sk tiles">${"<i></i>".repeat(4)}</div>
    ${Array.from({ length: rows }, () => `<div class="sk" style="height:150px"></div>`).join("")}
  </div>`;

/* ----------------------------------------------------------- pagination -- */

const span = (from, to) => Array.from({ length: to - from + 1 }, (_, i) => from + i);

/* Exactly seven slots once there is more than one screenful, so the control
   never changes width as you page through — that width change is what makes a
   paginated table feel like it is shifting under you. */
export function pageWindow(current, pages) {
  if (pages <= 7) return span(1, pages);
  if (current <= 4) return [...span(1, 5), "…", pages];
  if (current >= pages - 3) return [1, "…", ...span(pages - 4, pages)];
  return [1, "…", current - 1, current, current + 1, "…", pages];
}

/* Renders a stable pagination footer. `key` namespaces the click targets so
   several pagers can coexist on one view. */
export function pager(key, { total, page, size, unit = "item" }) {
  const pages = Math.max(1, Math.ceil(total / size));
  const current = Math.min(Math.max(1, page), pages);
  const from = total ? (current - 1) * size + 1 : 0;
  const to = Math.min(current * size, total);

  const btn = (label, target, { on = false, off = false, aria = "" } = {}) =>
    `<button class="pg" data-act="page" data-key="${esc(key)}" data-target="${target}"
      ${on ? 'aria-current="page"' : ""} ${off ? "disabled" : ""}
      ${aria ? `aria-label="${esc(aria)}"` : ""}>${label}</button>`;

  const numbers = pages > 1
    ? pageWindow(current, pages)
        .map((p) => (p === "…" ? `<span class="gap">…</span>`
                               : btn(p, p, { on: p === current, aria: `Page ${p}` })))
        .join("")
    : "";

  return `
    <div class="pager">
      <span class="range">${total
        ? `${num(from)}–${num(to)} of ${num(total)} ${esc(unit)}${total === 1 ? "" : "s"}`
        : `No ${esc(unit)}s`}</span>
      <div class="pg-group">
        ${btn("‹", current - 1, { off: current <= 1, aria: "Previous page" })}
        ${numbers}
        ${btn("›", current + 1, { off: current >= pages, aria: "Next page" })}
      </div>
    </div>`;
}

/* Blank rows that hold a short final page at the height of a full one. Only
   used once a table actually spans more than one page — a small table should
   still size itself naturally. */
export const fillerRows = (shown, size, total, cols) =>
  total > size && shown < size
    ? Array.from({ length: size - shown },
        () => `<tr class="filler"><td colspan="${cols}"></td></tr>`).join("")
    : "";

/* --------------------------------------------------------- button state -- */

/* Every action that talks to the server disables its own button and says what
   it is doing. Doing it in one place means no view has to remember to put the
   label back on the failure path. */
export async function withBusy(el, label, work) {
  if (!el) return work();
  const original = el.innerHTML;
  const wasDisabled = el.disabled;
  el.disabled = true;
  if (label) el.textContent = label;
  try {
    return await work();
  } finally {
    if (el.isConnected) {
      el.disabled = wasDisabled;
      el.innerHTML = original;
      paintIcons(el);
    }
  }
}
