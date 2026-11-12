// Accounts, read only: what the /setup assistant has saved, and the networks behind Beeper. Adding, changing and disconnecting
// an assistant's account stay in that assistant; the rest is set in the server's environment.
import { get } from "./api.js";
import { esc, fail, section, tag, titleBlock, wide } from "./ui.js";
import { page } from "./settings.js";

const ENV = "Set in the server's environment";

// One row per network Beeper is signed in to, or one row "Beeper" that says why there are none
function beeperRows(b) {
  if (!b) return [];
  const chips = (extra = "") => `<div class="chips">${tag("Through Beeper")}${extra}${tag(ENV)}</div>`;
  const how = (b.sending ? tag("Sends after your approval") : tag("Sending off", "warn")) + tag(b.reading ? "Reads chats" : "Reading: not yet");
  if (b.problem || !b.networks.length) {
    const why = b.problem || "Beeper lists no connected network.";
    return [`<ion-item><span class="av" slot="start">B</span>
      <ion-label class="ion-text-wrap"><h2>Beeper</h2><p>${esc(why)}</p>${chips()}</ion-label></ion-item>`];
  }
  return b.networks.map((n) => `<ion-item>
      <span class="av" slot="start">${esc((n.network || "?")[0].toUpperCase())}</span>
      <ion-label class="ion-text-wrap"><h2>${esc(n.network)}</h2><p style="overflow-wrap:anywhere">${esc(n.user)}</p>
        <p style="overflow-wrap:anywhere">Account ID ${esc(n.account_id)}</p>${chips(how)}</ion-label></ion-item>`);
}

class Accounts extends HTMLElement {
  connectedCallback() {
    // on a wide screen this page is the root of Settings (the side list has the way back), on a phone it is one step in
    this.body = page(this, "", wide() ? "" : "Settings");
    if (wide()) this.querySelector("ion-header")?.remove();      // no bar of its own: the side list is the way back
    this.load();
  }

  async load() {
    let r;
    try { r = await get("/accounts"); } catch (e) { fail(e); return; }
    // Which account is the default matters only where an area has more than one account; with one each, a tag on every row says nothing
    const held = {};
    r.accounts.forEach((a) => a.areas.forEach((x) => { held[x] = (held[x] || 0) + 1; }));
    const defaultTag = (a) => { const shared = (a.default_for || []).filter((x) => held[x] > 1); return shared.length ? tag("Default for " + shared.join(", ")) : ""; };
    const rows = r.accounts.map((a) => `<ion-item>
      <span class="av" slot="start">${esc((a.label || "?")[0].toUpperCase())}</span>
      <ion-label class="ion-text-wrap"><h2>${esc(a.label)}</h2><p style="overflow-wrap:anywhere">${esc(a.address)}</p>
        <div class="chips">${defaultTag(a)}${a.read_only ? tag("Read only", "warn") : ""}${tag(a.sign_in)}${a.areas.map((x) => tag(x)).join("")}${a.from_environment ? tag(ENV) : ""}</div>
      </ion-label></ion-item>`).concat(beeperRows(r.beeper));
    const count = r.accounts.length + (r.beeper ? r.beeper.networks.length : 0);
    this.body.innerHTML = titleBlock("Accounts")
      + (rows.length ? section("Connected", count ? `<span class="num">${count}</span>` : "") + `<ion-list class="inset">${rows.join("")}</ion-list>`
        : `<div class="empty"><b>No account is connected yet.</b>Open the setup assistant to connect one.</div>`)
      + section("Add or change an account")
      + (r.setup_url
        ? `<ion-list class="inset"><ion-item button detail="true" href="${esc(r.setup_url)}" target="_blank" rel="noopener">
          <ion-label class="ion-text-wrap"><h2>Setup assistant</h2><p>Connect, change or disconnect an account. It asks you to sign in through Tailscale.</p></ion-label></ion-item></ion-list>`
        : `<div class="notice"><span>The setup assistant is off on this server. It needs OWNER_TAILSCALE_LOGINS, so that it knows it is you.</span></div>`)
      + `<p class="note">Change the setup assistant's accounts in the assistant. Whoever runs this server changes the accounts marked "${ENV}" and the Beeper networks. Passwords stay on this server and are never shown here. Mail, calendar and contacts each come from one account.</p>`;
  }
}
customElements.define("icm-accounts", Accounts);
