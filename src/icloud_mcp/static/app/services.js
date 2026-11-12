// Connected services: one page per service (iCloud, another email and calendar provider, Beeper), "Connect a service" with its
// two forms, and the calendar choice. Changes to an account go to /accounts/... and restart the server; Beeper is set up by
// whoever runs the server, so its page only shows the networks and edits who it may write to.
import { get, post } from "./api.js";
import { esc, toast, fail, ask, section, titleBlock, row } from "./ui.js";
import { page, macRows, macAside, wireSwitches } from "./settings.js";

const AREAS = [["mail", "Mail"], ["calendar", "Calendar"], ["contacts", "Contacts"]];
const AREA_NAME = Object.fromEntries(AREAS);
const ONE_EACH = "Mail, calendar and contacts each come from one service. Turning one on here turns it off where it was before.";
const SIGN_IN = { icloud: "signed in with an app-specific password", other: "other email and calendar, signed in with a password" };

// The services as Settings lists them: [{key, title, sub}], the accounts first, then Beeper
export function serviceEntries(r) {
  const out = r.accounts.map((a) => ({ key: "acct:" + a.id, title: a.name, sub: a.address }));
  if (r.beeper) {
    const n = r.beeper.networks.length;
    out.push({ key: "beeper", title: "Beeper", sub: r.beeper.problem ? "Networks not readable" : `${n} network${n === 1 ? "" : "s"}` });
  }
  return out;
}

// After a saved change the server restarts; the page reloads once it is back
function restarting(r) {
  toast(r.note, { duration: 8000 });
  window.dispatchEvent(new Event("icm:settings"));
  setTimeout(() => { location.hash = "#/settings"; location.reload(); }, 30000);   // back in Settings once the server is up
}

// Saved settings wait for a restart; the same notice as on the Settings list, so it is seen where the change was made
function restartNotice(st) {
  return st.restart_needed ? `<div class="notice pause"><span>Saved changes apply after a restart. Agents are disconnected for about half a minute.</span>
    <ion-button size="small" data-restart>Restart now</ion-button></div>` : "";
}

function wireRestart(el) {
  el.querySelector("[data-restart]")?.addEventListener("click", async () => {
    try { restarting(await post("/settings/restart")); } catch (e) { fail(e); }
  });
}

function push(from, tag, props) { from.closest("ion-nav").push(tag, props || {}); }

// A page that is the root of Settings on a wide screen has no bar (the side list is the way back); on a phone it is one step in
function shell(el) {
  const body = page(el, "", el.asRoot ? "" : "Settings");
  if (el.asRoot) el.querySelector("ion-header")?.remove();
  return body;
}

// -- one service ----------------------------------------------------------------------------------------------------------
class Service extends HTMLElement {
  connectedCallback() {
    this.body = shell(this);
    this.load();
    window.addEventListener("icm:settings", (this.onChange = () => this.load()));
  }

  disconnectedCallback() { window.removeEventListener("icm:settings", this.onChange); }

  async load() {
    let r, st;
    try { [r, st] = await Promise.all([get("/accounts"), get("/settings")]); } catch (e) { fail(e); return; }
    this.r = r; this.st = st;
    if (this.sid === "beeper") return this.beeper(r.beeper);
    const a = r.accounts.find((x) => "acct:" + x.id === this.sid);
    if (!a) { this.body.innerHTML = titleBlock("Not connected", "This service is no longer connected."); return; }
    this.account(a);
  }

