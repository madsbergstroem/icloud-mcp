// Editing the words of a waiting request: the edit view, saving, and bringing back what the agent asked for.
// The server keeps the agent's text until the request is sent, and the log records that an edit happened, never the text.
import { put } from "./api.js";
import { esc, toast, fail, ask, titleBlock } from "./ui.js";

const STAYS = { mail: "The recipients and the subject stay the same.", imessage: "The conversation stays the same.",
  calendar: "The event, its time and its guests stay the same." };

// The edit view of one request. `rows` is the message card's own rows (already HTML), `sub` the line under the title.
export function editView(it, rows, sub) {
  const n = it.edit.text.length;
  return titleBlock(it.title, sub)
    + `<div class="msg">${rows ? `<dl>${rows}</dl>` : ""}
      <ion-textarea aria-label="Text of the request" auto-grow="true" rows="4" data-ta value="${esc(it.edit.text)}"></ion-textarea>
      <div class="capr"><span>Only the text changes. ${esc(STAYS[it.kind] || "")}</span><span class="num" data-count>${n.toLocaleString()} characters</span></div></div>
      <p class="note">Your edit replaces what your assistant asked for. You can bring that back until you send.</p>`;
}

export function editFoot() {
  return `<div class="acts"><ion-button class="neutral" data-cancel>Cancel</ion-button><ion-button data-save disabled>Save changes</ion-button></div>`;
}

// Wires the edit view inside `el` (the request element). done() runs after a save or a cancel and loads the request again.
export function wireEdit(el, key, it, done) {
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
  if (!(await ask("Bring back the original?", "Your edit is replaced by what your assistant asked for.", "Bring it back"))) return;
  try {
    const r = await put(`/items/${encodeURIComponent(key)}/text`, { sha: it.sha, restore: true });
    toast(r.note);
    done(true);
  } catch (e) {
    fail(e);
    if (e.status === 409) done(true);
  }
}
