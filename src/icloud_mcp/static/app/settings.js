// Activity, Settings and the pages Settings opens: notifications, passkeys, apps, the password, one settings area, the server.
import { get, post, session, loadSession, token, confirmNow } from "./api.js";
import { esc, icon, ago, toast, fail, ask, section, titleBlock, row, tag, wide } from "./ui.js";

export function page(el, title, back, body) {
  el.innerHTML = `<ion-header translucent><ion-toolbar>${back ? `<ion-buttons slot="start"><ion-back-button text="${esc(back)}"></ion-back-button></ion-buttons>` : ""}
    <ion-buttons slot="end" data-end></ion-buttons></ion-toolbar></ion-header><ion-content fullscreen>${body || '<div data-body></div>'}</ion-content>`;
  return el.querySelector("[data-body]");
}

function push(from, tag, props) { from.closest("ion-nav").push(tag, props || {}); }

// -- Activity -----------------------------------------------------------------------------------------------------------
class Activity extends HTMLElement {
  connectedCallback() {
    this.filter = "all";
    this.innerHTML = `<ion-content><div data-body></div></ion-content>`;
    this.load();
    window.addEventListener("icm:changed", (this.onChange = () => this.load()));
  }

  disconnectedCallback() { window.removeEventListener("icm:changed", this.onChange); }

  async load() {
    let a;
    try { a = await get("/activity?filter=" + encodeURIComponent(this.filter)); } catch (e) { fail(e); return; }
    const days = a.days.map((d) => section(d.day) + `<ion-list class="inset">${d.entries.map((e) => `<ion-item>
      <ion-label class="ion-text-wrap" style="display:flex;gap:12px"><span class="num" style="color:var(--muted);min-width:44px">${esc(e.time)}</span>
      <span><span${e.ok ? "" : ' style="color:var(--negative)"'}>${esc(e.who)} ${esc(e.what)}</span>${e.error ? `<br><small style="color:var(--muted)">${esc(e.error)}</small>` : ""}
      ${e.note ? `<br><small style="color:var(--muted)">${esc(e.note)}</small>` : ""}</span>
      </ion-label>${e.tag ? tag(e.tag, e.tag === "Unclear" ? "warn end" : "end").replace('class="tag', 'slot="end" class="tag') : ""}</ion-item>`).join("")}</ion-list>`).join("");
    const chain = a.chain.ok ? `The chain of entries is intact (${a.chain.entries} entries).`
      : `<span style="color:var(--negative)">The log was changed after it was written, at entry ${esc(a.chain.broken_at)}. Entries from there on cannot be trusted.</span>`;
    this.querySelector("[data-body]").innerHTML = titleBlock("Activity", `Everything that was sent, dropped or changed in the last ${esc(a.keep_days)} days.`)
      + (a.log_on ? "" : `<div class="notice pause"><span>The activity log is turned off on this server.</span></div>`)
      + `<div style="padding:10px 16px 0"><ion-segment value="${esc(a.filter)}" scrollable>${a.filters.map(([k, l]) =>
        `<ion-segment-button value="${esc(k)}"><ion-label>${esc(l)}</ion-label></ion-segment-button>`).join("")}</ion-segment></div>`
      + (days || `<div class="empty"><b>Nothing recorded${this.filter === "all" ? " yet" : " of this kind"}.</b>What your assistant does and what you change shows up here.</div>`)
      + `<p class="note">${chain} No message text, passwords or tokens. <a href="/dashboard/activity.json" download>Download</a></p>`;
    this.querySelector("ion-segment").addEventListener("ionChange", (e) => { this.filter = e.detail.value; this.load(); });
  }
}
customElements.define("icm-activity", Activity);

// -- Settings -----------------------------------------------------------------------------------------------------------
// Every setting lives in exactly one place. A switch with nothing behind it sits in its row; an area with rules opens a
// page that starts with the area's own switch and then shows only its rules.
const on = (v) => ["1", "true", "yes", "on"].includes(String(v).toLowerCase());
const state = (v) => ({ note: on(v) ? "On" : "Off", noteClass: on(v) ? "state-on" : "state-off" });
const MAC_SWITCHES = [["Reminders", "ENABLE_REMINDERS"], ["Notes", "ENABLE_NOTES"], ["iCloud Drive", "ENABLE_DRIVE"], ["Maps", "ENABLE_MAPS"], ["Health", "ENABLE_HEALTH"]];