  account(a) {
    const r = this.r, st = this.st, can = r.connect.allowed, dis = can ? "" : "disabled";
    const holder = (area) => r.accounts.find((x) => x.uses.includes(area));
    const used = AREAS.map(([k, label]) => {
      const other = holder(k), mine = a.uses.includes(k), possible = a.possible.includes(k);
      const sub = !possible ? `This service has no ${label.toLowerCase()} server`
        : !mine && other ? `Now from ${esc(other.name)}. Turn on to move it here.` : "";
      return `<ion-item><ion-toggle data-area="${k}" ${mine ? "checked" : ""} ${possible ? dis : "disabled"}>${label}${sub ? `<br><small class="sub">${sub}</small>` : ""}</ion-toggle></ion-item>`;
    }).join("");
    const g = (slug) => st.groups.find((x) => x.slug === slug) || {};
    const calendars = r.accounts.find((x) => x.default_for.includes("Calendar"));
    const settingsRows = a.uses.map((k) => row({ title: AREA_NAME[k], sub: esc(g(k).summary || ""), button: true, attrs: `data-page="${k}"` })).join("")
      + (calendars && calendars.id === a.id ? row({ title: "Calendars your assistant sees", button: true, attrs: "data-calendars" }) : "");
    const firstIcloud = r.accounts.find((x) => x.provider === "icloud");
    const mac = a.provider === "icloud" && firstIcloud.id === a.id
      ? section("Through your Mac", macAside(st.mac || {})) + `<ion-list class="inset">${macRows(st)}</ion-list>
        <p class="note">Reminders, notes, iCloud Drive and iMessage work only through your Mac, while it is on and connected.</p>` : "";
    const servers = a.servers ? section("Servers") + `<ion-list class="inset">${[["imap", "Incoming mail (IMAP)"], ["smtp", "Outgoing mail (SMTP)"],
      ["caldav", "Calendar (CalDAV)"], ["carddav", "Contacts (CardDAV)"]].map(([k, l]) => row({ title: l, note: esc(a.servers[k] || "not set") })).join("")}</ion-list>` : "";
    const icloud = a.provider === "icloud";
    this.body.innerHTML = titleBlock(a.name, `${esc(a.address)} · ${SIGN_IN[a.provider] || ""}`)
      + restartNotice(st)
      + section("Used for") + `<ion-list class="inset">${used}</ion-list><p class="note">${ONE_EACH}${a.provider === "other" ? " Reminders and notes are not available with this kind of service." : ""}</p>`
      + (can ? "" : `<div class="notice pause"><span>${esc(r.connect.why)}</span></div>`)
      + (settingsRows ? section("Settings") + `<ion-list class="inset">${settingsRows}</ion-list>` : "")
      + mac + servers
      + section("Sign-in") + `<ion-list class="inset">
        ${row({ title: icloud ? "App-specific password" : "Password", sub: icloud ? "Create a new one at account.apple.com, then paste it here." : "",
          note: "Replace", button: can, attrs: can ? "data-password" : "" })}
        <ion-item><ion-toggle data-readonly ${a.read_only ? "checked" : ""} ${dis}>Read only<br><small class="sub">Your assistant can read but not send or change anything here</small></ion-toggle></ion-item>
        ${can ? `<ion-item button detail="false" data-remove><ion-label color="danger">Disconnect ${esc(a.name)}</ion-label></ion-item>` : ""}</ion-list>`;
    this.wireAccount(a);
  }

  async change(a, body, question, detail, yes) {
    if (!(await ask(question, detail, yes))) { this.load(); return; }
    try { restarting(await post(`/accounts/${encodeURIComponent(a.id)}/change`, body)); } catch (e) { fail(e); }
    this.load();
  }

  wireAccount(a) {
    const later = "The server checks the sign-in, saves and restarts. That takes about half a minute.";
    this.querySelectorAll("[data-area]").forEach((n) => n.addEventListener("ionChange", (e) => {
      const k = n.dataset.area, on = e.detail.checked;
      const areas = on ? [...a.uses, k] : a.uses.filter((x) => x !== k);
      this.change(a, { areas }, `${on ? "Use" : "Stop using"} ${a.name} for ${AREA_NAME[k].toLowerCase()}?`, on ? later
        : `Your assistant then has no ${AREA_NAME[k].toLowerCase()} until you turn it on for a service. The server restarts.`, on ? "Turn on" : "Turn off");
    }));
    this.querySelectorAll("[data-page]").forEach((n) => n.addEventListener("click", () => push(this, "icm-group", { page: n.dataset.page })));
    this.querySelector("[data-calendars]")?.addEventListener("click", () => push(this, "icm-calendars", { acct: a.id }));
    this.querySelector("[data-readonly]")?.addEventListener("ionChange", (e) => this.change(a, { read_only: e.detail.checked },
      e.detail.checked ? `Make ${a.name} read only?` : `Let your assistant change things in ${a.name}?`,
      e.detail.checked ? "Your assistant can still read, but cannot send or change anything here. The server restarts." : later,
      e.detail.checked ? "Read only" : "Allow"));
    this.querySelector("[data-password]")?.addEventListener("click", () => this.password(a));
    wireRestart(this);
    wireSwitches(this, this.st, () => this.load());
    this.querySelector("[data-remove]")?.addEventListener("click", async () => {
      const reach = a.uses.length ? `Your assistant loses ${a.uses.map((k) => AREA_NAME[k].toLowerCase()).join(", ")} of ${a.address}. ` : "";
      const apple = a.provider === "icloud" ? " You can delete the app-specific password at account.apple.com too." : "";
      if (!(await ask(`Disconnect ${a.name}?`, `${reach}The address and password are deleted from this server.${apple}`, "Disconnect", { danger: true }))) return;
      try { restarting(await post(`/accounts/${encodeURIComponent(a.id)}/remove`)); this.closest("ion-nav").popToRoot(); } catch (e) { fail(e); }
    });
  }

