// The dashboard app: sign-in, the three tabs (Home, Activity, Settings), the side list on wide screens (with its own list for
// Settings), the live state every 15 seconds while the page is visible, and links that open one request (#/item/<kind>:<id>).
import { loadSession, get, post, session } from "./api.js";
import { esc, icon, fail, root, titleBlock, wide } from "./ui.js";
import { app } from "./state.js";
import "./today.js";
import { SECTIONS } from "./settings.js";
import { serviceEntries } from "./services.js";

const KIND_NAMES = { mail: "Mail", imessage: "iMessage", calendar: "Calendar" };
// a request of the external source counts under its action type's badge ("external:Beeper"); an unknown type under "Other"
const kindName = (k) => (k.startsWith("external:") ? k.slice(9) || "Other" : KIND_NAMES[k] || k);

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
  const tabs = [["today", "Home", "file-tray-outline"], ["activity", "Activity", "time-outline"], ["settings", "Settings", "settings-outline"]];
  root().innerHTML = `<div class="shell">
    <nav class="side" aria-label="Sections">
      <div class="who"><span class="av">I</span>iCloud connector</div>
      <ion-button class="neutral pause" expand="block" data-pause>Pause tools</ion-button>
      ${tabs.map(([t, l, i]) => `<ion-item button detail="false" data-side="${t}" lines="none">${icon(i)}
        <ion-label style="margin-left:10px">${l}</ion-label>${t === "today" ? `<ion-note slot="end" class="num" data-count></ion-note>` : ""}</ion-item>`).join("")}
      <div class="lbl">Waiting by kind</div><div data-kinds></div>
      <div class="foot" data-foot></div></nav>
    <nav class="side setnav" aria-label="Settings sections">
      <ion-item button detail="false" lines="none" data-back>${icon("chevron-back-outline")}<ion-label style="margin-left:10px">Back to app</ion-label></ion-item>
      <div class="lbl">General</div>
      ${SECTIONS.map(([id, label]) => `<ion-item button detail="false" lines="none" data-section="${id}"><ion-label>${esc(label)}</ion-label></ion-item>`).join("")}
      <div class="lbl">Connected services</div><div data-services></div>
      <ion-item button detail="false" lines="none" data-section="connect"><ion-label color="primary">Connect a service</ion-label></ion-item>
    </nav>
    <ion-tabs>
      ${tabs.map(([t]) => `<ion-tab tab="${t}"><ion-nav root="icm-${t}"></ion-nav></ion-tab>`).join("")}
      <ion-tab-bar slot="bottom">${tabs.map(([t, l, i]) => `<ion-tab-button tab="${t}">${icon(i)}<ion-label>${l}</ion-label>
        ${t === "today" ? `<ion-badge color="danger" data-badge hidden></ion-badge>` : ""}</ion-tab-button>`).join("")}</ion-tab-bar>
    </ion-tabs></div>`;
  const t = root().querySelector("ion-tabs");
  const shellEl = root().querySelector(".shell");
  const mark = (tab) => {
    root().querySelectorAll("[data-side]").forEach((n) => n.classList.toggle("on", n.dataset.side === tab));
    shellEl.classList.toggle("in-settings", tab === "settings");     // on a wide screen Settings swaps the side list for its own
  };
  t.addEventListener("ionTabsDidChange", (e) => mark(e.detail.tab));
  root().querySelectorAll("[data-side]").forEach((n) => n.addEventListener("click", async () => {
    const cur = await t.getSelected();
    if (cur === n.dataset.side) root().querySelector(`ion-tab[tab="${n.dataset.side}"] ion-nav`).popToRoot();
    t.select(n.dataset.side);
  }));
  root().querySelector("[data-back]").addEventListener("click", () => t.select("today"));
  const setNav = () => root().querySelector('ion-tab[tab="settings"] ion-nav');
  let current = SECTIONS[0][0];
  const markSection = (id) => { current = id; root().querySelectorAll("[data-section]").forEach((n) => n.classList.toggle("on", n.dataset.section === id)); };
  // a general group, a service ("acct:<id>", "beeper") or "connect" becomes the root of Settings
  const openSection = async (id) => {
    const [tag, props] = id === "connect" ? ["icm-connect", { asRoot: true }]
      : id === "beeper" || id.startsWith("acct:") ? ["icm-service", { sid: id, asRoot: true }] : ["icm-settings", { section: id }];
    await setNav().setRoot(tag, props, { animated: false });
    markSection(id);
  };
  const wireSection = (n) => n.addEventListener("click", () => openSection(n.dataset.section));
  root().querySelectorAll("[data-section]").forEach(wireSection);
  // the services in the side list follow what is connected
  const fillServices = async () => {
    let r;
    try { r = await get("/accounts"); } catch (e) { return; }
    const box = root().querySelector("[data-services]");
    box.innerHTML = serviceEntries(r).map((x) => `<ion-item button detail="false" lines="none" data-section="${esc(x.key)}">
      <ion-label>${esc(x.title)}<p>${esc(x.sub)}</p></ion-label></ion-item>`).join("");
    box.querySelectorAll("[data-section]").forEach(wireSection);
    markSection(current);
  };
  fillServices();
  window.addEventListener("icm:settings", fillServices);
  markSection(SECTIONS[0][0]);
  root().querySelector("[data-pause]").addEventListener("click", async () => {
    try { await post("/server/pause", { paused: !app.paused }); window.dispatchEvent(new Event("icm:changed")); } catch (e) { fail(e); }
  });
  const item = itemFromHash();
  const tab = /^#\/(activity|settings|connect)$/.exec(location.hash);
  if (tab) history.replaceState(null, "", location.pathname);
  const first = tab ? (tab[1] === "connect" ? "settings" : tab[1]) : "today";
  t.select(first).then(() => {
    mark(first);
    if (tab && tab[1] === "connect") wide() ? openSection("connect") : setNav().push("icm-connect");   // the old /setup address
  });
  renderSide();
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
  const badge = root().querySelector("[data-badge]");
  if (badge) { badge.textContent = String(n); badge.hidden = !n; }
  document.title = n ? `(${n}) iCloud connector` : "iCloud connector";
  renderSide();
}