// What each page shows: the group whose fields it edits, an area switch from another group at the top, keys or sections
// it leaves to another page.
export const PAGES = {
  mail: { slug: "mail", title: "Mail", lead: "ENABLE_MAIL", hide: ["ALLOW_PERMANENT_DELETE"], sendMode: true },
  calendar: { slug: "calendar", title: "Calendar", lead: "ENABLE_CALENDAR" },
  contacts: { slug: "contacts", title: "Contacts", lead: "ENABLE_CONTACTS" },
  messages: { slug: "helper", title: "Messages", lead: "ENABLE_IMESSAGE", leadLabel: "Agents can read Messages", sections: ["Messages"] },
  helper: { slug: "helper", title: "Mac helper", sections: ["Shortcuts and timing"] },
  tools: { slug: "areas", title: "Only these tools", keys: ["TOOLS"] },
  accounts: { slug: "accounts", title: "Account" },
  log: { slug: "dashboard", title: "Activity log", sections: ["Activity log", "Sign-in"], hide: ["OWNER_PASSWORD_LOGIN"] },
};

function owner(st, key) { return (st.groups.find((g) => g.fields.some((f) => f.key === key)) || {}).slug; }

async function saveKeys(st, values) {
  // one request per group the keys belong to; a change that weakens protection asks to confirm first (api.js)
  const by = {};
  for (const [k, v] of Object.entries(values)) (by[owner(st, k)] = by[owner(st, k)] || {})[k] = v;
  let note = "";
  for (const [slug, vals] of Object.entries(by)) note = (await post(`/settings/${slug}`, { values: vals })).note || note;
  return note;
}

// The groups of the Settings list. On a wide screen the side list picks one; "accounts" opens its own page.
export const SECTIONS = [["security", "Sign-in and security"], ["accounts", "Connected accounts"], ["icloud", "iCloud"], ["mac", "Your Mac"],
  ["safety", "Safety"], ["server", "Account and server"]];

class Settings extends HTMLElement {
  connectedCallback() {
    this.innerHTML = `<ion-content><div data-body></div></ion-content>`;
    this.load();
    window.addEventListener("icm:settings", (this.onChange = () => this.load()));
    this.mq = window.matchMedia("(min-width: 900px)");
    this.mq.addEventListener("change", (this.onWidth = () => this.load()));
  }

  disconnectedCallback() {
    window.removeEventListener("icm:settings", this.onChange);
    this.mq.removeEventListener("change", this.onWidth);
  }

