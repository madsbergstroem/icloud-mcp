/*
 * attach.js: the attach page (/dashboard/attach/<id>). A file dropped on or chosen in a slot is uploaded at once, with its
 * progress; Send unlocks when no file is missing. An added file can be replaced or removed until the mail is sent, and Send
 * locks again while a file is missing. Without this script each slot and file row is a plain form with its own buttons.
 */
(function () {
  "use strict";
  var root = document.querySelector("[data-attach]");
  if (!root) return;
  document.documentElement.classList.add("js");
  var id = root.getAttribute("data-id"), csrf = root.getAttribute("data-csrf"), limit = +root.getAttribute("data-limit");
  var approve = root.querySelector("[data-approve]"), sha = approve.querySelector('input[name="sha"]');
  var send = approve.querySelector("[data-send]"), sub = root.querySelector("[data-sub]"), err = root.querySelector("[data-error]");

  function mb(n) {   // as the server writes it (_size in dashboard.py)
    return n < 1048576 ? Math.max(1, Math.round(n / 1024)) + " KB" : (n % 1048576 ? (n / 1048576).toFixed(1) : n / 1048576) + " MB";
  }
  function esc(t) { var e = document.createElement("div"); e.textContent = t; return e.innerHTML; }
  function frag(html) { var t = document.createElement("template"); t.innerHTML = html; return t.content.firstChild; }
  function say(text) { err.textContent = text; err.hidden = !text; }

  function fail(slot, text) {
    slot.classList.add("bad");
    slot.querySelector(".slot-main").textContent = text;
    slot.querySelector(".slot-sub").textContent = "Drop another file or tap to choose one.";
  }

  // Send unlocks once nothing is missing, and locks again when a file is taken back out
  function ready(r) {
    sha.value = r.sha;
    var none = !r.waiting.length;
    send.disabled = !none;
    send.textContent = none ? send.getAttribute("data-label") : "Add the file first";
    sub.textContent = sub.getAttribute(none ? "data-ready" : "data-wait");
  }

  function post(url, fd, row, done, failed) {
    fd.append("csrf", csrf);
    var x = new XMLHttpRequest();
    x.open("POST", "/dashboard/attach/" + encodeURIComponent(id) + url);
    x.setRequestHeader("Accept", "application/json");
    if (row) x.upload.onprogress = function (e) {
      if (!e.lengthComputable) return;
      var p = Math.round(e.loaded / e.total * 100);
      row.querySelector(".file-sub").textContent = "Uploading · " + p + "%";
      row.querySelector(".bar i").style.width = p + "%";
    };
    x.onload = function () {
      var r = {};
      try { r = JSON.parse(x.responseText); } catch (e) { /* not JSON: the generic message below */ }
      if (x.status !== 200 || !r.ok) failed(r.error || "That did not work. Try again.");
      else done(r);
    };
    x.onerror = function () { failed("That did not go through. Check the connection and try again."); };
    x.send(fd);
  }

  // a file added for a placeholder: its name, Replace and Remove (the same markup the server renders)
  function fileRow(r) {
    var other = r.filename !== r.asked;
    var el = frag('<div class="file" data-file data-index="' + r.index + '"><span class="file-txt"><span class="file-name">' +
      esc(r.filename) + '</span><span class="file-sub">' + mb(r.size) + (other ? " · asked for " + esc(r.asked) : "") +
      "</span></span>" + (other ? '<span class="chip warn">Other name</span>' : "") +
      '<label class="btn text" data-replace>Replace<input type="file" name="file" hidden></label>' +
      '<button class="btn text danger" type="button" data-remove>Remove</button></div>');
    wireFile(el);
    return el;
  }

  // the empty slot again, after Remove
  function slotRow(index, asked) {
    var el = frag('<form class="slot-drop" data-slot data-index="' + index + '"><label class="slot-pick">' +
      '<input type="file" name="file"><span class="slot-txt"><span class="slot-main">Drop ' + esc(asked) +
      ' here</span><span class="slot-sub">or tap to choose · up to ' + mb(limit) + "</span></span></label></form>");
    wireSlot(el);
    return el;
  }

  // slot: the empty slot or the row of the file being replaced; it comes back if the upload fails
  function upload(slot, file) {
    say("");
    var isSlot = slot.hasAttribute("data-slot");
    function failed(text) { row.replaceWith(slot); if (isSlot) fail(slot, text); else say(text); }
    if (file.size > limit) {
      var big = "This file is " + mb(file.size) + ". The limit is " + mb(limit) + ".";
      if (isSlot) fail(slot, big); else say(big);
      return;
    }
    var row = frag('<div class="file busy"><span class="file-txt"><span class="file-name">' + esc(file.name) +
      '</span><span class="file-sub">Uploading · 0%</span><span class="bar"><i></i></span></span></div>');
    slot.replaceWith(row);
    var fd = new FormData();
    fd.append("index", slot.getAttribute("data-index"));
    fd.append("file", file, file.name);
    post("/file", fd, row, function (r) { row.replaceWith(fileRow(r)); ready(r); }, failed);
  }

  function remove(el, btn) {
    say("");
    btn.disabled = true;
    var fd = new FormData();
    fd.append("index", el.getAttribute("data-index"));
    post("/remove", fd, null, function (r) { el.replaceWith(slotRow(r.index, r.asked)); ready(r); },
      function (text) { btn.disabled = false; say(text); });
  }

  function wireSlot(slot) {
    var input = slot.querySelector('input[type="file"]');
    input.addEventListener("change", function () { if (input.files[0]) upload(slot, input.files[0]); });
    slot.addEventListener("dragover", function (e) { e.preventDefault(); slot.classList.add("over"); });
    slot.addEventListener("dragleave", function () { slot.classList.remove("over"); });
    slot.addEventListener("drop", function (e) {
      e.preventDefault();
      slot.classList.remove("over");
      if (e.dataTransfer.files[0]) upload(slot, e.dataTransfer.files[0]);
    });
  }

  function wireFile(el) {
    var input = el.querySelector("[data-replace] input"), btn = el.querySelector("[data-remove]");
    input.addEventListener("change", function () { if (input.files[0]) upload(el, input.files[0]); });
    btn.addEventListener("click", function (e) { e.preventDefault(); remove(el, btn); });
  }

  root.querySelectorAll("[data-slot]").forEach(wireSlot);
  root.querySelectorAll("[data-file]").forEach(wireFile);

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
