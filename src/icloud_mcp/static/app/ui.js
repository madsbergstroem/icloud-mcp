// Small helpers on top of Ionic: escaping, icons, times, toasts, alerts, sheets and the confirm step.
import { onConfirm, post, session, ApiError } from "./api.js";

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export function esc(v) { return String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ESC[c]); }

export function icon(name) {
  return `<ion-icon src="/dashboard/vendor/ionicons/8.1.0/${name}.svg" aria-hidden="true"></ion-icon>`;
}

export function root() { return document.querySelector("ion-app"); }

export function ago(t) {
  if (!t) return "never";
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) { const h = Math.floor(s / 3600); return `${h} hour${h === 1 ? "" : "s"} ago`; }
  const d = Math.floor(s / 86400); return `${d} day${d === 1 ? "" : "s"} ago`;
}

export function clock(t) {
  return new Date(t * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

export function until(t) {
  // "today at 18:30", "tomorrow at 10:56", "on 7 Nov at 09:00"
  const d = new Date(t * 1000), now = new Date();
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(d) - day(now)) / 86400000);
  const at = clock(t);
  if (diff === 0) return `today at ${at}`;
  if (diff === 1) return `tomorrow at ${at}`;
  return `on ${d.toLocaleDateString("en-GB", { day: "numeric", month: "short" })} at ${at}`;
}

// How long is left, in plain words: "41 min", "1 h 10 min", "2 days"
export function left(t) {
  const m = Math.max(1, Math.round((t - Date.now() / 1000) / 60));
  if (m < 60) return `${m} min`;
  if (m < 180) { const h = Math.floor(m / 60), r = m % 60; return r ? `${h} h ${r} min` : `${h} h`; }
  if (m < 2880) return `${Math.round(m / 60)} h`;
  return `${Math.round(m / 1440)} days`;
}

export function tag(text, cls = "") {
  return `<span class="tag ${cls}">${esc(text)}</span>`;
}

export function size(n) {
  if (n < 1048576) return `${Math.max(1, Math.round(n / 1024))} KB`;
  return `${(n / 1048576).toFixed(n % 1048576 ? 1 : 0)} MB`;
}

export async function toast(message, opts = {}) {
  const t = document.createElement("ion-toast");
  Object.assign(t, { message, duration: opts.duration || 3500, position: "bottom", color: opts.bad ? "danger" : undefined,
    buttons: opts.buttons || [], cssClass: opts.cssClass || "" });
  if (window.matchMedia("(max-width: 899px)").matches) t.positionAnchor = document.querySelector("ion-tab-bar") || undefined;
  root().appendChild(t);
  await t.present();
  return t;
}

export function fail(e) {
  const msg = e instanceof ApiError || e instanceof Error ? e.message : String(e);
  return toast(msg, { bad: true, duration: 5000 });
}

export async function ask(header, message, yes, opts = {}) {
  const a = document.createElement("ion-alert");
  a.header = header;
  a.message = message;
  a.buttons = [{ text: "Cancel", role: "cancel" }, { text: yes, role: opts.danger ? "destructive" : "confirm" }];
  root().appendChild(a);
  await a.present();
  const { role } = await a.onDidDismiss();
  a.remove();
  return role === "confirm" || role === "destructive";
}

export async function sheet(title, bodyHtml, onReady) {
  const m = document.createElement("ion-modal");
  m.innerHTML = `<ion-header><ion-toolbar><ion-title>${esc(title)}</ion-title>
    <ion-buttons slot="end"><ion-button data-close>Done</ion-button></ion-buttons></ion-toolbar></ion-header>
    <ion-content>${bodyHtml}</ion-content>`;
  m.querySelector("[data-close]").addEventListener("click", () => m.dismiss());
  m.addEventListener("didDismiss", () => m.remove());
  root().appendChild(m);
  if (onReady) onReady(m);
  await m.present();
  return m;
}

// Confirm it is you: the passkey if there is one, else (or also) the owner password. Resolves when confirmed.
function confirmIdentity() {
  return new Promise((resolve, reject) => {
    const s = session.data || {};
    const pk = s.passkeys && window.Passkey && window.Passkey.available();
    let done = false;
    sheet("Confirm it is you", `<div class="title"><p>You signed in more than 5 minutes ago. This step changes how your
      server is protected, so it asks you again.</p></div>
      ${pk ? `<div class="acts stack"><ion-button expand="block" data-pk>Confirm with a passkey</ion-button></div>` : ""}
      ${pk && s.password_login ? `<p class="or">or with the owner password</p>` : ""}
      ${s.password_login ? `<ion-list class="inset"><ion-item><ion-input type="password" label="Owner password" label-placement="stacked"
        autocomplete="current-password" data-pw></ion-input></ion-item></ion-list>
        <div class="acts stack"><ion-button expand="block" fill="outline" color="medium" data-go>Confirm</ion-button></div>` : ""}`,
    (m) => {
      const ok = () => { done = true; m.dismiss(); resolve(); };
      const pkBtn = m.querySelector("[data-pk]");
      if (pkBtn) pkBtn.addEventListener("click", () => window.Passkey.signin().then(ok, fail));
      const go = m.querySelector("[data-go]");
      if (go) go.addEventListener("click", async () => {
        const value = await m.querySelector("[data-pw]").getInputElement().then((i) => i.value);
        try { await post("/signin", { password: value }, true); ok(); } catch (e) { fail(e); }
      });
      m.addEventListener("didDismiss", () => { if (!done) reject(new Error("Not confirmed. Nothing was changed.")); });
    });
  });
}
onConfirm(confirmIdentity);

// A row: <ion-item> with a title, an optional subline and an optional note on the right
export function row({ title, sub, note, noteClass, button, detail, attrs }) {
  return `<ion-item ${button ? "button" : ""} detail="${detail === false || !button ? "false" : "true"}" ${attrs || ""}>
    <ion-label class="ion-text-wrap">${title ? `<h2>${title}</h2>` : ""}${sub ? `<p>${sub}</p>` : ""}</ion-label>
    ${note != null && note !== "" ? `<ion-note slot="end" class="${noteClass || ""}">${note}</ion-note>` : ""}</ion-item>`;
}

export function section(title, aside) {
  return `<ion-list-header class="sec"><ion-label>${esc(title)}</ion-label>${aside ? `<span class="aside">${aside}</span>` : ""}</ion-list-header>`;
}

export function titleBlock(h1, p) {
  return `<div class="title"><h1>${esc(h1)}</h1>${p ? `<p>${p}</p>` : ""}</div>`;
}

export function wide() { return window.matchMedia("(min-width: 900px)").matches; }