  async load() {
    let st, sec, accts = null;
    try { [st, sec] = await Promise.all([get("/settings"), get("/security")]); } catch (e) { fail(e); return; }
    try { accts = (await get("/accounts")).accounts.length; } catch (e) { /* the row still opens the list */ }
    this.st = st;
    const v = st.values, mac = st.mac || {}, s = session.data, ed = st.editable ? "" : "disabled";
    const g = (slug) => st.groups.find((x) => x.slug === slug) || {};
    const sw = (key, label, sub) => `<ion-item><ion-toggle data-switch="${key}" ${on(v[key]) ? "checked" : ""} ${ed}>${esc(label)}${sub ? `<br><small class="sub">${esc(sub)}</small>` : ""}</ion-toggle></ion-item>`;
    const macAside = !mac.set_up ? "Not set up" : mac.online ? `<span style="color:var(--positive)">● Online</span>`
      : `<span style="color:var(--note-ink)">Offline${mac.last_seen_at ? `, seen ${esc(ago(mac.last_seen_at))}` : ""}</span>`;
    const macList = !mac.set_up
      ? row({ title: "Not set up", sub: "Reminders, Notes, iCloud Drive, Maps, Messages and Health work through a small helper on your Mac. See the Mac helper guide." })
      : row({ title: "Messages", sub: on(v.ENABLE_IMESSAGE) ? (on(v.IMESSAGE_ALLOW_SEND) ? "Reads chats, sends after your approval" : "Reads chats") : "", ...state(v.ENABLE_IMESSAGE), button: true, attrs: 'data-page="messages"' })
        + MAC_SWITCHES.map(([label, key]) => sw(key, label, on(v[key]) && !mac.online ? "Waits until your Mac is back" : "")).join("")
        + row({ title: "Shortcuts and timing", sub: v.SHORTCUTS_ALLOW ? esc(v.SHORTCUTS_ALLOW) : "No Shortcuts allowed", button: true, attrs: 'data-page="helper"' });
    const only = wide() ? (SECTIONS.some(([id]) => id === this.section) ? this.section : SECTIONS[0][0]) : null;
    const head = (title, aside) => (only && !aside ? `<div style="height:14px"></div>` : section(title, aside));   // the page title already says it
    const groups = {
      security: head("Sign-in and security") + `<ion-list class="inset">
        ${row({ title: "Notifications", button: true, attrs: 'data-nav="icm-notifications"' })}
        ${row({ title: "Passkeys", note: String(sec.passkeys.length), button: true, attrs: 'data-nav="icm-passkeys"' })}
        ${row({ title: "Connected apps", note: String(sec.apps.length), button: true, attrs: 'data-nav="icm-apps"' })}
        ${row({ title: "Owner password", ...state(sec.password_login), button: true, attrs: 'data-nav="icm-password"' })}
        ${row({ title: "This device", sub: `Signed in with ${esc(sec.via)}, ${esc(ago(sec.signed_in_at))}` })}
        <ion-item button detail="false" data-signout><ion-label color="danger">Sign out</ion-label></ion-item></ion-list>`,
      accounts: head("Connected accounts") + `<ion-list class="inset">
        ${row({ title: "Connected accounts", sub: "Where mail, calendar and contacts come from", note: accts == null ? "" : String(accts), button: true, attrs: 'data-nav="icm-accounts"' })}</ion-list>`,
      icloud: head("iCloud") + `<ion-list class="inset">
        ${row({ title: "Mail", ...state(v.ENABLE_MAIL), button: true, attrs: 'data-page="mail"' })}
        ${row({ title: "Calendar", ...state(v.ENABLE_CALENDAR), button: true, attrs: 'data-page="calendar"' })}
        ${row({ title: "Contacts", ...state(v.ENABLE_CONTACTS), button: true, attrs: 'data-page="contacts"' })}</ion-list>`,
      mac: head("Your Mac", macAside) + `<ion-list class="inset">${macList}</ion-list>`,
      safety: head("Safety") + `<ion-list class="inset">
        ${sw("READ_ONLY", "Read-only server", "Nothing can be sent, moved or deleted")}
        ${sw("ALLOW_PERMANENT_DELETE", "Allow emptying Trash")}
        ${row({ title: "Only these tools", note: v.TOOLS ? "Some" : "All", button: true, attrs: 'data-page="tools"' })}</ion-list>`,
      server: head("Account and server") + `<ion-list class="inset">
        ${row({ title: "Account", sub: esc(g("accounts").summary || ""), button: true, attrs: 'data-page="accounts"' })}
        ${row({ title: "Activity log", button: true, attrs: 'data-page="log"' })}
        ${row({ title: "Server", note: esc(s.version), button: true, attrs: 'data-nav="icm-server"' })}</ion-list>`,
    };
    // a phone shows every group in one list; a wide screen shows the one chosen in its own side list
    const heading = only ? SECTIONS.find(([id]) => id === only)[1] : "Settings";
    this.querySelector("[data-body]").innerHTML = titleBlock(heading, esc(s.host))
      + (st.restart_needed ? `<div class="notice pause"><span>Saved changes apply after a restart. Agents are disconnected for about half a minute.</span>
          <ion-button size="small" data-restart>Restart now</ion-button></div>` : "")
      + (st.editable ? "" : `<div class="notice pause"><span>Settings can only be viewed here.</span></div>`)
      + SECTIONS.filter(([id]) => !only || id === only).map(([id]) => groups[id]).join("");
    this.wire();
  }

  wire() {
    this.querySelectorAll("[data-nav]").forEach((n) => n.addEventListener("click", () => push(this, n.dataset.nav)));
    this.querySelectorAll("[data-page]").forEach((n) => n.addEventListener("click", () => push(this, "icm-group", { page: n.dataset.page })));
    this.querySelector("[data-signout]")?.addEventListener("click", async () => {
      if (!(await ask("Sign out?", "You need your passkey or the password to come back.", "Sign out", { danger: true }))) return;
      try { await post("/signout"); } catch (e) { /* signed out anyway */ }
      location.reload();
    });
    this.querySelector("[data-restart]")?.addEventListener("click", async () => {
      try { const r = await post("/settings/restart"); toast(r.note, { duration: 8000 }); setTimeout(() => location.reload(), 30000); } catch (e) { fail(e); }
    });
    this.querySelectorAll("[data-switch]").forEach((n) => n.addEventListener("ionChange", async (e) => {
      try { toast(await saveKeys(this.st, { [n.dataset.switch]: e.detail.checked })); } catch (err) { fail(err); }
      this.load();
    }));
  }
}
customElements.define("icm-settings", Settings);