  async password(a) {
    const icloud = a.provider === "icloud";
    const al = document.createElement("ion-alert");
    al.header = icloud ? "New app-specific password" : "New password";
    al.message = icloud ? "Create it at account.apple.com under Sign-In and Security. The server checks it before anything is saved."
      : "The server checks it before anything is saved.";
    al.inputs = [{ name: "password", type: "password", placeholder: icloud ? "abcd-efgh-ijkl-mnop" : "Password", attributes: { autocomplete: "off" } }];
    al.buttons = [{ text: "Cancel", role: "cancel" }, { text: "Check and save", role: "confirm" }];
    document.querySelector("ion-app").appendChild(al);
    await al.present();
    const { role, data } = await al.onDidDismiss();
    al.remove();
    if (role !== "confirm") return;
    try { restarting(await post(`/accounts/${encodeURIComponent(a.id)}/change`, { password: data.values.password || "" })); } catch (e) { fail(e); }
  }

  beeper(b) {
    if (!b) { this.body.innerHTML = titleBlock("Beeper", "Beeper is not set up on this server."); return; }
    const st = this.st;
    const allow = (st.groups.find((x) => x.slug === "beeper") || {}).summary || "";
    const nets = b.problem || !b.networks.length
      ? row({ title: esc(b.problem || "Beeper lists no connected network.") })
      : b.networks.map((n, i) => row({ title: esc(n.network), sub: esc(n.user), button: true, attrs: `data-net="${i}"` })).join("");
    this.body.innerHTML = titleBlock("Beeper", "Chat networks through Beeper. Whoever runs this server sets it up.") + restartNotice(st)
      + section("Networks") + `<ion-list class="inset">${nets}</ion-list>`
      + `<p class="note">${b.networks.length ? "Tap a network to see its account ID. " : ""}New networks are added in the Beeper app.</p>`
      + section("What your assistant may do") + `<ion-list class="inset">
        ${row({ title: "Send", sub: "Only after your approval, and only to the chats below", note: b.sending ? "On" : "Off", noteClass: b.sending ? "state-on" : "state-off" })}
        ${row({ title: "Who it may write to", note: esc(allow), button: true, attrs: "data-allow" })}
        ${row({ title: "Read chats", note: "Not built yet" })}</ion-list>`;
    this.querySelectorAll("[data-net]").forEach((n) => n.addEventListener("click", () => {
      const net = b.networks[Number(n.dataset.net)];
      const al = document.createElement("ion-alert");
      Object.assign(al, { header: net.network, subHeader: net.user, message: `Account ID ${net.account_id}`, buttons: ["Done"] });   // Ionic shows these as text
      al.addEventListener("didDismiss", () => al.remove());
      document.querySelector("ion-app").appendChild(al);
      al.present();
    }));
    this.querySelector("[data-allow]").addEventListener("click", () => push(this, "icm-group", { page: "beeper" }));
    wireRestart(this);
  }
}
customElements.define("icm-service", Service);

// -- connect a service ----------------------------------------------------------------------------------------------------
class Connect extends HTMLElement {
  async connectedCallback() {
    const body = shell(this);
    let r;
    try { r = await get("/accounts"); } catch (e) { fail(e); return; }
    const can = r.connect.allowed;
    // each choice ends with one plain line on whether it can be connected here
    const tile = (key, title, text, on, status) => `<ion-card ${on ? `button data-provider="${key}"` : 'disabled="true"'}>
      <ion-card-header><ion-card-title>${title}</ion-card-title></ion-card-header>
      <ion-card-content>${text}<p class="status">${status}</p></ion-card-content></ion-card>`;
    const here = can ? "Available" : "Needs your sign-in through Tailscale";
    body.innerHTML = titleBlock("Connect a service", can ? `You are signed in through ${esc(r.connect.via)}, so the server knows it is you.` : "")
      + (can ? "" : `<div class="notice pause"><span>${esc(r.connect.why)}</span></div>`)
      + `<div class="tiles">${tile("icloud", "iCloud", "Mail, calendar and contacts. With your Mac also reminders, notes, iCloud Drive and iMessage.", can, here)}
        ${tile("other", "Other email &amp; calendar", "Any provider with IMAP and SMTP, and CalDAV or CardDAV if it has them. You enter the servers and a password.", can, here)}
        ${tile("beeper", "Beeper", "Chat networks such as WhatsApp, Signal and Telegram.", false, "Set up by whoever runs this server")}</div>`
      + `<p class="note">Mail, calendar and contacts each come from one service. You can connect a second mail service, but only one is used for mail at a time.</p>`;
    this.querySelectorAll("[data-provider]").forEach((n) => n.addEventListener("click", () => push(this, "icm-connect-form", { provider: n.dataset.provider })));
  }
}
customElements.define("icm-connect", Connect);

