// Connected accounts: what the /setup assistant has saved, read only. Adding, changing and disconnecting stay in that assistant.
import { get } from "./api.js";
import { esc, fail, section, tag, titleBlock, wide } from "./ui.js";
import { page } from "./settings.js";

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
    const rows = r.accounts.map((a) => `<ion-item>
      <span class="av" slot="start">${esc((a.label || "?")[0].toUpperCase())}</span>
      <ion-label class="ion-text-wrap"><h2>${esc(a.label)}</h2><p style="overflow-wrap:anywhere">${esc(a.address)}</p>
        <div class="chips">${a.read_only ? tag("Read only", "warn") : ""}${tag(a.sign_in)}${a.areas.map((x) => tag(x)).join("")}${a.from_environment ? tag("Set in the server's environment") : ""}</div>
      </ion-label></ion-item>`).join("");
    this.body.innerHTML = titleBlock("Connected accounts", "Everything your assistant reads from or writes through. Sending always waits for your approval.")
      + (rows ? section("Connected", `<span class="num">${r.accounts.length}</span>`) + `<ion-list class="inset">${rows}</ion-list>`
        : `<div class="empty"><b>No account is connected yet.</b>Open the setup assistant to connect one.</div>`)
      + section("Add or change an account")
      + (r.setup_url
        ? `<ion-list class="inset"><ion-item button detail="true" href="${esc(r.setup_url)}" target="_blank" rel="noopener">
          <ion-label class="ion-text-wrap"><h2>Setup assistant</h2><p>Connect, change or disconnect an account. It asks you to sign in through Tailscale or Cloudflare Access.</p></ion-label></ion-item></ion-list>`
        : `<div class="notice"><span>The setup assistant is off on this server. It needs OWNER_TAILSCALE_LOGINS or a Cloudflare Access sign-in, so that it knows it is you.</span></div>`)
      + `<p class="note">This list only shows what the setup assistant saved. Passwords stay on this server and are never shown here. Mail, calendar and contacts each come from one account.</p>`;
  }
}
customElements.define("icm-accounts", Accounts);