// -- one settings page --------------------------------------------------------------------------------------------------
class Group extends HTMLElement {
  async connectedCallback() {
    const body = page(this, "", "Settings");
    let st;
    try { st = await get("/settings"); } catch (e) { fail(e); return; }
    const cfg = PAGES[this.page] || { slug: this.page };
    const g = st.groups.find((x) => x.slug === cfg.slug);
    if (!g) { body.innerHTML = titleBlock("Not found", "There is no such settings page."); return; }
    this.st = st; this.changed = {};
    if (!g.set_up) { body.innerHTML = titleBlock(cfg.title || g.title, esc(g.summary)); return; }
    const fields = g.fields.filter((f) => (!cfg.sections || cfg.sections.includes(f.section)) && (!cfg.keys || cfg.keys.includes(f.key))
      && !(cfg.hide || []).includes(f.key) && f.key !== cfg.lead);
    const sections = {};
    fields.forEach((f) => { (sections[f.section || ""] = sections[f.section || ""] || []).push(f); });
    const lead = cfg.lead ? st.groups.flatMap((x) => x.fields).find((f) => f.key === cfg.lead) : null;
    let h = titleBlock(cfg.title || g.title, esc(cfg.keys ? (fields[0] || {}).help || "" : g.lede));
    if (lead) h += `<ion-list class="inset" style="margin-top:14px!important"><ion-item><ion-toggle data-key="${esc(lead.key)}" ${lead.value ? "checked" : ""} ${st.editable ? "" : "disabled"}>
      ${esc(cfg.leadLabel || `Agents can use ${cfg.title}`)}</ion-toggle></ion-item></ion-list>`;
    if (cfg.sendMode) {
      h += section("When agents send mail") + `<div style="padding:6px 16px 0"><ion-segment value="${esc(g.send_mode)}" data-sendmode ${st.editable ? "" : "disabled"}>
        <ion-segment-button value="drafts"><ion-label>Drafts</ion-label></ion-segment-button>
        <ion-segment-button value="approval"><ion-label>After approval</ion-label></ion-segment-button>
        <ion-segment-button value="direct"><ion-label>Directly</ion-label></ion-segment-button></ion-segment></div>
        <p class="note" data-sendnote>${esc(SEND_NOTES[g.send_mode])}</p>`;
    }
    for (const [name, list] of Object.entries(sections)) {
      const helps = list.filter((f) => f.help && !cfg.keys).map((f) => `${f.label}: ${f.help}`);
      const env = list.filter((f) => f.source === "environment").map((f) => f.label);
      h += (name && Object.keys(sections).length > 1 ? section(name) : `<div style="height:14px"></div>`)
        + `<ion-list class="inset">${list.map((f) => this.field(f, st.editable)).join("")}</ion-list>`
        + (helps.length || env.length ? `<p class="note">${esc(helps.join(" "))}${env.length ? ` ${esc(env.join(", "))} ${env.length === 1 ? "comes" : "come"} from the server's environment.` : ""}</p>` : "");
    }
    const here = fields.filter((f) => f.set_here);
    if (here.length && st.editable) {
      h += section("Changed here") + `<ion-list class="inset">${here.map((f) => `<ion-item><ion-label class="ion-text-wrap"><h2>${esc(f.label)}</h2>
        <p>${esc(f.display)}</p></ion-label><ion-button slot="end" fill="clear" size="small" data-reset="${esc(f.key)}" data-slug="${esc(cfg.slug)}">Reset</ion-button></ion-item>`).join("")}</ion-list>`;
    }
    if (cfg.slug === "accounts" && st.editable) {
      h += section("App-specific password") + `<ion-list class="inset"><ion-item><ion-input type="password" label="New app-specific password"
        label-placement="stacked" placeholder="abcd-efgh-ijkl-mnop" data-apppw></ion-input></ion-item></ion-list>
        <p class="note">Create it at account.apple.com. iCloud checks it before anything is saved.</p>
        <div class="acts"><ion-button fill="outline" color="medium" data-apppw-save>Replace password</ion-button></div>`;
    }
    body.innerHTML = h;
    if (st.editable && (fields.length || lead)) this.querySelector("[data-end]").innerHTML = `<ion-button strong disabled data-save>Save</ion-button>`;
    this.wire();
  }

  field(f, editable) {
    const dis = editable ? "" : "disabled";
    const attrs = `data-key="${esc(f.key)}" ${dis}`;
    if (f.kind === "bool") return `<ion-item><ion-toggle ${attrs} ${f.value ? "checked" : ""}>${esc(f.label)}</ion-toggle></ion-item>`;
    if (f.kind === "choice") {
      return `<ion-item><ion-select ${attrs} label="${esc(f.label)}" interface="action-sheet" value="${esc(String(f.value).toLowerCase())}">
        ${f.choices.map(([c, l]) => `<ion-select-option value="${esc(c)}">${esc(l)}</ion-select-option>`).join("")}</ion-select></ion-item>`;
    }
    if (f.kind === "textarea") return `<ion-item><ion-textarea ${attrs} label="${esc(f.label)}" label-placement="stacked" auto-grow="true" value="${esc(f.value)}"></ion-textarea></ion-item>`;
    const type = f.kind === "int" ? `type="number" inputmode="numeric" ${f.min != null ? `min="${f.min}"` : ""} ${f.max != null ? `max="${f.max}"` : ""}` : `type="text" autocomplete="off"`;
    return `<ion-item><ion-input ${attrs} ${type} label="${esc(f.label)}${f.unit_label ? ` (${esc(f.unit_label)})` : ""}" label-placement="stacked"
      value="${esc(f.value)}" ${f.kind === "list" ? 'placeholder="Comma-separated"' : ""}></ion-input></ion-item>`;
  }

  wire() {
    const save = this.querySelector("[data-save]");
    const mark = (k, val) => { this.changed[k] = val; if (save) save.disabled = false; };
    this.querySelectorAll("ion-toggle[data-key]").forEach((n) => n.addEventListener("ionChange", (e) => mark(n.dataset.key, e.detail.checked)));
    this.querySelectorAll("ion-select[data-key]").forEach((n) => n.addEventListener("ionChange", (e) => mark(n.dataset.key, e.detail.value)));
    this.querySelectorAll("ion-input[data-key],ion-textarea[data-key]").forEach((n) => n.addEventListener("ionInput", (e) => mark(n.dataset.key, e.detail.value)));
    this.querySelector("[data-sendmode]")?.addEventListener("ionChange", (e) => {
      const mode = e.detail.value;
      this.querySelector("[data-sendnote]").textContent = SEND_NOTES[mode];
      mark("ALLOW_SEND", mode !== "drafts"); mark("SEND_REQUIRES_APPROVAL", mode !== "direct");
    });
    save?.addEventListener("click", async () => {
      this.querySelectorAll("[data-key]").forEach((n) => n.classList.remove("ion-invalid", "ion-touched"));
      try {
        toast(await saveKeys(this.st, this.changed));
        window.dispatchEvent(new Event("icm:settings"));
        this.closest("ion-nav").pop();
      } catch (e) {
        Object.entries(e.body?.fields || {}).forEach(([k, why]) => {
          const n = this.querySelector(`[data-key="${CSS.escape(k)}"]`);
          if (n) { n.classList.add("ion-invalid", "ion-touched"); n.errorText = why; }
        });
        fail(e);
      }
    });
    this.querySelectorAll("[data-reset]").forEach((b) => b.addEventListener("click", async () => {
      try { const r = await post(`/settings/${b.dataset.slug}/reset`, { key: b.dataset.reset }); toast(r.note); window.dispatchEvent(new Event("icm:settings")); this.closest("ion-nav").pop(); }
      catch (e) { fail(e); }
    }));
    this.querySelector("[data-apppw-save]")?.addEventListener("click", async () => {
      const value = await this.querySelector("[data-apppw]").getInputElement().then((i) => i.value);
      try { const r = await post("/settings/accounts/app-password", { password: value }); toast(r.note); window.dispatchEvent(new Event("icm:settings")); }
      catch (e) { fail(e); }
    });
  }
}
customElements.define("icm-group", Group);
const SEND_NOTES = { drafts: "Agents save drafts; you send them in Mail.", approval: "Agents queue mail; you send it from Home.",
  direct: "Mail leaves at once. Your client's own prompt is the only check." };

// -- passkeys -----------------------------------------------------------------------------------------------------------
class Passkeys extends HTMLElement {
  connectedCallback() { this.body = page(this, "", "Settings"); this.load(); }