const APPLE = "https://account.apple.com";
const APPLE_HELP = "https://support.apple.com/en-us/102654";

class ConnectForm extends HTMLElement {
  async connectedCallback() {
    this.body = page(this, "", "Back");
    try { this.r = await get("/accounts"); } catch (e) { fail(e); return; }
    const icloud = this.provider === "icloud";
    const input = (name, label, attrs = "") => `<ion-item><ion-input data-f="${name}" label="${label}" label-placement="stacked" ${attrs}></ion-input></ion-item>`;
    const prep = icloud ? section("First, at Apple") + `<ion-list class="inset"><ion-item><ion-label class="ion-text-wrap"><ol class="steps">
        <li>Sign in at <a href="${APPLE}" target="_blank" rel="noopener noreferrer">account.apple.com</a>.</li>
        <li>Under Sign-In and Security, choose App-Specific Passwords.</li>
        <li>Generate one, name it for example “Assistant”, and copy it.</li></ol></ion-label></ion-item></ion-list>
        <p class="note">Apple gives app-specific passwords only to accounts with two-factor authentication. <a href="${APPLE_HELP}" target="_blank" rel="noopener noreferrer">Apple's guide</a></p>`
      : section("Advanced server settings") + `<ion-list class="inset">${input("imap", "Incoming mail (IMAP)", 'placeholder="imap.example.org:993" autocomplete="off"')}
        ${input("smtp", "Outgoing mail (SMTP)", 'placeholder="smtp.example.org:587" autocomplete="off"')}
        ${input("caldav", "Calendar (CalDAV)", 'type="url" placeholder="https://dav.example.org/" autocomplete="off"')}
        ${input("carddav", "Contacts (CardDAV)", 'type="url" placeholder="https://dav.example.org/" autocomplete="off"')}</ion-list>
        <p class="note">Your provider's help pages list these, often under IMAP or “set up a mail app”. Leave out what you do not need.</p>`;
    const holder = (k) => this.r.accounts.find((x) => x.uses.includes(k));
    const areas = AREAS.map(([k, label]) => `<ion-item><ion-toggle data-area="${k}" ${holder(k) ? "" : "checked"}>${label}${holder(k)
      ? `<br><small class="sub">Now from ${esc(holder(k).name)}. Turn on to move it here.</small>` : ""}</ion-toggle></ion-item>`).join("");
    // another provider: a preset only fills in the servers, which stay editable below
    const provider = icloud ? "" : `<ion-list class="inset" style="margin-top:14px!important"><ion-item><ion-select data-preset label="Provider" interface="action-sheet" value="custom">
        <ion-select-option value="custom">Custom</ion-select-option>${this.r.connect.presets.map((p) =>
          `<ion-select-option value="${esc(p.id)}">${esc(p.label)}</ion-select-option>`).join("")}</ion-select></ion-item></ion-list>
        <p class="note">A preset fills in the servers under Advanced server settings. You can still change them.</p>`;
    this.body.innerHTML = titleBlock(icloud ? "Connect iCloud" : "Connect other email & calendar",
      icloud ? "Your assistant can use the areas you choose below." : "Any provider with IMAP and SMTP, CalDAV or CardDAV.")
      + provider + (icloud ? prep : "") + `<div data-failure></div>`
      + section("Sign-in") + `<ion-list class="inset">${input("username", icloud ? "Apple Account email" : "Email address or user name", 'type="email" autocomplete="username"')}
        ${input("password", icloud ? "App-specific password" : "Password", `type="password" autocomplete="off" ${icloud ? 'placeholder="abcd-efgh-ijkl-mnop"' : ""}`)}</ion-list>`
      + section("Use it for") + `<ion-list class="inset">${areas}
        <ion-item><ion-toggle data-f="read_only">Read only<br><small class="sub">Your assistant can read but not send or change anything here</small></ion-toggle></ion-item></ion-list>`
      + (icloud ? "" : prep)
      + section("Sender (optional)") + `<ion-list class="inset">${input("email_address", "Send as", 'type="email" placeholder="Leave empty to use the address above"')}
        ${input("display_name", "Your name in mail", 'autocomplete="name"')}</ion-list>`
      + `<p class="note">The password stays on this server and is never shown; your assistant never sees it. The check signs in once and takes a few seconds.</p>
        <div class="acts"><ion-button data-go>Check and connect</ion-button></div>`;
    this.querySelector("[data-go]").addEventListener("click", () => this.send());
    this.querySelector("[data-preset]")?.addEventListener("ionChange", (e) => {
      const p = this.r.connect.presets.find((x) => x.id === e.detail.value);
      if (!p) return;                                        // Custom keeps what is there
      for (const k of ["imap", "smtp", "caldav", "carddav"]) this.querySelector(`[data-f="${k}"]`).value = p[k];
    });
  }

