/*
 * attach.js: the attach page (/dashboard/attach/<id>). A file dropped on or chosen in a slot is uploaded at once, with its
 * progress; Send unlocks when no file is missing. Without this script each slot is a plain form with its own Upload button.
 */
(function () {
  "use strict";
  var root = document.querySelector("[data-attach]");
  if (!root) return;
  document.documentElement.classList.add("js");
  var id = root.getAttribute("data-id"), csrf = root.getAttribute("data-csrf"), limit = +root.getAttribute("data-limit");
  var approve = root.querySelector("[data-approve]"), sha = approve.querySelector('input[name="sha"]');
  var send = approve.querySelector("[data-send]"), sub = root.querySelector("[data-sub]"), err = root.querySelector("[data-error]");

  function mb(n) { return n < 1048576 ? Math.max(1, Math.round(n / 1024)) + " KB" : (n / 1048576).toFixed(1) + " MB"; }
  function esc(t) { var e = document.createElement("div"); e.textContent = t; return e.innerHTML; }
  function frag(html) { var t = document.createElement("template"); t.innerHTML = html; return t.content.firstChild; }
  function say(text) { err.textContent = text; err.hidden = !text; }

  function fail(slot, text) {
    slot.classList.add("bad");
    slot.querySelector(".slot-main").textContent = text;
    slot.querySelector(".slot-sub").textContent = "Drop another file or tap to choose one.";
  }

  function upload(slot, file) {
    say("");
    if (file.size > limit) { fail(slot, "This file is " + mb(file.size) + ". The limit is " + mb(limit) + "."); return; }
    var row = frag('<div class="file busy"><span class="file-txt"><span class="file-name">' + esc(file.name) +
      '</span><span class="file-sub">Uploading · 0%</span><span class="bar"><i></i></span></span></div>');
    slot.replaceWith(row);
    var fd = new FormData();
    fd.append("csrf", csrf);
    fd.append("index", slot.getAttribute("data-index"));
    fd.append("file", file, file.name);
    var x = new XMLHttpRequest();
    x.open("POST", "/dashboard/attach/" + encodeURIComponent(id) + "/file");
    x.setRequestHeader("Accept", "application/json");
    x.upload.onprogress = function (e) {
      if (!e.lengthComputable) return;
      var p = Math.round(e.loaded / e.total * 100);
      row.querySelector(".file-sub").textContent = "Uploading · " + p + "%";
      row.querySelector(".bar i").style.width = p + "%";
    };
    x.onload = function () {
      var r = {};
      try { r = JSON.parse(x.responseText); } catch (e) { /* not JSON: the generic message below */ }
      if (x.status !== 200 || !r.ok) { row.replaceWith(slot); fail(slot, r.error || "The upload failed. Try again."); return; }
      sha.value = r.sha;
      row.className = "file";
      row.innerHTML = '<span class="file-txt"><span class="file-name">' + esc(r.filename) + '</span><span class="file-sub">' +
        mb(r.size) + (r.other_name ? " · asked for " + esc(r.asked) : "") + "</span></span>" +
        (r.other_name ? '<span class="chip warn">Other name</span>' : "");
      if (!r.waiting.length) {
        send.disabled = false;
        send.textContent = send.getAttribute("data-label");
        sub.textContent = sub.getAttribute("data-ready");
      }
    };
    x.onerror = function () { row.replaceWith(slot); fail(slot, "The upload failed. Check the connection and try again."); };
    x.send(fd);
  }

  root.querySelectorAll("[data-slot]").forEach(function (slot) {
    var input = slot.querySelector('input[type="file"]');
    input.addEventListener("change", function () { if (input.files[0]) upload(slot, input.files[0]); });
    slot.addEventListener("dragover", function (e) { e.preventDefault(); slot.classList.add("over"); });
    slot.addEventListener("dragleave", function () { slot.classList.remove("over"); });
    slot.addEventListener("drop", function (e) {
      e.preventDefault();
      slot.classList.remove("over");
      if (e.dataTransfer.files[0]) upload(slot, e.dataTransfer.files[0]);
    });
  });

  approve.addEventListener("submit", function (e) {
    var btn = e.submitter;
    if (!btn) return;
    e.preventDefault();
    var fd = new FormData(approve);
    fd.append("action", btn.value);
    btn.disabled = true;
    // the buttons are named "action", which hides the form's own action property: read the attribute
    fetch(approve.getAttribute("action"), { method: "POST", body: fd, headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) { btn.disabled = false; say(j.error); return; }
        root.querySelector(".card-body").innerHTML = '<div class="t-head"><h1>' + (btn.value === "approve" ? "Sent" : "Discarded") +
          '</h1><p class="t-sub">' + esc(j.note) + "</p></div>";
        approve.innerHTML = '<a class="btn" href="/dashboard">Back to the overview</a>';
      })
      .catch(function () { btn.disabled = false; say("That did not go through. Check the connection and try again."); });
  });

  var more = root.querySelector("[data-more]"), body = root.querySelector("[data-body]");
  if (more && body) {
    if (body.scrollHeight <= body.clientHeight + 4) more.remove();
    else more.addEventListener("click", function () { body.classList.add("open"); more.remove(); });
  }
})();