  async load() {
    let sec;
    try { sec = await get("/security"); } catch (e) { fail(e); return; }
    const can = sec.passkeys_available && window.Passkey && window.Passkey.available();
    this.body.innerHTML = titleBlock("Passkeys", "Sign in with Face ID, Touch ID or a security key, on this address only.")
      + (sec.passkeys.length ? section("On this server") + `<ion-list class="inset">${sec.passkeys.map((k) => `<ion-item>
        <ion-label class="ion-text-wrap"><h2>${esc(k.name)}</h2><p>Added ${esc(ago(k.created_at))} · used ${esc(ago(k.last_used_at))}</p></ion-label>
        <ion-button slot="end" fill="clear" color="danger" size="small" data-remove="${esc(k.id)}" data-name="${esc(k.name)}">Remove</ion-button></ion-item>`).join("")}</ion-list>` : "")
      + (can ? section("Add a passkey") + `<ion-list class="inset"><ion-item><ion-input label="Name" label-placement="stacked" placeholder="For example: iPhone" maxlength="40" data-name-input></ion-input></ion-item></ion-list>
        <div class="acts"><ion-button data-add>Add a passkey</ion-button></div>`
        : `<p class="note">${sec.passkeys_available ? "This browser cannot create passkeys here." : "This server cannot use passkeys: the optional webauthn package is not installed."}</p>`);
    this.querySelectorAll("[data-remove]").forEach((b) => b.addEventListener("click", async () => {
      if (!(await ask(`Remove “${b.dataset.name}”?`, "You can no longer sign in with it.", "Remove", { danger: true }))) return;
      try { const r = await post(`/security/passkeys/${encodeURIComponent(b.dataset.remove)}/remove`); toast(r.note); this.load(); window.dispatchEvent(new Event("icm:settings")); }
      catch (e) { fail(e); }
    }));
    this.querySelector("[data-add]")?.addEventListener("click", async () => {
      const name = await this.querySelector("[data-name-input]").getInputElement().then((i) => i.value);
      const run = () => window.Passkey.register(name, token());
      try {
        try { await run(); } catch (e) {
          if (!e.confirm) throw e;
          await confirmNow();
          await run();
        }
        toast("Passkey added. You can now sign in with it."); this.load(); window.dispatchEvent(new Event("icm:settings"));
      } catch (e) { fail(e); }
    });
  }
}
customElements.define("icm-passkeys", Passkeys);

// -- connected apps -----------------------------------------------------------------------------------------------------
class Apps extends HTMLElement {
  connectedCallback() { this.body = page(this, "", "Settings"); this.load(); }

