// Who a messaging plugin may write to, for any plugin that offers a recipient list (Beeper is the first): the "Sending" part of
// its page, the chats on the list by name, finding a chat to add, and the networks. Names come live from the service each time;
// the server keeps only the ids. Changes go through /settings/<group> like every other setting and apply after a restart.
import { get } from "./api.js";
import { esc, ago, toast, fail, ask, section, titleBlock, row } from "./ui.js";
import { page, saveKeys, wireSwitches } from "./settings.js";

const STATE_WORDS = { no_read_key: "Names cannot be read: the server has no read key.", unreachable: "The service cannot be reached right now.",
  refused: "The service refused the read key.", unreadable: "The service gave an answer that could not be read." };

// "WhatsApp · one person · last message yesterday", "Telegram · group of 5"
export function chatLine(c, withActivity = true) {
  const who = c.group ? (c.people ? `group of ${c.people >= 100 ? "100+" : c.people}` : "group") : "one person";
  const when = withActivity && c.last_activity ? `last message ${ago(Date.parse(c.last_activity) / 1000)}` : "";
  return [c.network, who, when].filter(Boolean).join(" · ");
}

function listOf(ids, any) { return [...(any ? ["*"] : []), ...ids].join(","); }

// The "Sending" part of a plugin's page, from its data and the live recipients answer (null while it is not there)
export function sendingSection(p, rec) {
  const m = p.messages;
  const names = rec ? rec.chats.filter((c) => c.found).map((c) => c.title) : [];
  const listNote = !rec ? "" : rec.any ? "Any chat" : `${rec.ids.length} chat${rec.ids.length === 1 ? "" : "s"}`;
  const nets = rec && m.networks_key ? (rec.networks.includes("*") ? rec.all_networks : rec.networks) : [];
  const netNote = !rec || !m.networks_key ? "" : rec.networks.includes("*") ? "All" : `${rec.networks.length} of ${rec.all_networks.length || rec.networks.length}`;
  return section("Sending") + `<ion-list class="inset">
      ${row({ title: "Send messages", sub: "Every message waits for your approval", note: m.sending ? "On" : "Off", noteClass: m.sending ? "state-on" : "state-off" })}
      ${row({ title: "Who it may write to", sub: esc(names.slice(0, 3).join(", ") + (names.length > 3 ? ` and ${names.length - 3} more` : "")), note: esc(listNote), button: true, attrs: "data-who" })}
      ${m.networks_key ? row({ title: "Networks", sub: esc(nets.join(", ")), note: esc(netNote), button: true, attrs: "data-nets" }) : ""}
      ${m.groups_key ? `<ion-item><ion-toggle data-switch="${esc(m.groups_key)}" ${rec && rec.groups ? "checked" : ""}>Group chats<br>
        <small class="sub">A group reaches everyone in it. Only groups you add by name.</small></ion-toggle></ion-item>` : ""}</ion-list>
    <p class="note">A chat must pass all three: it is on the list, its network is on, and it is not a group unless groups are on.</p>`;
}

// Reading, while it is not built: where it will go, greyed
export function readingSection(p) {
  return section("Reading", `<span style="color:var(--note-ink)">planned, not built yet</span>`) + `<ion-list class="inset planned">
      <ion-item><ion-toggle disabled>Read chats<br><small class="sub">Your assistant can list, read and search chats</small></ion-toggle></ion-item>
      ${row({ title: "Which chats", sub: "All except hidden ones" })}
      ${row({ title: "Hidden chats", sub: "Never shown to your assistant" })}
      ${row({ title: "Only the last", note: "30 days" })}
      <ion-item><ion-toggle disabled checked>Hide service senders<br><small class="sub">Codes, delivery notices, short numbers</small></ion-toggle></ion-item></ion-list>
    <p class="note">The same filters iMessage has today. Reading happens live through ${esc(p.name)}; nothing is stored here.</p>`;
}

export function wireSending(el, p, st, reload) {
  el.querySelector("[data-who]")?.addEventListener("click", () => el.closest("ion-nav").push("icm-recipients", { pid: p.id }));
  el.querySelector("[data-nets]")?.addEventListener("click", () => el.closest("ion-nav").push("icm-networks", { pid: p.id }));
  wireSwitches(el, st, reload);
}

