// Editing the words of a waiting request: the edit view, saving, and bringing back what the agent asked for.
// The server keeps the agent's text until the request is sent, and the log records that an edit happened, never the text.
import { put } from "./api.js";
import { esc, toast, fail, ask, titleBlock } from "./ui.js";

const STAYS = { mail: "The recipients and the subject stay the same.", imessage: "The conversation stays the same.",
  calendar: "The event, its time and its guests stay the same." };

// The edit view of one request. `rows` is the message card's own rows (already HTML), `sub` the line under the title.
export function editView(it, rows, sub) {
  if (it.edit.fields && it.edit.fields.length) return formView(it);
  const n = it.edit.text.length;
  return titleBlock(it.title, sub)
    + `<div class="msg">${rows ? `<dl>${rows}</dl>` : ""}
      <ion-textarea aria-label="Text of the request" auto-grow="true" rows="4" data-ta value="${esc(it.edit.text)}"></ion-textarea>
      <div class="capr"><span>Only the text changes. ${esc(STAYS[it.kind] || "")}</span><span class="num" data-count>${n.toLocaleString()} characters</span></div></div>
      <p class="note">${it.kind === "external" ? "Your edit becomes a new version. The earlier ones stay in the list."
        : "Your edit replaces what your assistant asked for. You can bring that back until you send."}</p>`;
}

export function editFoot() {
  return `<div class="acts"><ion-button class="neutral" data-cancel>Cancel</ion-button><ion-button data-save disabled>Save changes</ion-button></div>`;
}

// Package 2d: every field the owner may change, as one form like a mail being written: the label and the value on one line,
// the text below. A recipient typed in by hand who is not on the list is allowed for this request only (the server records it).
function formView(it) {
  const f = it.edit.fields;
  const line = (x, i) => {
    const v = x.value === null || x.value === undefined ? "" : String(x.value);
    if (x.type === "boolean") return `<div class="r"><span class="k">${esc(x.label)}</span><span class="v"><ion-toggle aria-label="${esc(x.label)}" data-f="${i}" ${x.value === true ? "checked" : ""}></ion-toggle></span></div>`;
    const kind = x.type === "number" || x.type === "integer" ? `type="number" inputmode="decimal"` : `type="text" autocapitalize="off" spellcheck="false"`;
    return `<div class="r${x.owner_allowed ? " own" : ""}"><span class="k">${esc(x.label)}</span><ion-input class="v" aria-label="${esc(x.label)}" data-f="${i}" ${kind} value="${esc(v)}"${x.limit ? ` maxlength="${x.limit}"` : ""}></ion-input></div>`
      + (x.owner_allowed ? `<p class="cf-note">Not on your list. You allowed it for this request.</p>` : "");
  };
  const rows = f.map((x, i) => (x.text ? "" : line(x, i))).join("");
  const ti = f.findIndex((x) => x.text);
  const text = ti < 0 ? "" : `<ion-textarea class="t" aria-label="Text of the request" auto-grow="true" rows="3" data-f="${ti}" value="${esc(String(f[ti].value || ""))}"></ion-textarea>`;
  const people = f.some((x) => x.recipient);
  return titleBlock(it.title, "Editing")
    + `<div class="cf">${rows}${text}</div>
      <p class="note">${people ? "Someone you type in who is not on your list is allowed for this request only. " : ""}Your edit becomes a new version. The earlier ones stay in the list.</p>`;
}

function wireForm(el, key, it, done) {
  const f = it.edit.fields, save = el.querySelector("[data-save]");
  const value = (x, node) => {
    if (x.type === "boolean") return !!node.checked;
    const raw = node.value === null || node.value === undefined ? "" : String(node.value);
    if ((x.type === "number" || x.type === "integer") && raw.trim() !== "") return Number(raw);
    return raw;
  };
  const nodes = [...el.querySelectorAll("[data-f]")];
  const changes = () => {
    const out = {};
    for (const n of nodes) {
      const x = f[Number(n.dataset.f)], v = value(x, n), was = x.value === null || x.value === undefined ? "" : x.value;
      if (v !== was && !(typeof v === "number" && Number.isNaN(v))) out[x.name] = v;
    }
    return out;
  };
  const check = () => { save.disabled = Object.keys(changes()).length === 0; };
  for (const n of nodes) { n.addEventListener("ionInput", check); n.addEventListener("ionChange", check); }
  el.querySelector("[data-cancel]").addEventListener("click", () => done(false));
  save.addEventListener("click", async () => {
    save.disabled = true;
    try {
      const r = await put(`/items/${encodeURIComponent(key)}/fields`, { sha: it.sha, fields: changes() });
      toast(r.changed ? r.note : "Nothing changed.");
      done(true);
    } catch (e) {
      fail(e);
      if (e.status === 409) done(true);
      else check();
    }
  });
}

// Wires the edit view inside `el` (the request element). done() runs after a save or a cancel and loads the request again.
export function wireEdit(el, key, it, done) {
  if (it.edit.fields && it.edit.fields.length) return wireForm(el, key, it, done);
  const ta = el.querySelector("[data-ta]"), save = el.querySelector("[data-save]"), count = el.querySelector("[data-count]");
  let draft = it.edit.text;
  const limit = it.edit.limit;
  const check = () => {
    const over = draft.length > limit;
    count.textContent = `${draft.length.toLocaleString()} characters${over ? `, ${limit.toLocaleString()} is the most` : ""}`;
    count.style.color = over ? "var(--negative)" : "";
    ta.classList.toggle("ion-invalid", over);
    save.disabled = over || draft === it.edit.text;
  };
  ta.addEventListener("ionInput", (e) => { draft = e.detail.value || ""; check(); });
  customElements.whenDefined("ion-textarea").then(() => setTimeout(() => ta.setFocus && ta.setFocus(), 50));
  el.querySelector("[data-cancel]").addEventListener("click", () => done(false));
  save.addEventListener("click", async () => {
    save.disabled = true;
    try {
      const r = await put(`/items/${encodeURIComponent(key)}/text`, { sha: it.sha, text: draft });
      toast(r.changed ? r.note : "Nothing changed.");
      done(true);
    } catch (e) {
      fail(e);
      if (e.status === 409) done(true);       // it changed or went away meanwhile: show the request as it is now
      else check();
    }
  });
}

// "Bring it back": the text the agent asked for replaces the owner's edit
export async function bringBack(key, it, done) {
  const why = it.versions ? "Your assistant's text becomes the newest version. Your edit stays in the list."
    : "Your edit is replaced by what your assistant asked for.";
  if (!(await ask("Bring back the original?", why, "Bring it back"))) return;
  try {
    const r = await put(`/items/${encodeURIComponent(key)}/text`, { sha: it.sha, restore: true });
    toast(r.note);
    done(true);
  } catch (e) {
    fail(e);
    if (e.status === 409) done(true);
  }
}