  async load() {
    let sec;
    try { sec = await get("/security"); } catch (e) { fail(e); return; }
    this.body.innerHTML = titleBlock("Connected apps", "A signed-out app has to connect again, and that needs you.")
      + (sec.apps.length ? `<ion-list class="inset" style="margin-top:14px!important">${sec.apps.map((a) => `<ion-item>
        <ion-label class="ion-text-wrap"><h2>${esc(a.name)}</h2><p>${a.host ? `${esc(a.host)} · ` : ""}active ${esc(ago(a.last_used))}</p></ion-label>
        <ion-button slot="end" fill="clear" size="small" data-out="${esc(a.id)}" data-name="${esc(a.name)}">Sign out</ion-button></ion-item>`).join("")}</ion-list>
        <div class="acts"><ion-button fill="outline" color="danger" data-all>Sign out all apps</ion-button></div>`
        : `<div class="empty">No app is signed in.</div>`);
    this.querySelectorAll("[data-out]").forEach((b) => b.addEventListener("click", async () => {
      if (!(await ask(`Sign out ${b.dataset.name}?`, "It has to connect again, and that needs you.", "Sign out", { danger: true }))) return;
      try { const r = await post(`/security/apps/${encodeURIComponent(b.dataset.out)}/sign-out`); toast(r.note); this.load(); } catch (e) { fail(e); }
    }));
    this.querySelector("[data-all]")?.addEventListener("click", async () => {
      if (!(await ask("Sign out all apps?", "Every app has to connect again, and that needs you.", "Sign out all", { danger: true }))) return;
      try { const r = await post("/security/apps/sign-out-all"); toast(r.note); this.load(); } catch (e) { fail(e); }
    });
  }
}
customElements.define("icm-apps", Apps);

// -- owner password -----------------------------------------------------------------------------------------------------
class Password extends HTMLElement {
  async connectedCallback() {
    const body = page(this, "", "Settings");
    const [sec, st] = await Promise.all([get("/security"), get("/settings")]).catch((e) => { fail(e); return [null, null]; });
    if (!sec) return;
    const ed = st.editable ? "" : "disabled";
    body.innerHTML = titleBlock("Owner password", "It also approves new apps on the connect page.")
      + `<ion-list class="inset" style="margin-top:14px!important"><ion-item><ion-toggle data-login ${on(st.values.OWNER_PASSWORD_LOGIN) ? "checked" : ""} ${ed}>
        Sign in with the password</ion-toggle></ion-item></ion-list>
        <p class="note">${sec.passkeys.length ? "Off means passkey and Tailscale only." : "Stays on until you have a passkey."}</p>`
      + section("Change it") + `<ion-list class="inset"><ion-item><ion-input type="password" label="New owner password" label-placement="stacked"
        autocomplete="new-password" minlength="12" data-pw></ion-input></ion-item></ion-list>
        <p class="note">At least 12 characters. It applies after the server restarts.</p>
        <div class="acts"><ion-button data-save>Change password</ion-button></div>`;
    this.querySelector("[data-login]").addEventListener("ionChange", async (e) => {
      try { toast(await saveKeys(st, { OWNER_PASSWORD_LOGIN: e.detail.checked })); window.dispatchEvent(new Event("icm:settings")); } catch (err) { fail(err); }
    });
    this.querySelector("[data-save]").addEventListener("click", async () => {
      const value = await this.querySelector("[data-pw]").getInputElement().then((i) => i.value);
      try { const r = await post("/security/password", { password: value }); toast(r.note); this.closest("ion-nav").pop(); } catch (e) { fail(e); }
    });
  }
}
customElements.define("icm-password", Password);

// -- server -------------------------------------------------------------------------------------------------------------
class Server extends HTMLElement {
  connectedCallback() { this.body = page(this, "", "Settings"); this.load(); }