  async value(name) {
    const n = this.querySelector(`[data-f="${name}"]`);
    if (!n) return undefined;
    if (n.tagName === "ION-TOGGLE") return n.checked;
    return (await n.getInputElement()).value;
  }

  async send() {
    const go = this.querySelector("[data-go]");
    const body = { provider: this.provider, areas: [...this.querySelectorAll("[data-area]")].filter((n) => n.checked).map((n) => n.dataset.area) };
    for (const k of ["username", "password", "read_only", "email_address", "display_name", "imap", "smtp", "caldav", "carddav"]) {
      const v = await this.value(k);
      if (v !== undefined) body[k] = v;
    }
    const box = this.querySelector("[data-failure]");
    box.innerHTML = "";
    go.disabled = true; go.textContent = "Checking…";
    try {
      restarting(await post("/accounts/connect", body));
      this.closest("ion-nav").popToRoot();
    } catch (e) {
      const f = e.body && e.body.failure;
      if (f) {
        box.innerHTML = `<div class="notice bad"><div><b>${esc(f.head)}</b>${f.entered ? `<br>You entered ${esc(f.entered)}.` : ""}
          <ol class="steps">${f.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ol></div></div>`;
        box.scrollIntoView({ behavior: "smooth", block: "center" });
      } else fail(e);
    }
    go.disabled = false; go.textContent = "Check and connect";
  }
}
customElements.define("icm-connect-form", ConnectForm);

// -- which calendars the assistant sees -----------------------------------------------------------------------------------
class Calendars extends HTMLElement {
  async connectedCallback() {
    const body = page(this, "", "Back");
    body.innerHTML = titleBlock("Calendars your assistant sees", "Signing in to read the list…");
    let r;
    try { r = await get(`/accounts/${encodeURIComponent(this.acct)}/calendars`); } catch (e) { body.innerHTML = titleBlock("Calendars your assistant sees"); fail(e); return; }
    const dis = r.why ? "disabled" : "";
    body.innerHTML = titleBlock("Calendars your assistant sees", "Your assistant cannot see a calendar that is turned off, even when it looks for free time.")
      + (r.why ? `<div class="notice pause"><span>${esc(r.why)}</span></div>` : "")
      + `<ion-list class="inset" style="margin-top:14px!important">${r.calendars.map((c) => `<ion-item><ion-toggle data-cal="${esc(c.id)}" ${c.visible ? "checked" : ""} ${dis}>${esc(c.name)}</ion-toggle></ion-item>`).join("")}</ion-list>`;
    if (r.why) return;
    this.querySelector("[data-end]").innerHTML = `<ion-button strong disabled data-save>Save</ion-button>`;
    const save = this.querySelector("[data-save]");
    this.querySelectorAll("[data-cal]").forEach((n) => n.addEventListener("ionChange", () => { save.disabled = false; }));
    save.addEventListener("click", async () => {
      const visible = [...this.querySelectorAll("[data-cal]")].filter((n) => n.checked).map((n) => n.dataset.cal);
      try { restarting(await post(`/accounts/${encodeURIComponent(this.acct)}/calendars`, { visible })); this.closest("ion-nav").pop(); } catch (e) { fail(e); }
    });
  }
}
customElements.define("icm-calendars", Calendars);

