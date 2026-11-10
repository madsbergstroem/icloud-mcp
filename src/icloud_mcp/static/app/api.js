// Talking to /dashboard/api: the session, reads, and writes with the CSRF token. A write that changes how the server is
// protected may answer {confirm: true}; then the owner confirms (passkey or password) and the write runs once more.
let csrf = "";
let confirmer = null;                 // set by ui.js: shows the confirm sheet, resolves when the owner confirmed
export const session = { data: null };

export function onConfirm(fn) { confirmer = fn; }

// For steps that do not go through post() (a passkey being added): ask, then take the fresh token
export async function confirmNow() {
  if (!confirmer) throw new Error("Sign in again first.");
  await confirmer();
  return loadSession();
}

export class ApiError extends Error {
  constructor(message, status, body) { super(message); this.status = status; this.body = body || {}; }
}

async function parse(r) {
  let body = {};
  try { body = await r.json(); } catch (e) { body = {}; }
  if (r.status === 401) { window.dispatchEvent(new CustomEvent("icm:signed-out")); }
  if (!r.ok || body.ok === false) throw new ApiError(body.message || body.error || "The server did not answer.", r.status, body);
  return body;
}

export async function loadSession() {
  const r = await fetch("/dashboard/api/session", { credentials: "same-origin", cache: "no-store" });
  const body = await r.json();
  session.data = body;
  csrf = body.csrf || "";
  return body;
}

export function token() { return csrf; }

export async function get(path, live) {
  const r = await fetch("/dashboard/api" + path, { credentials: "same-origin", cache: "no-store",
    headers: live ? { "x-live": "1" } : {} });   // a background poll does not keep the session alive
  return parse(r);
}

export async function post(path, body, again) {
  const r = await fetch("/dashboard/api" + path, { method: "POST", credentials: "same-origin", cache: "no-store",
    headers: { "content-type": "application/json", "x-csrf": csrf }, body: JSON.stringify(body || {}) });
  try {
    return await parse(r);
  } catch (e) {
    if (e.status === 403 && e.body.confirm && !again && confirmer) {
      await confirmer();                          // throws when the owner cancels
      await loadSession();                        // a fresh sign-in has a fresh token
      return post(path, body, true);
    }
    throw e;
  }
}

export async function put(path, body) {
  const r = await fetch("/dashboard/api" + path, { method: "PUT", credentials: "same-origin", cache: "no-store",
    headers: { "content-type": "application/json", "x-csrf": csrf }, body: JSON.stringify(body || {}) });
  return parse(r);
}

// The same POST, sent even while the page is being hidden or closed (an Undo that ran out as the owner left)
export function postNow(path, body) {
  return fetch("/dashboard/api" + path, { method: "POST", credentials: "same-origin", keepalive: true,
    headers: { "content-type": "application/json", "x-csrf": csrf }, body: JSON.stringify(body || {}) });
}

// A file for a waiting mail goes through the existing multipart route
export async function upload(itemId, index, file) {
  const form = new FormData();
  form.append("csrf", csrf);
  form.append("index", String(index));
  form.append("file", file);
  const r = await fetch("/dashboard/attach/" + encodeURIComponent(itemId) + "/file", { method: "POST", credentials: "same-origin",
    headers: { accept: "application/json" }, body: form });
  return parse(r);
}

export async function removeFile(itemId, index) {
  const form = new FormData();
  form.append("csrf", csrf);
  form.append("index", String(index));
  const r = await fetch("/dashboard/attach/" + encodeURIComponent(itemId) + "/remove", { method: "POST", credentials: "same-origin",
    headers: { accept: "application/json" }, body: form });
  return parse(r);
}