  async load() {
    let srv, st;
    try { [srv, st] = await Promise.all([get("/server"), get("/settings")]); } catch (e) { fail(e); return; }
    const areas = srv.areas.length ? srv.areas.map((a) => `<ion-item><ion-label class="ion-text-wrap"><h2>${esc(a.area)}</h2>${a.ok ? "" : `<p>${esc(a.error)}</p>`}</ion-label>
      <ion-note slot="end" class="${a.ok ? "state-on" : ""}" ${a.ok ? "" : 'style="color:var(--negative)"'}>${a.ok ? `Reachable · ${(a.ms / 1000).toFixed(1)} s` : "Failed"}</ion-note></ion-item>`).join("")
      : `<ion-item><ion-label><p>Not checked since the server started.</p></ion-label></ion-item>`;
    this.body.innerHTML = titleBlock("Server", `${esc(srv.host)} · version ${esc(srv.version)}${srv.paused ? " · paused" : ""}`)
      + section("Services", srv.checked_at ? `checked ${esc(ago(srv.checked_at))}` : "") + `<ion-list class="inset">${areas}</ion-list>
        <div class="acts"><ion-button fill="outline" color="medium" data-check>Check now</ion-button></div>`
      + section("Tools") + `<ion-list class="inset">${row({ title: "Offered", note: `${srv.tools} tools` })}${row({ title: "Connected apps", note: esc(srv.apps.join(", ") || "none") })}</ion-list>
        <div class="acts"><ion-button fill="outline" color="${srv.paused ? "primary" : "danger"}" data-pause>${srv.paused ? "Resume tools" : "Pause all tools"}</ion-button></div>
        <p class="note">Pausing makes every tool answer that the server is paused. Nobody is signed out.</p>`
      + st.server.map((grp) => section(grp.title) + `<ion-list class="inset">${grp.rows.map(([k, val]) => row({ title: esc(k), note: esc(val) })).join("")}</ion-list>`).join("")
      + `<p class="note">Set in the server's environment by whoever runs it. A browser cannot change these.</p>`;
    this.querySelector("[data-check]").addEventListener("click", async (e) => {
      e.target.disabled = true;
      try { await post("/server/check"); toast("Checked."); this.load(); } catch (err) { fail(err); e.target.disabled = false; }
    });
    this.querySelector("[data-pause]").addEventListener("click", async () => {
      if (!srv.paused && !(await ask("Pause all tools?", "Every tool then answers that the server is paused, until you resume.", "Pause", { danger: true }))) return;
      try { const r = await post("/server/pause", { paused: !srv.paused }); toast(r.note); window.dispatchEvent(new Event("icm:changed")); this.load(); } catch (e) { fail(e); }
    });
  }
}
customElements.define("icm-server", Server);

// -- notifications (Web Push) -------------------------------------------------------------------------------------------
function deviceLabel() {
  const ua = navigator.userAgent;
  const dev = /iPhone/.test(ua) ? "iPhone" : /iPad|Macintosh.*Mobile/.test(ua) || (navigator.maxTouchPoints > 1 && /Macintosh/.test(ua)) ? "iPad"
    : /Android/.test(ua) ? "Android" : /Macintosh/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows" : "This device";
  const br = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) && !/CriOS/.test(ua) ? "Chrome" : "Safari";
  const app = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone ? "app" : br;
  return `${dev} · ${app}`;
}

async function sha16(text) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(d)).map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 16);
}

function keyBytes(b64) {
  const s = (b64 + "===".slice((b64.length + 3) % 4)).replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
}

class Notifications extends HTMLElement {
  connectedCallback() { this.body = page(this, "", "Settings"); this.load(); }