// The side list's own bits: the count, the pause button, the waiting count by kind and the state card at the bottom
function renderSide() {
  const q = (s) => root().querySelector(s);
  const count = q("[data-count]");
  if (count) count.textContent = app.waiting ? String(app.waiting) : "";
  const pause = q("[data-pause]");
  if (pause) pause.textContent = app.paused ? "Resume tools" : "Pause tools";
  const kinds = q("[data-kinds]");
  if (kinds) kinds.innerHTML = Object.entries(app.kinds).map(([k, n]) => `<ion-item class="sub" lines="none"><ion-label>${esc(kindName(k))}</ion-label>
    <ion-note slot="end" class="num">${n}</ion-note></ion-item>`).join("");
  const foot = q("[data-foot]");
  if (foot) foot.innerHTML = `<b>${app.online ? (app.paused ? "Live, tools paused" : "Live") : "Offline"}</b>${esc((session.data || {}).host || "")}`
    + (app.mac === "off" ? "" : `<br>Mac helper ${app.mac === "online" ? "online" : "offline"}`);
}

async function poll() {
  try {
    const s = await get("/state", true);
    app.online = true; app.updated = Date.now() / 1000;
    app.paused = s.paused; app.kinds = s.kinds || {}; app.mac = s.mac || "off";
    setCount(s.waiting);
    if (lastV !== null && s.v !== lastV) window.dispatchEvent(new Event("icm:changed"));
    lastV = s.v;
  } catch (e) {
    app.online = false;
  }
  renderSide();
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
  window.addEventListener("hashchange", () => {
    const k = itemFromHash();
    if (k) openItem(k);
    else if (/^#\/(activity|settings|connect)$/.test(location.hash)) location.reload();   // the shell reads these when it starts
  });
  window.addEventListener("icm:signed-out", () => location.reload());
  window.addEventListener("icm:changed", () => setTimeout(poll, 300));
  navigator.serviceWorker?.addEventListener("message", (e) => { if (e.data && e.data.open) openItem(e.data.open); });
}

main();