// -- who it may write to --------------------------------------------------------------------------------------------------
class Recipients extends HTMLElement {
  connectedCallback() {
    this.body = page(this, "", "Back");
    this.load();
  }

  async load() {
    this.body.innerHTML = titleBlock("Who it may write to", "Asking for the names…") + `<div class="empty"><ion-spinner></ion-spinner></div>`;
    let r, st;
    try { [r, st] = await Promise.all([get(`/plugins/${encodeURIComponent(this.pid)}/recipients`), get("/settings")]); } catch (e) { fail(e); return; }
    this.r = r; this.st = st;
    const rows = r.state !== "ok" ? row({ title: esc(STATE_WORDS[r.state] || "Names cannot be read right now."), sub: `${r.ids.length} chat${r.ids.length === 1 ? "" : "s"} on the list` })
      : r.chats.map((c) => `<ion-item><ion-label class="ion-text-wrap">${c.found ? `<h2>${esc(c.title)}</h2><p>${esc(chatLine(c))}</p>`
          : `<h2 style="color:var(--note-ink)">Chat not found</h2><p>${esc(r.name)} no longer knows this chat (it may have been deleted)</p>`}</ion-label>
          <ion-button slot="end" fill="clear" color="danger" size="small" data-remove="${esc(c.id)}">Remove</ion-button></ion-item>`).join("");
    this.body.innerHTML = titleBlock("Who it may write to", "Your assistant can ask to write to these chats. You still approve every message.")
      + `<ion-list class="inset" style="margin-top:14px!important">${rows}
        <ion-item button detail="false" data-add><ion-label color="primary">Add a chat</ion-label></ion-item></ion-list>
        <p class="note">Names come live from ${esc(r.name)} each time you open this page. The server only keeps the chat ids.</p>`
      + section("Instead of a list") + `<ion-list class="inset"><ion-item><ion-toggle data-any ${r.any ? "checked" : ""}>Any chat<br>
        <small class="sub">Your assistant may ask to write to anyone except groups. You still approve every message.</small></ion-toggle></ion-item></ion-list>`;
    this.querySelectorAll("[data-remove]").forEach((b) => b.addEventListener("click", () => this.save(r.ids.filter((x) => x !== b.dataset.remove), r.any)));
    this.querySelector("[data-any]").addEventListener("ionChange", (e) => this.save(r.ids, e.detail.checked));
    this.querySelector("[data-add]").addEventListener("click", () => this.closest("ion-nav").push("icm-add-chat", { pid: this.pid }));
  }

  async save(ids, any) {
    try { toast(await saveKeys(this.st, { [this.r.keys.recipients]: listOf(ids, any) })); window.dispatchEvent(new Event("icm:settings")); } catch (e) { fail(e); }
    this.load();
  }
}
customElements.define("icm-recipients", Recipients);

// -- add a chat -----------------------------------------------------------------------------------------------------------
class AddChat extends HTMLElement {
  async connectedCallback() {
    const body = page(this, "", "Back");
    body.innerHTML = titleBlock("Add a chat", "Search your chats by name.")
      + `<div style="padding:8px 8px 0"><ion-searchbar data-q debounce="400" placeholder="Name of a person or group"></ion-searchbar></div>
        <div data-results></div>`;
    this.querySelector("[data-q]").addEventListener("ionInput", (e) => this.find(e.detail.value || ""));
    try { [this.rec, this.st] = await Promise.all([get(`/plugins/${encodeURIComponent(this.pid)}/recipients`), get("/settings")]); } catch (e) { fail(e); return; }
    this.find("");
  }