  async support() {
    const ios = /iPhone|iPad/.test(navigator.userAgent) || (navigator.maxTouchPoints > 1 && /Macintosh/.test(navigator.userAgent));
    const standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone;
    if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
      return ios && !standalone ? "ios-browser" : "none";
    }
    if (Notification.permission === "denied") return "blocked";
    return "ok";
  }

  async load() {
    let st;
    try { st = await get("/push"); } catch (e) { fail(e); return; }
    const support = await this.support();
    let mine = null;
    if (support === "ok") {
      const reg = await navigator.serviceWorker.getRegistration("/dashboard");
      const sub = reg && await reg.pushManager.getSubscription();
      if (sub) mine = await sha16(sub.endpoint);
    }
    this.st = st; this.mine = mine;
    const onHere = mine && st.devices.some((d) => d.id === mine);
    const others = st.devices.filter((d) => d.id !== mine);
    const toggle = support === "ok" ? `<ion-item><ion-toggle data-toggle ${onHere ? "checked" : ""}>Notify this device</ion-toggle></ion-item>`
      : `<ion-item><ion-toggle disabled>Notify this device</ion-toggle></ion-item>`;
    const why = { "ios-browser": "On iPhone and iPad this works only in the app on your Home Screen: in Safari tap Share, then Add to Home Screen, and open it from there.",
      blocked: "Notifications are blocked for this app. Allow them in the system settings, then come back.",
      none: "This browser cannot show notifications from a web app." }[support];
    this.body.innerHTML = titleBlock("Notifications", "Know when something waits for you.")
      + section("This device") + `<ion-list class="inset">${toggle}</ion-list>`
      + (why ? `<p class="note"${support === "blocked" ? ' style="color:var(--negative)"' : ""}>${esc(why)}</p>` : "")
      + (others.length ? section("Other devices") + `<ion-list class="inset">${others.map((d) => `<ion-item><ion-label class="ion-text-wrap">
          <h2>${esc(d.label)}</h2><p>Since ${esc(ago(d.created_at))}${d.last_sent_at ? ` · last note ${esc(ago(d.last_sent_at))}` : ""}${d.failing ? " · not reached last time" : ""}</p></ion-label>
          <ion-button slot="end" fill="clear" color="danger" size="small" data-remove="${esc(d.id)}" data-name="${esc(d.label)}">Remove</ion-button></ion-item>`).join("")}</ion-list>` : "")
      + section("How it looks") + `<ion-list class="inset"><ion-item><ion-label class="ion-text-wrap"><p style="font-size:13px!important">iCloud MCP · now</p>
          <h2>Approval needed</h2><p style="color:var(--text)!important">An email is waiting for your approval.</p></ion-label></ion-item></ion-list>
        <p class="note">A note says only what kind of item waits, never who it goes to or what it says. Tap it to open the item.</p>`
      + (st.devices.length ? `<div class="acts"><ion-button fill="outline" color="medium" data-test>Send a test</ion-button></div>` : "");
    this.querySelector("[data-toggle]")?.addEventListener("ionChange", (e) => (e.detail.checked ? this.turnOn() : this.turnOff()));
    this.querySelectorAll("[data-remove]").forEach((b) => b.addEventListener("click", async () => {
      if (!(await ask(`Stop notifications for ${b.dataset.name}?`, "It gets no more notes from this server.", "Remove", { danger: true }))) return;
      try { const r = await post(`/push/${encodeURIComponent(b.dataset.remove)}/remove`); toast(r.note); this.load(); } catch (e) { fail(e); }
    }));
    this.querySelector("[data-test]")?.addEventListener("click", async () => {
      try { const r = await post("/push/test", {}); toast(r.note); } catch (e) { fail(e); }
    });
  }

  async turnOn() {
    try {
      const perm = await Notification.requestPermission();
      if (perm !== "granted") { toast("Notifications were not allowed.", { bad: true }); this.load(); return; }
      const reg = await navigator.serviceWorker.register("/dashboard/sw.js", { scope: "/dashboard" });
      await navigator.serviceWorker.ready;
      const sub = (await reg.pushManager.getSubscription())
        || await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(this.st.public_key) });
      const r = await post("/push/subscribe", { subscription: sub.toJSON(), label: deviceLabel() });
      toast(r.note);
    } catch (e) { fail(e); }
    this.load();
  }

  async turnOff() {
    try {
      const reg = await navigator.serviceWorker.getRegistration("/dashboard");
      const sub = reg && await reg.pushManager.getSubscription();
      if (sub) {
        const id = await sha16(sub.endpoint);
        await post(`/push/${encodeURIComponent(id)}/remove`).catch(() => {});
        await sub.unsubscribe();
      }
      toast("No more notifications on this device.");
    } catch (e) { fail(e); }
    this.load();
  }
}
customElements.define("icm-notifications", Notifications);

export { loadSession };
