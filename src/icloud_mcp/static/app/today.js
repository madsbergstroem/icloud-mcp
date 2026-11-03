// Today: what waits for the owner's approval, and one request exactly as it will go out.
import { get, post, postNow, upload, removeFile, ApiError } from "./api.js";
import { esc, icon, ago, until, size, toast, fail, ask, sheet, section, titleBlock, wide, root } from "./ui.js";
import { app } from "./state.js";

function greeting() {
  const h = new Date().getHours();
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

function finding(waiting) {
  if (!waiting.length) return "Nothing waits for you. When an agent wants to send something, it shows up here.";
  const first = waiting[0];
  const n = waiting.length === 1 ? "One thing waits" : waiting.length === 2 ? "Two things wait" : `${waiting.length} things wait`;
  return `${n} for your approval. ${waiting.length === 1 ? "It is" : "The oldest is"} dropped ${until(first.expires_at)}.`;
}

function itemSub(w) {
  const what = w.kind === "mail" ? `Email${w.to ? ` to ${esc(w.to)}` : ""}` : w.kind === "calendar" ? `${esc(w.badge)}${w.to ? ` · ${esc(w.to)}` : ""}`
    : `iMessage${w.to ? ` to ${esc(w.to)}` : ""}`;
  const extra = w.blocked ? " · a file is missing" : w.warning ? " · check this first" : "";
  return `${what}${w.by ? ` · from ${esc(w.by)}` : ""}${extra}`;
}

class Today extends HTMLElement {
  connectedCallback() {
    this.selected = null;
    this.innerHTML = `<ion-content><div data-body></div></ion-content>`;
    this.onChange = () => this.load();
    window.addEventListener("icm:changed", this.onChange);
    window.addEventListener("icm:open", (this.onOpen = (e) => this.open(e.detail)));
    this.mq = window.matchMedia("(min-width: 900px)");
    this.mq.addEventListener("change", (this.onWidth = () => { if (this.data) this.render(); }));   // a window or an iPad turned
    this.load();
  }

  disconnectedCallback() {
    window.removeEventListener("icm:changed", this.onChange);
    window.removeEventListener("icm:open", this.onOpen);
    this.mq.removeEventListener("change", this.onWidth);
  }

  async load() {
    try { this.data = await get("/today"); } catch (e) { if (!(e instanceof ApiError && e.status === 401)) fail(e); return; }
    this.data.waiting = this.data.waiting.filter((w) => !app.hidden.has(w.key));      // a discard that can still be undone
    this.render();
    if (app.pendingOpen) { const k = app.pendingOpen; app.pendingOpen = null; this.open(k); }
  }

  notices() {
    const out = [];
    if (app.note && Date.now() < app.noteUntil) out.push(`<div class="notice ${app.noteBad ? "bad" : "ok"}"><span>${esc(app.note)}</span></div>`);
    if (this.data.paused) out.push(`<div class="notice pause"><span>Tools are paused. Agents get an answer that the server is paused.</span>
      <ion-button size="small" fill="clear" data-resume>Resume</ion-button></div>`);
    if (this.data.recent_burst > 2) out.push(`<div class="notice pause"><span>Agents asked for ${this.data.recent_burst} of these in the
      last hour. Approve only what you asked for.</span></div>`);
    const hint = window.InstallHint && window.InstallHint.state ? window.InstallHint.state() : "none";
    if (hint !== "none" && !app.hintClosed) out.push(`<div class="notice" style="background:var(--surface)"><span>Add this app to your
      Home Screen to get notifications${hint.startsWith("ios") ? ": in Safari tap Share, then Add to Home Screen" : ""}.</span>
      ${hint === "button" ? `<ion-button size="small" data-install>Install</ion-button>` : ""}<ion-button size="small" fill="clear" color="medium" data-hint-close>Close</ion-button></div>`);
    return out.join("");
  }

  list() {
    const d = this.data;
    const items = d.waiting.map((w) => `<ion-item button detail="true" data-key="${esc(w.key)}" class="${w.key === this.selected ? "sel" : ""}">
      <ion-label class="ion-text-wrap"><h2>${esc(w.title)}</h2><p>${itemSub(w)}</p></ion-label>
      <ion-note slot="end" class="num">${esc(ago(w.created_at).replace(" ago", ""))}</ion-note></ion-item>`).join("");
    const sent = d.sent_today.map((e) => `<ion-item class="sent"><ion-label class="ion-text-wrap"><h2>${esc(e.title)}</h2>
      <p>${esc([e.what, e.to ? `to ${e.to}` : ""].filter(Boolean).join(" "))}</p></ion-label>
      <ion-note slot="end" class="num">${esc(e.time)}</ion-note></ion-item>`).join("");
    return titleBlock(greeting(), esc(finding(d.waiting)))
      + `<div class="fresh ${app.online ? "" : "off"}"><i></i>${app.online ? `Live · updated ${esc(ago(app.updated))}` : "Offline · showing the last state"}</div>`
      + this.notices()
      + (d.waiting.length ? section("Waiting", `<span class="num">${d.waiting.length}</span>`) + `<ion-list class="inset">${items}</ion-list>` : "")
      + (sent ? section("Sent today") + `<ion-list class="inset">${sent}</ion-list>` : "");
  }

  render() {
    const body = this.querySelector("[data-body]");
    if (wide()) {
      body.innerHTML = `<div class="split"><div class="list">${this.list()}</div><div class="detail" data-detail></div></div>`;
      const first = this.selected && this.data.waiting.some((w) => w.key === this.selected) ? this.selected
        : (this.data.waiting[0] || {}).key;
      const detail = this.querySelector("[data-detail]");
      if (first) {
        this.selected = first;
        this.querySelector(`[data-key="${CSS.escape(first)}"]`)?.classList.add("sel");
        const el = document.createElement("icm-item");
        el.key = first; el.embedded = true;
        detail.appendChild(el);
      } else {
        detail.innerHTML = `<div class="empty">Nothing waits for you.</div>`;
      }
    } else {
      body.innerHTML = this.list();
    }
    body.querySelectorAll("[data-key]").forEach((n) => n.addEventListener("click", () => this.open(n.dataset.key)));
    body.querySelector("[data-resume]")?.addEventListener("click", async () => {
      try { await post("/server/pause", { paused: false }); app.note = "Resumed."; app.noteUntil = Date.now() + 10000; window.dispatchEvent(new Event("icm:changed")); } catch (e) { fail(e); }
    });
    body.querySelector("[data-hint-close]")?.addEventListener("click", () => { app.hintClosed = true; window.InstallHint?.dismiss?.(); this.render(); });
    body.querySelector("[data-install]")?.addEventListener("click", () => window.InstallHint.install().then(() => this.render()));
  }

  open(key) {
    if (wide()) { this.selected = key; this.render(); return; }
    this.closest("ion-nav").push("icm-item", { key });
  }
}
customElements.define("icm-today", Today);

// -- one request -------------------------------------------------------------------------------------------------------
class Item extends HTMLElement {
  connectedCallback() {
    this.innerHTML = (this.embedded ? "" : `<ion-header translucent><ion-toolbar><ion-buttons slot="start">
      <ion-back-button text="Today"></ion-back-button></ion-buttons></ion-toolbar></ion-header>`)
      + `<ion-content${this.embedded ? "" : " fullscreen"}><div data-body></div></ion-content><ion-footer data-foot></ion-footer>`;
    if (this.embedded) {             // inside the split on a wide screen: no page of its own, the buttons sit under the message
      this.innerHTML = `<div data-body></div><div data-foot></div>`;
    }
    this.onChange = () => this.load(true);
    window.addEventListener("icm:changed", this.onChange);
    this.load();
  }

  disconnectedCallback() { window.removeEventListener("icm:changed", this.onChange); }

  async load(quiet) {
    try {
      this.it = await get("/items/" + encodeURIComponent(this.key));
    } catch (e) {
      if (e.status === 404) { this.gone(); return; }
      if (!quiet) fail(e);
      return;
    }
    this.render();
  }

  gone() {
    this.querySelector("[data-body]").innerHTML = titleBlock("No longer waiting",
      "It was sent, discarded or it expired, maybe in another window.");
    this.querySelector("[data-foot]").innerHTML = "";
  }

  subline() {
    const it = this.it;
    const who = it.by ? ` from ${esc(it.by)}` : "";
    const base = it.kind === "mail" ? `${esc(it.badge)}${it.to ? ` to ${esc(it.to)}` : ""}${who}, ${esc(ago(it.created_at))}.`
      : it.kind === "calendar" ? `${esc(it.badge)}${who}, ${esc(ago(it.created_at))}.` : `iMessage${who}, ${esc(ago(it.created_at))}.`;
    if (it.missing.length) return `${base} ${it.missing.length === 1 ? "One file is" : `${it.missing.length} files are`} missing before it can go out.`;
    if (it.warnings.length || it.needs_override) return `${base} Check it before it goes out.`;
    return base;
  }

  render() {
    const it = this.it;
    const id = this.key.split(":").slice(1).join(":");
    let h = titleBlock(it.title, this.subline());
    if (it.warnings.length) {
      h += `<ion-list-header class="sec"><ion-label>Why this was held</ion-label></ion-list-header>
        <div class="warn"><strong>${icon("warning-outline")} Check before sending</strong><ul>${it.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>`;
      if (it.original && !it.original.unavailable) h += section("The mail it answers")
        + `<ion-list class="inset">${`<ion-item><ion-label class="ion-text-wrap"><h2>${esc(it.original.subject || "(no subject)")}</h2><p>${esc(it.original.from)}</p></ion-label></ion-item>`}</ion-list>`;
    }
    if (it.missing.length) {
      h += section("Missing") + it.missing.map((m) => `<div class="slot" data-slot="${m.index}"><div class="main"><div class="t">${esc(m.filename)}</div>
        <div class="s">Up to ${esc(size(it.max_bytes))}</div></div><ion-button data-pick="${m.index}">Choose file</ion-button>
        <input type="file" hidden data-input="${m.index}"></div>`).join("");
    }
    const addrs = (s) => (String(s).match(/[^\s<>,]+@[^\s<>,]+/g) || []).map((a) => a.toLowerCase()).sort().join(",");
    const visible = addrs(it.rows.filter(([k]) => k === "To" || k === "Cc").map(([, v]) => v).join(","));
    const rows = it.rows.filter(([k, v]) => !(k === "Will be delivered to" && addrs(v) === visible));      // the same people twice
    // the quoted mail it answers is folded away, so the new text and the buttons are what you see first
    const m = /^(On .+ wrote:|Am .+ schrieb .+:|-{2,} ?Original Message ?-{2,}|>)/m.exec(it.text || "");
    const own = m ? it.text.slice(0, m.index).trimEnd() : (it.text || "");
    const quoted = m ? it.text.slice(m.index) : "";
    const long = own.length > 900;
    if (it.kind === "calendar") {
      h += section("Event") + `<ion-list class="inset">${rows.map(([k, v]) => `<ion-item><ion-label>${esc(k)}</ion-label><ion-note slot="end" class="ion-text-wrap" style="text-align:right;max-width:65%">${esc(v)}</ion-note></ion-item>`).join("")}</ion-list>`
        + (it.text ? `<div class="mail"><div class="body">${esc(it.text)}</div></div>` : "");
    } else {
      h += section(it.kind === "imessage" ? "Message" : "Message") + `<div class="mail">${rows.length ? `<dl>${rows.map(([k, v]) => `<dt>${esc(k === "Will be delivered to" ? "Sent to" : k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>` : ""}
        <div class="body${long ? " short" : ""}" data-text>${esc(own)}</div>${long ? `<ion-button fill="clear" size="small" data-more>Show the whole message</ion-button>` : ""}
        ${quoted ? `<div class="body quoted" data-quoted hidden>${esc(quoted)}</div><ion-button fill="clear" size="small" data-quote>Show the quoted mail</ion-button>` : ""}</div>`;
    }
    if (it.files.length) {
      h += section("Attached", `<span class="num">${it.files.length}</span>`) + `<ion-list class="inset">${it.files.map((f) => `
        <ion-item ${f.preview ? `button detail="true" data-preview="${f.index}"` : `detail="false"`}>
          ${icon(f.content_type.startsWith("image/") ? "image-outline" : "document-outline")}
          <ion-label class="ion-text-wrap" style="margin-left:12px"><h2>${esc(f.filename)}${f.added && f.asked && f.asked !== f.filename ? `<span class="pill">Other name</span>` : ""}</h2>
          <p class="num">${esc(size(f.size))}${f.added ? ` · added by you${f.asked && f.asked !== f.filename ? ` for ${esc(f.asked)}` : ""}` : it.by ? ` · from ${esc(it.by)}` : ""}</p>
          ${f.added ? `<div style="display:flex;gap:4px;margin-top:4px"><ion-button size="small" fill="clear" data-replace="${f.index}">Replace</ion-button>
            <ion-button size="small" fill="clear" color="danger" data-remove="${f.index}">Remove</ion-button>
            <input type="file" hidden data-input="${f.index}"></div>` : ""}</ion-label></ion-item>`).join("")}</ion-list>`;
    }
    const notes = it.notes.filter((n) => !n.startsWith("Attachments:") && !n.startsWith("Waiting for:") && !n.startsWith("In reply to:"));
    h += `<p class="note">${notes.map(esc).join(" ")} ${notes.length ? "" : ""}Dropped ${esc(until(it.expires_at))} if nothing happens.</p>`;
    this.querySelector("[data-body]").innerHTML = h + (this.embedded ? "" : `<div style="height:16px"></div>`);
    const careful = it.warnings.length || it.needs_override;
    const foot = careful
      ? `<div class="acts stack"><ion-button expand="block" data-discard>Discard</ion-button>
         <ion-button expand="block" fill="clear" color="danger" data-anyway>Send anyway…</ion-button></div>`
      : `<div class="acts"><ion-button fill="outline" color="medium" data-discard>Discard</ion-button>
         <ion-button data-approve ${it.missing.length ? "disabled" : ""}>${esc(it.approve_label)}</ion-button></div>`;
    this.querySelector("[data-foot]").innerHTML = this.embedded ? foot : `<ion-toolbar style="--background:var(--ground)">${foot}</ion-toolbar>`;
    this.wire(id);
  }

  wire(id) {
    const $ = (s) => this.querySelector(s);
    $("[data-more]")?.addEventListener("click", (e) => { $("[data-text]").classList.remove("short"); e.target.remove(); });
    $("[data-quote]")?.addEventListener("click", (e) => { $("[data-quoted]").hidden = false; e.target.remove(); });
    $("[data-approve]")?.addEventListener("click", () => this.approve(false));
    $("[data-anyway]")?.addEventListener("click", async () => {
      if (await ask("Send it anyway?", `It goes to ${this.it.to || "the recipients"} although the mail it answers looked like an attempt to steer the agent.`, "Send", { danger: true })) this.approve(true);
    });
    $("[data-discard]")?.addEventListener("click", () => this.discard());
    this.querySelectorAll("[data-pick],[data-replace]").forEach((b) => b.addEventListener("click", () => {
      this.querySelector(`[data-input="${b.dataset.pick || b.dataset.replace}"]`).click();
    }));
    this.querySelectorAll("[data-input]").forEach((inp) => inp.addEventListener("change", async () => {
      if (!inp.files.length) return;
      const f = inp.files[0];
      if (f.size > this.it.max_bytes) { fail(new Error(`${f.name} is larger than ${size(this.it.max_bytes)}.`)); return; }
      try { await upload(id, inp.dataset.input, f); toast(`Added ${f.name}.`); this.load(); } catch (e) { fail(e); }
    }));
    this.querySelectorAll("[data-remove]").forEach((b) => b.addEventListener("click", async () => {
      try { await removeFile(id, b.dataset.remove); toast("Removed."); this.load(); } catch (e) { fail(e); }
    }));
    this.querySelectorAll("[data-slot]").forEach((slot) => {
      slot.addEventListener("dragover", (e) => { e.preventDefault(); slot.classList.add("drag"); });
      slot.addEventListener("dragleave", () => slot.classList.remove("drag"));
      slot.addEventListener("drop", async (e) => {
        e.preventDefault(); slot.classList.remove("drag");
        const f = e.dataTransfer.files[0];
        if (!f) return;
        try { await upload(id, slot.dataset.slot, f); toast(`Added ${f.name}.`); this.load(); } catch (err) { fail(err); }
      });
    });
    this.querySelectorAll("[data-preview]").forEach((n) => n.addEventListener("click", () => this.preview(this.it.files.find((f) => String(f.index) === n.dataset.preview))));
  }

  preview(f) {
    const url = `/dashboard/api/items/${encodeURIComponent(this.key)}/files/${f.index}`;
    let body;
    if (f.content_type.startsWith("image/")) body = `<div class="preview"><img src="${esc(url)}" alt="${esc(f.filename)}"></div>`;
    else if (f.content_type === "application/pdf") body = `<div class="preview"><iframe src="${esc(url)}" title="${esc(f.filename)}"></iframe></div>`;
    else body = `<div class="preview"><pre data-text>Loading…</pre></div>`;
    sheet(f.filename, body + `<p class="note" style="padding-bottom:16px">Shown only here. The file stays on the server.</p>`, (m) => {
      const pre = m.querySelector("[data-text]");
      if (pre) fetch(url, { credentials: "same-origin" }).then((r) => r.text()).then((t) => { pre.textContent = t; }, () => { pre.textContent = "Could not load it."; });
    });
  }

  leave() {
    if (this.embedded) { window.dispatchEvent(new Event("icm:changed")); return; }
    const nav = this.closest("ion-nav");
    nav.pop().then(() => window.dispatchEvent(new Event("icm:changed")));
  }

  async approve(override) {
    const btns = this.querySelectorAll("ion-button");
    btns.forEach((b) => { b.disabled = true; });
    try {
      const r = await post(`/items/${encodeURIComponent(this.key)}/approve`, { sha: this.it.sha, override });
      app.note = r.note; app.noteBad = false; app.noteUntil = Date.now() + 10000;
      this.leave();
    } catch (e) {
      btns.forEach((b) => { b.disabled = false; });
      fail(e);
      if (e.status === 409) this.load();
    }
  }

  // Discard waits 5 seconds with Undo before it is sent; hiding or leaving the page sends it at once
  discard() {
    const key = this.key, sha = this.it.sha, title = this.it.title;
    app.hidden.add(key);
    let undone = false, sent = false;
    const send = (now) => {
      if (undone || sent) return;
      sent = true;
      document.removeEventListener("visibilitychange", onHide);
      const path = `/items/${encodeURIComponent(key)}/discard`;
      const p = now ? postNow(path, { sha }) : post(path, { sha });
      Promise.resolve(p).then(() => { app.hidden.delete(key); window.dispatchEvent(new Event("icm:changed")); },
        (e) => { app.hidden.delete(key); fail(e); window.dispatchEvent(new Event("icm:changed")); });
    };
    const onHide = () => { if (document.visibilityState === "hidden") send(true); };
    document.addEventListener("visibilitychange", onHide);
    toast(`Discarded “${title}”`, { duration: 5000, cssClass: "undo", buttons: [{ text: "Undo", role: "cancel",
      handler: () => { undone = true; app.hidden.delete(key); document.removeEventListener("visibilitychange", onHide); window.dispatchEvent(new Event("icm:changed")); } }] })
      .then((t) => t.onDidDismiss().then(() => send(false)));
    this.leave();
  }
}
customElements.define("icm-item", Item);
export { Today, Item };
