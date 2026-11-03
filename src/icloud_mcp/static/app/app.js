// The dashboard app: sign-in, the three tabs (Today, Activity, Settings), the side navigation on wide screens, the live
// state every 15 seconds while the page is visible, and links that open one request (#/item/<kind>:<id>).
import { loadSession, get, post, session } from "./api.js";
import { esc, icon, fail, root, titleBlock } from "./ui.js";
import { app } from "./state.js";
import "./today.js";
import "./settings.js";

const POLL = 15000;
let lastV = null, timer = null;

function itemFromHash() {
  const m = /^#\/item\/([a-z]+:[A-Za-z0-9_-]+)$/.exec(location.hash);
  return m ? m[1] : null;
}

function signin() {
  const s = session.data;
  const pk = s.passkeys && window.Passkey && window.Passkey.available();
  root().innerHTML = `<ion-content><div class="signin">${titleBlock("Sign in", "Approve what your agents want to send, see what they did, and manage this server.")}
    ${pk ? `<div class="acts"><ion-button expand="block" data-pk>Sign in with a passkey</ion-button></div>` : ""}
    ${pk && s.password_login ? `<p class="or">or with the owner password</p>` : ""}
    ${s.password_login ? `<ion-list class="inset"><ion-item><ion-input type="password" label="Owner password" label-placement="stacked"
      autocomplete="current-password" data-pw></ion-input></ion-item></ion-list>
      <div class="acts"><ion-button expand="block" ${pk ? 'fill="outline" color="medium"' : ""} data-go>Sign in</ion-button></div>` : ""}
    <p class="note">${s.passkeys ? "" : "After you sign in, you can add a passkey under Settings. "}Ten wrong passwords in 15 minutes lock password sign-in for a while.</p>
    </div></ion-content>`;
  const done = () => location.reload();
  root().querySelector("[data-pk]")?.addEventListener("click", () => window.Passkey.signin().then(done, fail));
  const go = root().querySelector("[data-go]");
  const send = async () => {
    const value = await root().querySelector("[data-pw]").getInputElement().then((i) => i.value);
    try { await post("/signin", { password: value }); done(); } catch (e) { fail(e); }
  };
  go?.addEventListener("click", send);
  root().querySelector("[data-pw]")?.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
}

function shell() {
  const tabs = [["today", "Today", "file-tray-outline"], ["activity", "Activity", "time-outline"], ["settings", "Settings", "settings-outline"]];
  root().innerHTML = `<div class="shell">
    <nav class="side" aria-label="Sections">${tabs.map(([t, l, i]) => `<ion-item button detail="false" data-side="${t}" lines="none">${icon(i)}
      <ion-label style="margin-left:10px">${l}</ion-label>${t === "today" ? `<ion-note slot="end" class="num" data-count></ion-note>` : ""}</ion-item>`).join("")}
      <div class="foot"><span data-live-dot>●</span> <span data-live-text>Live</span><br>${esc(session.data.host)}</div></nav>
    <ion-tabs>
      ${tabs.map(([t]) => `<ion-tab tab="${t}"><ion-nav root="icm-${t}"></ion-nav></ion-tab>`).join("")}
      <ion-tab-bar slot="bottom">${tabs.map(([t, l, i]) => `<ion-tab-button tab="${t}">${icon(i)}<ion-label>${l}</ion-label>
        ${t === "today" ? `<ion-badge color="danger" data-badge hidden></ion-badge>` : ""}</ion-tab-button>`).join("")}</ion-tab-bar>
    </ion-tabs></div>`;
  const t = root().querySelector("ion-tabs");
  const mark = (tab) => root().querySelectorAll("[data-side]").forEach((n) => n.classList.toggle("on", n.dataset.side === tab));
  t.addEventListener("ionTabsDidChange", (e) => mark(e.detail.tab));
  root().querySelectorAll("[data-side]").forEach((n) => n.addEventListener("click", async () => {
    const cur = await t.getSelected();
    if (cur === n.dataset.side) root().querySelector(`ion-tab[tab="${n.dataset.side}"] ion-nav`).popToRoot();
    t.select(n.dataset.side);
  }));
  const item = itemFromHash();
  const tab = /^#\/(activity|settings)$/.exec(location.hash);
  if (tab) history.replaceState(null, "", location.pathname);
  t.select(tab ? tab[1] : "today").then(() => mark(tab ? tab[1] : "today"));
  if (item) openItem(item);
}

function openItem(key) {
  const t = root().querySelector("ion-tabs");
  history.replaceState(null, "", location.pathname);
  app.pendingOpen = key;
  t.select("today").then(() => window.dispatchEvent(new CustomEvent("icm:open", { detail: key })))
    .then(() => { app.pendingOpen = null; });
}

function setCount(n) {
  n = Math.max(0, n - app.hidden.size);      // a discard that can still be undone no longer counts
  app.waiting = n;
  const badge = root().querySelector("[data-badge]"), count = root().querySelector("[data-count]");
  if (badge) { badge.textContent = String(n); badge.hidden = !n; }
  if (count) count.textContent = n ? String(n) : "";
  document.title = n ? `(${n}) iCloud connector` : "iCloud connector";
}

function setLive() {
  const dot = root().querySelector("[data-live-dot]"), text = root().querySelector("[data-live-text]");
  if (dot) dot.style.color = app.online ? "var(--positive)" : "var(--note-ink)";
  if (text) text.textContent = app.online ? "Live" : "Offline";
}

async function poll() {
  try {
    const s = await get("/state", true);
    app.online = true; app.updated = Date.now() / 1000;
    setCount(s.waiting);
    if (lastV !== null && s.v !== lastV) window.dispatchEvent(new Event("icm:changed"));
    lastV = s.v;
  } catch (e) {
    app.online = false;
  }
  setLive();
}

function startPolling() {
  const start = () => { if (!timer) { poll(); timer = setInterval(poll, POLL); } };
  const stop = () => { clearInterval(timer); timer = null; };
  document.addEventListener("visibilitychange", () => (document.visibilityState === "visible" ? start() : stop()));
  if (document.visibilityState === "visible") start();
}

async function main() {
  await customElements.whenDefined("ion-app");
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/dashboard/sw.js", { scope: "/dashboard" }).catch(() => {});
  let s;
  try { s = await loadSession(); } catch (e) { root().innerHTML = `<ion-content><div class="empty">The server did not answer. Reload in a moment.</div></ion-content>`; return; }
  if (!s.signed_in) { signin(); return; }
  if (window.InstallHint && window.InstallHint.init) {
    try { window.InstallHint.init({ app: "iCloud connector", lang: "en", storageKey: "icm-install-hint" }); } catch (e) { /* the tip is optional */ }
  }
  shell();
  startPolling();
  window.addEventListener("hashchange", () => { const k = itemFromHash(); if (k) openItem(k); });
  window.addEventListener("icm:signed-out", () => location.reload());
  window.addEventListener("icm:changed", () => setTimeout(poll, 300));
  navigator.serviceWorker?.addEventListener("message", (e) => { if (e.data && e.data.open) openItem(e.data.open); });
}

main();