  async find(q) {
    const box = this.querySelector("[data-results]");
    box.innerHTML = `<div class="empty"><ion-spinner></ion-spinner></div>`;
    let r;
    try { r = await get(`/plugins/${encodeURIComponent(this.pid)}/find?q=${encodeURIComponent(q)}`); } catch (e) { box.innerHTML = ""; fail(e); return; }
    if ((this.querySelector("[data-q]").value || "") !== q) return;          // a newer search is on its way
    const rows = r.chats.map((c, i) => `<ion-item><ion-label class="ion-text-wrap"><h2>${esc(c.title)}</h2><p>${esc(chatLine(c, !q))}</p></ion-label>
      ${c.added ? `<ion-note slot="end">Added</ion-note>` : `<ion-button slot="end" fill="clear" size="small" data-add="${i}">Add</ion-button>`}</ion-item>`).join("");
    box.innerHTML = section(q ? "Results" : "Recent chats")
      + (r.state !== "ok" ? `<div class="notice pause"><span>${esc(STATE_WORDS[r.state] || "Nothing could be read right now.")}</span></div>`
        : rows ? `<ion-list class="inset">${rows}</ion-list>` : `<div class="empty">No chat with that name.</div>`)
      + (q ? "" : `<p class="note">Adding a group asks once more, because a message there reaches everyone in it.</p>`);
    box.querySelectorAll("[data-add]").forEach((b) => b.addEventListener("click", () => this.add(r.chats[Number(b.dataset.add)], b)));
  }

  async add(c, button) {
    let checked;
    try { checked = (await get(`/plugins/${encodeURIComponent(this.pid)}/resolve?id=${encodeURIComponent(c.id)}`)).chat; } catch (e) { fail(e); return; }
    const values = {}, keys = this.rec.keys;
    if (checked.group) {
      const reach = checked.people ? `A message there reaches all ${checked.people >= 100 ? "100+" : checked.people} people.` : "A message there reaches everyone in it.";
      const off = keys.groups && !this.rec.groups ? " Group chats are off; adding it turns them on." : "";
      if (!(await ask(`Add ${checked.title}?`, reach + off, "Add"))) return;
      if (off) values[keys.groups] = true;
    }
    this.rec.ids = [...this.rec.ids, checked.id];
    values[keys.recipients] = listOf(this.rec.ids, this.rec.any);
    try {
      toast(await saveKeys(this.st, values));
      if (values[keys.groups]) this.rec.groups = true;
      button.replaceWith(Object.assign(document.createElement("ion-note"), { slot: "end", textContent: "Added" }));
      window.dispatchEvent(new Event("icm:settings"));
    } catch (e) { this.rec.ids = this.rec.ids.filter((x) => x !== checked.id); fail(e); }
  }
}
customElements.define("icm-add-chat", AddChat);

// -- networks -------------------------------------------------------------------------------------------------------------
class Networks extends HTMLElement {
  async connectedCallback() {
    const body = page(this, "", "Back");
    let r, st;
    try { [r, st] = await Promise.all([get(`/plugins/${encodeURIComponent(this.pid)}/recipients`), get("/settings")]); } catch (e) { fail(e); return; }
    const all = r.all_networks.length ? r.all_networks : r.networks.filter((x) => x !== "*");
    const on = (n) => r.networks.includes("*") || r.networks.some((x) => x.toLowerCase() === n.toLowerCase());
    body.innerHTML = titleBlock("Networks", "Your assistant may ask to write only on the networks that are on.")
      + (all.length ? `<ion-list class="inset" style="margin-top:14px!important">${all.map((n) => `<ion-item><ion-toggle data-net="${esc(n)}" ${on(n) ? "checked" : ""}>${esc(n)}</ion-toggle></ion-item>`).join("")}</ion-list>`
        : `<div class="notice pause"><span>${esc(STATE_WORDS.no_read_key.replace("Names", "The networks"))}</span></div>`)
      + `<p class="note">New networks are added in the ${esc(r.name)} app. One that is added later is on only while all networks are on.</p>`;
    if (!all.length) return;
    this.querySelector("[data-end]").innerHTML = `<ion-button strong disabled data-save>Save</ion-button>`;
    const save = this.querySelector("[data-save]");
    this.querySelectorAll("[data-net]").forEach((n) => n.addEventListener("ionChange", () => { save.disabled = false; }));
    save.addEventListener("click", async () => {
      const chosen = [...this.querySelectorAll("[data-net]")].filter((n) => n.checked).map((n) => n.dataset.net);
      const value = chosen.length === all.length ? "*" : chosen.join(",");
      try { toast(await saveKeys(st, { [r.keys.networks]: value })); window.dispatchEvent(new Event("icm:settings")); this.closest("ion-nav").pop(); } catch (e) { fail(e); }
    });
  }
}
customElements.define("icm-networks", Networks);
