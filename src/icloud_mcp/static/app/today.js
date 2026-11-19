// Home: what waits for the owner's approval, and one request exactly as it will go out.
import { get, post, postNow, upload, removeFile, ApiError } from "./api.js";
import { esc, icon, ago, clock, left, size, tag, toast, fail, ask, sheet, section, titleBlock, wide } from "./ui.js";
import { app } from "./state.js";
import { editView, editFoot, wireEdit, bringBack } from "./edit.js";

const DAY = 86400;      // Home mentions a request the assistant took back for a day; Activity keeps it

function greeting() {
  const h = new Date().getHours();
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

function finding(waiting) {
  if (!waiting.length) return "Nothing waits for you. When your assistant wants to send something, it shows up here, and a note reaches your phone.";
  const n = waiting.length;
  const head = n === 1 ? "One request waits for you." : n === 2 ? "Two requests wait for you." : `${n} requests wait for you.`;
  return `${head} ${n === 1 ? "It is" : "The oldest is"} dropped in ${left(waiting[0].expires_at)}.`;
}

// A request that reaches a whole group: an iMessage group chat, or any action type that tags it so (package 4b)
const isGroup = (w) => !!(w.group || (w.tags || []).includes("Group"));

// What a request of a type the server registers itself is about and whom it reaches: "Autumn fair · to jana@example.org" (5b)
const about = (w) => [w.subject ? esc(w.subject) : "", w.to ? `to ${esc(w.to)}` : ""].filter(Boolean).join(" · ");

// The line under a request in the list: its kind, its tags, who it goes to
function itemSub(w) {
  const text = w.kind === "external" && w.to_shown ? about(w) : "";
  const extra = w.blocked ? "a file is missing" : w.held ? "cannot be approved yet" : w.warning && !isGroup(w) ? "check this first" : "";
  const by = w.by ? `from ${esc(w.by)}` : "";
  const tags = [...(w.group ? ["Group"] : []), ...(w.tags || [])].filter((t, i, all) => all.indexOf(t) === i);
  return tag(w.kind === "external" ? w.kind_label || "Other" : w.kind) + tags.map((t) => tag(t, "warn")).join("")
    + [text, by, extra].filter(Boolean).join(" · ");
}

class Today extends HTMLElement {
  connectedCallback() {
    this.selected = null;
    this.innerHTML = `<ion-content><div data-body></div></ion-content>`;
    this.onChange = () => this.load();
    window.addEventListener("icm:changed", this.onChange);
    window.addEventListener("icm:open", (this.onOpen = (e) => this.open(e.detail)));
    this.mq = window.matchMedia("(min-width: 900px)");
    this.mq.addEventListener("change", (this.onWidth = () => { this.split = null; if (this.data) this.render(); }));   // a window or an iPad turned
    this.load();
  }

  disconnectedCallback() {
    window.removeEventListener("icm:changed", this.onChange);
    window.removeEventListener("icm:open", this.onOpen);
    this.mq.removeEventListener("change", this.onWidth);
    clearTimeout(this.letGoTimer);
  }

  // Requests the assistant took back in the last 24 hours. Each time is the server's; the list is worked out here at every draw,
  // and a draw is timed for the moment the oldest one passes the day, so a row goes away by itself even on an open tab.
  withdrawn() {
    const now = Date.now() / 1000;
    return (this.data.withdrawn || []).filter((w) => now - w.t < DAY);
  }

  letGo() {
    clearTimeout(this.letGoTimer);
    const live = this.withdrawn();
    if (live.length) this.letGoTimer = setTimeout(() => this.render(), Math.max(1000, (Math.min(...live.map((w) => w.t)) + DAY - Date.now() / 1000) * 1000 + 500));
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
    if (hint !== "none" && !app.hintClosed) out.push(`<div class="notice"><span>Add this app to your
      Home Screen to get notifications${hint.startsWith("ios") ? ": in Safari tap Share, then Add to Home Screen" : ""}.</span>
      ${hint === "button" ? `<ion-button size="small" data-install>Install</ion-button>` : ""}<ion-button size="small" fill="clear" color="medium" data-hint-close>Close</ion-button></div>`);
    return out.join("");
  }

  list() {
    const d = this.data;
    const items = d.waiting.map((w) => `<ion-item button detail="true" data-key="${esc(w.key)}" class="${w.key === this.selected ? "sel" : ""}">
      <ion-label class="ion-text-wrap"><h2>${esc(w.title)}</h2><p>${itemSub(w)}</p></ion-label>
      <ion-note slot="end" class="num">${esc(left(w.expires_at))}</ion-note></ion-item>`).join("");
    const sent = d.sent_today.map((e) => `<ion-item class="sent"><ion-label class="ion-text-wrap"><h2>${esc(e.title)}</h2>
      <p>${esc(e.what || "")}${e.to ? ` to ${esc(e.to)}` : ""}${e.unclear ? ` ${tag("Unclear", "warn end")}` : ""}</p></ion-label>
      <ion-note slot="end" class="num">${esc(e.time)}</ion-note></ion-item>`).join("");
    // taken back: what it was and for whom, when, and the reason the assistant chose; tapping one opens it in Activity
    const taken = this.withdrawn().map((w) => `<ion-item button detail="true" data-taken="${esc(w.t)}"><ion-label class="ion-text-wrap">
      <h2>${esc(w.title)}</h2><p>${esc(w.what)}${w.to ? ` to ${esc(w.to)}` : ""}</p><p>${w.why ? `${esc(w.why)} ` : ""}Nothing was sent.</p></ion-label>
      <ion-note slot="end" class="num">${esc(w.time)}</ion-note></ion-item>`).join("");
    return titleBlock(greeting(), esc(finding(d.waiting)))
      + `<div class="fresh ${app.online ? "" : "off"}"><i></i>${app.online ? `Live, checked ${esc(ago(app.updated))}` : "Offline, showing the last state"}</div>`
      + this.notices()
      + (d.waiting.length ? section("Waiting", `<span class="num">${d.waiting.length}</span>`) + `<ion-list class="inset">${items}</ion-list>` : "")
      + (taken ? section("Taken back by your assistant") + `<ion-list class="inset">${taken}</ion-list>` : "")
      + (sent ? section("Sent today") + `<ion-list class="inset">${sent}</ion-list>` : "");
  }

  render() {
    const body = this.querySelector("[data-body]");
    // with nothing waiting (and no request still showing how its send went) a wide screen shows one column, not an empty half
    if (wide() && (this.data.waiting.length || app.holding)) {
      // the two columns stay in place: a refresh redraws the list and leaves the request open, so nothing flickers or loses what you typed
      if (!this.split) {
        body.innerHTML = `<div class="split"><div class="list" data-list></div><div class="detail" data-detail></div></div>`;
        this.split = true; this.detailKey = undefined;
      }
      // a request that is going out, or showing how its send went, stays open until the owner is done (it is not in the queue meanwhile)
      const holding = app.holding && this.detailKey === app.holding;
      const first = holding ? app.holding : this.selected && this.data.waiting.some((w) => w.key === this.selected) ? this.selected : (this.data.waiting[0] || {}).key;
      this.selected = first || null;
      this.querySelector("[data-list]").innerHTML = this.list();
      const detail = this.querySelector("[data-detail]");
      if (holding) {
        // leave it as it is
      } else if ((first || null) !== this.detailKey) {
        detail.innerHTML = "";
        this.detailKey = first || null;
        if (first) {
          const el = document.createElement("icm-item");
          el.key = first; el.embedded = true;
          detail.appendChild(el);
        } else {
          detail.innerHTML = `<div class="empty"><b>Nothing waits for you.</b>Requests from your assistant show up here, and a note reaches your phone.</div>`;
        }
      }
    } else {
      this.split = null;
      body.innerHTML = wide() ? `<div class="single">${this.list()}</div>` : this.list();
    }
    this.letGo();
    body.querySelectorAll("[data-taken]").forEach((n) => n.addEventListener("click", () => {
      app.activityFocus = Number(n.dataset.taken);
      window.dispatchEvent(new Event("icm:activity-focus"));          // app.js switches to Activity, which finds the entry
    }));
    body.querySelectorAll("[data-key]").forEach((n) => n.addEventListener("click", () => this.open(n.dataset.key)));
    body.querySelector("[data-resume]")?.addEventListener("click", async () => {
      try { await post("/server/pause", { paused: false }); app.note = "Resumed."; app.noteUntil = Date.now() + 10000; window.dispatchEvent(new Event("icm:changed")); } catch (e) { fail(e); }
    });
    body.querySelector("[data-hint-close]")?.addEventListener("click", () => { app.hintClosed = true; window.InstallHint?.dismiss?.(); this.render(); });
    body.querySelector("[data-install]")?.addEventListener("click", () => window.InstallHint.install().then(() => this.render()));
  }

  open(key) {
    if (wide()) { app.holding = null; this.selected = key; this.render(); return; }
    this.closest("ion-nav").push("icm-item", { key });
  }
}
customElements.define("icm-today", Today);

// -- one request -------------------------------------------------------------------------------------------------------
class Item extends HTMLElement {
  connectedCallback() {
    this.mode = "view";            // "view" or "edit"
    this.result = null;            // how the last approve went: {state: "sending" | "failed" | "unclear", note}
    this.innerHTML = (this.embedded ? "" : `<ion-header translucent><ion-toolbar><ion-buttons slot="start">
      <ion-back-button text="Home"></ion-back-button></ion-buttons></ion-toolbar></ion-header>`)
      + `<ion-content${this.embedded ? "" : " fullscreen"}><div data-body></div></ion-content><ion-footer data-foot></ion-footer>`;
    if (this.embedded) {             // inside the split on a wide screen: no page of its own, the buttons sit under the message
      this.innerHTML = `<div data-body></div><div data-foot></div>`;
    }
    this.onChange = () => { if (!this.result || this.result.state === "failed") this.load(true); };    // not while it is sending, nor once it is sent
    window.addEventListener("icm:changed", this.onChange);
    this.load();
  }

  disconnectedCallback() {
    window.removeEventListener("icm:changed", this.onChange);
    if (app.holding === this.key) app.holding = null;
  }

  async load(quiet) {
    if (this.mode === "edit" && quiet) return;         // a refresh does not take the text out from under the owner's hands
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

  // "Mail · asked by your assistant 4 min ago", an edit, a file that is missing
  subline() {
    const it = this.it;
    const who = it.by ? `by ${esc(it.by)}` : "by your assistant";
    // a type whose list line names what it is about and who it reaches says it here too ("Autumn fair · to jana@example.org ·
    // asked by …"), in place of the badge
    let p = `${it.kind === "external" && it.to_shown && (it.to || it.subject) ? about(it) : esc(it.badge)} · asked ${who} ${esc(ago(it.created_at))}`;
    if (it.edit && it.edit.edited) p += ` · ${tag("Edited by you", "end")}`;
    if (it.missing.length) return `${p}. ${it.missing.length === 1 ? "One file is" : `${it.missing.length} files are`} missing before it can go out.`;
    return p;
  }

  // The rows of the message card, as <dt><dd> pairs, each with the tag its type gives it (a mail's account on From, package 6b)
  rowsHtml() {
    const it = this.it;
    return it.rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}${it.row_tags && it.row_tags[k] ? ` ${tag(it.row_tags[k])}` : ""}</dd>`).join("");
  }

  // The message exactly as it goes out, with the edit button under it
  card() {
    const it = this.it;
    // the quoted mail it answers is folded away, so the new text and the buttons are what you see first
    const m = it.text_style === "mail" ? /^(On .+ wrote:|Am .+ schrieb .+:|-{2,} ?Original Message ?-{2,}|>)/m.exec(it.text || "") : null;
    const own = m ? it.text.slice(0, m.index).trimEnd() : (it.text || "");
    const quoted = m ? it.text.slice(m.index) : "";
    const long = own.length > 900;
    // an action of a registered type (external): who or what it reaches first, then the text, then its other rows (DESIGN.md)
    // a chat message (a type that shows its text as a bubble, such as an iMessage) keeps the message layout: rows, then the bubble
    const chat = it.text_style === "bubble";
    const ext = it.kind === "external" && !chat;
    // a value the service changed since the owner looked is marked (package 2c)
    const fresh = new Set([...(it.changed || []).map((c) => c.label), ...(it.edited_rows || [])]);
    // a type may tag a row's value (package 6b: the account a mail goes out from, "iCloud")
    const rowTag = (k) => (it.row_tags && it.row_tags[k] ? ` ${tag(it.row_tags[k])}` : "");
    const dd = ([k, v]) => `<dt>${esc(k)}</dt><dd>${fresh.has(k) ? `<mark class="changed">${esc(v)}</mark>` : esc(v)}${rowTag(k)}</dd>`;
    // a type that names who it reaches under the title (to_shown) has no recipient row: all its rows come before the text
    const rows = ext ? (it.to_shown ? it.rows : it.rows.slice(0, 1)).map(dd).join("") : this.rowsHtml();
    const more = ext && !it.to_shown ? it.rows.slice(1).map(dd).join("") : "";
    // an empty text has no block, for every kind (package 5b); where the type names a caption and the text may be edited, a line
    // says there is none and keeps the edit button; a type whose empty text cannot be edited shows nothing about it
    const text = !own ? "" : chat ? `<div class="bubble" data-text>${esc(own)}</div>` : `<div class="body${long ? " short" : ""}" data-text>${esc(own)}</div>`;
    const caption = it.text_caption ? esc(it.text_caption) : chat ? "Exactly as it will be sent. Formatting stays as typed." : "Exactly as it will be sent.";
    const edit = it.edit || { allowed: false, reason: "" };
    const form = !!(edit.fields && edit.fields.length);          // package 2d: the fields the owner may change, edited as one form
    return `<div class="msg">${rows ? `<dl>${rows}</dl>` : ""}${text}
      ${long ? `<ion-button fill="clear" size="small" data-more>Show the whole message</ion-button>` : ""}
      ${quoted ? `<div class="body quoted" data-quoted hidden>${esc(quoted)}</div><ion-button fill="clear" size="small" data-quote>Show the quoted mail</ion-button>` : ""}
      ${ext && !own && !form && (!it.text_caption || !edit.allowed) ? "" : `<div class="capr"><span>${!own && it.text_caption ? "No description." : caption}</span><ion-button fill="clear" size="small" data-edit ${edit.allowed ? "" : "disabled"}>${form ? "Edit" : "Edit text"}</ion-button></div>`}
      ${edit.allowed || (ext && !own) ? "" : `<div class="why" data-why>Edit text is not available: ${esc(edit.reason)}</div>`}
      ${more ? `<div class="rows2"><dl>${more}</dl></div>` : ""}</div>`;
  }

  // The history of a request (package 2c), newest first, in sentences. Tapping an older one shows it; only the assistant's first
  // text offers to come back. Only what the card shows can be sent.
  showHistory() {
    const it = this.it, vs = it.versions;
    const first = vs[0];
    const said = (v) => {
      const c = v.changes, row = c.find((x) => x.label !== "Text"), text = c.find((x) => x.label === "Text");
      if (v.n === 1) return ["Your assistant asked", v.text || (v.rows[0] || [])[1] || ""];
      if (v.by === "service") return [`${it.badge} changed ${row ? `the ${row.label.toLowerCase()}` : "the target"}`, row ? `${row.old} → ${row.new}. Nothing was sent.` : "Nothing was sent."];
      if (v.by === "assistant") return ["Your assistant revised it", text ? `“${text.new}”` : c.map((x) => `${x.label}: ${x.new}`).join(" · ")];
      if (text && text.new === first.text) return ["You went back to your assistant's text", `“${text.new}”`];
      const rows = c.filter((x) => x.label !== "Text");      // package 2d: the fields the owner changed, by name
      const was = (x) => (x.new === "" ? `${x.old}, removed` : x.old === "" ? x.new : `${x.old} → ${x.new}`);
      if (rows.length) return [`You changed ${[...(text ? ["the text"] : []), ...rows.map((x) => x.label)].join(", ")}`, rows.map((x) => `${x.label}: ${was(x)}`).join(" · ")];
      return ["You changed the text", text ? `“${text.new}”` : ""];
    };
    const items = vs.slice().reverse().map((v) => {
      const [h, p] = said(v), open = v.n !== vs.length && (v.text || v.rows.length);
      return `<ion-item ${open ? `button detail="true" data-v="${v.n}"` : ""}><ion-label class="ion-text-wrap"><h2>${esc(h)}</h2><p>${esc(p)}</p>
        <div data-show="${v.n}" hidden></div></ion-label><ion-note slot="end" class="num">${esc(clock(v.at))}</ion-note></ion-item>`;
    }).join("");
    sheet("History", `<ion-list class="inset" style="margin-top:12px!important">${items}</ion-list>
      <p class="note">Only what you see on the card can be sent.</p>`, (m) => {
      m.querySelectorAll("[data-v]").forEach((el) => el.addEventListener("click", () => {
        const v = vs.find((x) => x.n === Number(el.dataset.v)), box = m.querySelector(`[data-show="${v.n}"]`);
        if (!box.hidden) { box.hidden = true; return; }
        const rows = v.rows.map(([k, val]) => `<dt>${esc(k)}</dt><dd>${esc(val)}</dd>`).join("");
        const back = it.edit && it.edit.edited && v.text === it.edit.original;   // the text "Bring it back" restores, wherever it is
        box.innerHTML = `<div class="msg" style="margin:10px 0 0">${rows ? `<dl>${rows}</dl>` : ""}${v.text ? `<div class="body">${esc(v.text)}</div>` : ""}</div>
          ${back ? `<ion-button fill="clear" size="small" data-back>Use this text again</ion-button>` : ""}`;
        box.hidden = false;
        box.querySelector("[data-back]")?.addEventListener("click", (e) => {
          e.stopPropagation(); m.dismiss(); bringBack(this.key, this.it, (reload) => { if (reload) this.load(); });
        });
      }));
    });
  }

  // How the last approve went, above the message
  resultHtml() {
    const r = this.result, it = this.it;
    if (!r) return "";
    // a mail names whom it goes to (package 6b keeps it); another action names its kind
    const to = it.kind === "external" && it.text_style !== "mail" ? esc(it.badge) : it.to ? esc(it.to) : "the recipients";
    // a type's own note says where to look (a calendar change: the calendar), so the line does not name the type as a place
    if (r.state === "unclear" && it.kind === "external") return `<div class="notice warn"><div>${r.note ? "<b>Not sure it went through.</b> Nothing will be sent again." : `<b>Not sure it reached ${esc(it.badge)}.</b> Nothing will be sent again; look there first.`}
      <div class="btns"><ion-button size="small" data-done>Done</ion-button></div>${r.note ? `<p class="sub" style="margin:10px 0 0">${esc(r.note)}</p>` : ""}</div></div>`;
    if (r.state === "failed" && it.kind === "external") return `<div class="notice bad"><div><b>Did not go out:</b> ${esc(r.note)}
      <div class="btns"><ion-button size="small" data-retry>Try again</ion-button><ion-button size="small" class="neutral" data-discard>Discard</ion-button></div></div></div>`;
    if (r.state === "sending") return `<div class="notice"><ion-spinner name="crescent"></ion-spinner><div><b>Sending to ${to}…</b><br>Nothing is sent twice.</div></div>`;
    // the source's own note says where to look (a calendar change: the calendar); without one, the chat advice
    if (r.state === "unclear") return `<div class="notice warn"><div><b>Not sure it arrived.</b> Nothing is sent again. ${r.note ? "" : "Look at the conversation first, then decide."}
      <div class="btns"><ion-button size="small" data-done>Done</ion-button></div>${r.note ? `<p class="sub" style="margin:10px 0 0">${esc(r.note)}</p>` : ""}</div></div>`;
    if (r.state === "failed") return `<div class="notice bad"><div><b>Not sent, still waiting.</b> ${esc(r.note)}
      <div class="btns"><ion-button size="small" data-retry>Try again</ion-button><ion-button size="small" class="neutral" data-discard>Discard</ion-button></div></div></div>`;
    return "";
  }

  render() {
    const it = this.it;
    const id = this.key.split(":").slice(1).join(":");
    // the buttons sit in a plain bar, not in an ion-toolbar: Ionic restyles buttons inside a toolbar
    const foot = (html) => { this.querySelector("[data-foot]").innerHTML = this.embedded ? html : `<div class="footbar">${html}</div>`; };
    if (this.result && this.result.state === "unclear") {            // sent, but not certain: the result is the page until the owner is done
      this.querySelector("[data-body]").innerHTML = titleBlock(it.title, `${esc(it.badge)} · approved just now`) + this.resultHtml();
      foot("");
      this.wire(id);
      return;
    }
    if (this.mode === "edit") {
      this.querySelector("[data-body]").innerHTML = editView(it, this.rowsHtml(), this.subline());
      foot(editFoot());
      wireEdit(this, this.key, it, (reload) => { this.mode = "view"; if (reload) this.load(); else this.render(); });
      return;
    }
    let h = titleBlock(it.title, this.subline()) + `<div data-result>${this.resultHtml()}</div>`;
    const ext = it.kind === "external" && it.text_style !== "bubble";      // a chat message keeps its card under the warnings
    if (it.held) h += `<div class="warnbox held" id="held-why"><strong>${icon("warning-outline")} Agents can send this without you</strong>
      ${esc(it.held)} Approving is off until then. Settings › Server shows it.</div>`;
    if (it.changed && it.changed.length) h += `<div class="warnbox"><strong>${icon("warning-outline")} Changed since you looked</strong>
      ${it.changed.map((c) => `${esc(c.label)} is now <b>${esc(c.new)}</b> (was ${esc(c.old)}).`).join(" ")} Nothing was sent. Check it again before approving.</div>`;
    if (ext && it.approvable !== false) h += this.card();
    if (it.versions) h += `<ion-list class="inset" style="margin-top:12px!important"><ion-item button detail="true" data-history>
      <ion-label>Earlier versions</ion-label><ion-note slot="end" class="num">${it.versions.length - 1}</ion-note></ion-item></ion-list>`;
    if (it.warnings.length) {
      const grp = isGroup(it);
      h += `<div class="warnbox"><strong>${icon("warning-outline")} ${grp ? "Group chat: everyone in it receives this" : "Check before sending"}</strong>
        ${grp ? "Everyone in the conversation sees your message and your name." : ""}
        ${grp && it.warnings.length === 1 ? "" : `<ul>${it.warnings.filter((w) => !(grp && w.startsWith("A group conversation"))).map((w) => `<li>${esc(w)}</li>`).join("")}</ul>`}</div>`;
      if (it.original && !it.original.unavailable) h += section("The mail it answers")
        + `<ion-list class="inset">${`<ion-item><ion-label class="ion-text-wrap"><h2>${esc(it.original.subject || "(no subject)")}</h2><p>${esc(it.original.from)}</p></ion-label></ion-item>`}</ion-list>`;
    }
    if (it.missing.length) {
      h += section("Missing") + it.missing.map((m) => `<div class="slot" data-slot="${m.index}"><div class="main"><div class="t">${esc(m.filename)}</div>
        <div class="s">Up to ${esc(size(it.max_bytes))}</div></div><ion-button data-pick="${m.index}">Choose file</ion-button>
        <input type="file" hidden data-input="${m.index}"></div>`).join("");
    }
    if (!ext) h += this.card();
    if (it.edit && it.edit.edited && !it.versions) {
      h += section("What your assistant asked for", `<ion-button fill="clear" size="small" data-restore>Bring it back</ion-button>`)
        + `<div class="msg"><div class="asked">${esc(it.edit.original)}</div></div>`;
    }
    if (it.files.length) {
      h += section("Attached", `<span class="num">${it.files.length}</span>`) + `<ion-list class="inset">${it.files.map((f) => `
        <ion-item ${f.preview ? `button detail="true" data-preview="${f.index}"` : `detail="false"`}>
          ${icon(f.content_type.startsWith("image/") ? "image-outline" : "document-outline")}
          <ion-label class="ion-text-wrap" style="margin-left:12px"><h2>${esc(f.filename)}${f.added && f.asked && f.asked !== f.filename ? tag("Other name", "warn") : ""}</h2>
          <p class="num">${esc(size(f.size))}${f.added ? ` · added by you${f.asked && f.asked !== f.filename ? ` for ${esc(f.asked)}` : ""}` : it.by ? ` · from ${esc(it.by)}` : ""}</p>
          ${f.added ? `<div style="display:flex;gap:4px;margin-top:4px"><ion-button size="small" fill="clear" data-replace="${f.index}">Replace</ion-button>
            <ion-button size="small" fill="clear" color="danger" data-remove="${f.index}">Remove</ion-button>
            <input type="file" hidden data-input="${f.index}"></div>` : ""}</ion-label></ion-item>`).join("")}</ion-list>`;
    }
    // what the request answers or forwards, as its type read it (package 6b): its subject and its sender, never more
    if (it.kind === "external" && it.original && !it.original.unavailable && (it.original.subject || it.original.from)) {
      h += `<p class="note"><b>${esc(it.original.label || "In reply to")}</b> “${esc(it.original.subject || "(no subject)")}”${it.original.from ? ` from ${esc(it.original.from)}` : ""}</p>`;
    }
    const notes = it.notes.filter((n) => !n.startsWith("Attachments:") && !n.startsWith("Waiting for:") && !n.startsWith("In reply to:"));
    h += `<p class="note">${notes.map(esc).join(" ")} This request expires in about ${esc(left(it.expires_at))}.${it.edit && it.edit.edited
      ? " Approving sends your version. Your assistant is told the request was queued, not what you changed." : ""}</p>`;
    this.querySelector("[data-body]").innerHTML = h + (this.embedded ? "" : `<div style="height:16px"></div>`);
    const careful = it.warnings.length || it.needs_override;
    const sending = this.result && this.result.state === "sending";
    // with a warning, Discard is the main button and the send asks first
    if (it.held) {                                                   // approving is off until the setup is fixed: Discard is the main button
      foot(`<div class="acts"><ion-button data-discard>Discard</ion-button><ion-button class="neutral" disabled aria-describedby="held-why">${esc(it.approve_label)}</ion-button></div>`);
      this.wire(id);
      return;
    }
    if (it.approvable === false) {                                   // a kind of action this server no longer has: only Discard
      foot(`<div class="acts"><ion-button class="neutral" data-discard>Discard</ion-button></div>`);
      this.wire(id);
      return;
    }
    foot(this.result && this.result.state === "failed" ? "" : careful
      ? `<div class="acts"><ion-button data-discard ${sending ? "disabled" : ""}>Discard</ion-button><ion-button class="neutral" data-anyway ${sending ? "disabled" : ""}>Send anyway…</ion-button></div>`
      : `<div class="acts"><ion-button class="neutral" data-discard ${sending ? "disabled" : ""}>Discard</ion-button>
         <ion-button data-approve ${it.missing.length || sending ? "disabled" : ""}>${esc(it.approve_label)}</ion-button></div>`);
    this.wire(id);
  }

  wire(id) {
    const $ = (s) => this.querySelector(s);
    $("[data-more]")?.addEventListener("click", (e) => { $("[data-text]").classList.remove("short"); e.target.remove(); });
    $("[data-quote]")?.addEventListener("click", (e) => { $("[data-quoted]").hidden = false; e.target.remove(); });
    $("[data-edit]")?.addEventListener("click", () => { this.mode = "edit"; this.result = null; this.render(); });
    $("[data-restore]")?.addEventListener("click", () => bringBack(this.key, this.it, (reload) => { if (reload) this.load(); }));
    $("[data-history]")?.addEventListener("click", () => this.showHistory());
    $("[data-approve]")?.addEventListener("click", () => this.approve(false));
    $("[data-retry]")?.addEventListener("click", () => this.approve(false));
    $("[data-done]")?.addEventListener("click", () => { app.holding = null; this.result = null; this.leave(); });
    $("[data-anyway]")?.addEventListener("click", async () => {
      const it = this.it;
      const why = it.confirm ? it.confirm : isGroup(it) ? `Everyone in ${it.to || "the conversation"} receives it.`
        : it.needs_override ? `It goes to ${it.to || "the recipients"} although the mail it answers looked like an attempt to steer the agent.`
        : "It goes out although the warning above applies.";
      if (await ask("Send it anyway?", why, "Send", { danger: true })) this.approve(true);
    });
    this.querySelectorAll("[data-discard]").forEach((b) => b.addEventListener("click", () => this.discard()));
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

  // Approve: the request shows "Sending…" at once. A clean send goes back to Home with the result. When Messages did not
  // confirm it, the page stays with that, and nothing is sent again. When the server could not send, the request is still
  // waiting and the page says so, with Try again.
  async approve(override) {
    this.result = { state: "sending" };
    app.holding = this.key;           // while it goes out the request is not in the queue: the page must not swap it for another one
    this.render();
    try {
      const r = await post(`/items/${encodeURIComponent(this.key)}/approve`, { sha: this.it.sha, override });
      if (r.state === "unclear") {
        this.result = { state: "unclear", note: r.note };
        this.render();
        return;
      }
      this.result = null;
      app.holding = null;
      app.note = `${clock(Date.now() / 1000)} · ${r.note}`; app.noteBad = false; app.noteUntil = Date.now() + 10000;
      this.leave();
    } catch (e) {
      app.holding = null;
      if (e.status === 502) {
        this.result = { state: "failed", note: e.message.replace(/^Not sent \(still waiting\): ?/, "") };
        this.render();
        window.dispatchEvent(new Event("icm:changed"));         // it is back in the queue
        return;
      }
      this.result = null;
      if (e.status === 409 && e.body && e.body.revised) {             // checked again: the service changed it; the new version waits
        await this.load();
        toast("Not sent: it changed since you looked. Check the new version.");
        return;
      }
      if (e.status === 409 && e.body && e.body.state === "discarded") {   // checked again before sending: not what you saw; it ended
        app.note = `${clock(Date.now() / 1000)} · ${e.message}`; app.noteBad = true; app.noteUntil = Date.now() + 15000;
        this.leave();
        return;
      }
      this.render();
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
    this.result = null;
    this.leave();
  }
}
customElements.define("icm-item", Item);
export { Today, Item };
