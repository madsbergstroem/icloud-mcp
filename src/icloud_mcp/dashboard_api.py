"""The dashboard's JSON API, which the Ionic app (static/app/) talks to.

Every call needs the dashboard session cookie. Calls that change something also need the session's CSRF token in the
x-csrf header and must come from this origin. A step that changes how the server is protected answers 403
{"error": "confirm"} when the sign-in is older than five minutes; the app then asks for the passkey or the password
(POST /dashboard/api/signin with the session) and repeats the call.
"""
from __future__ import annotations

import asyncio
import email
import hmac
import importlib.metadata
import logging
import os
import re
import time
from datetime import datetime, timedelta
from email import policy
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import textedit as T
from .approval_withdraw import REASON_WORDS
from dataclasses import replace as dc_replace

from .gate.actions import Rechecked, Revised
from .dashboard_accounts import mail_account

log = logging.getLogger(__name__)

# What the preview shows inline. Everything else is offered by name and size only: the browser never runs or renders it.
_PREVIEW_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp", "image/heic", "application/pdf", "text/plain")
_WITHDRAWN_NOTICE_SECONDS = 24 * 3600      # Home mentions a request the assistant took back for this long, then it is only in Activity


def register_api(mcp: Any, d: Any, provider: Any, s: Any, audit: Any, *, signed_in: Callable, recent: Callable,
                 set_cookie: Callable, record_failure_line: Callable, headers: dict[str, str],
                 for_owner: Callable[[str], str],
                 owner_password_problem: Callable[[str, str], str]) -> None:
    from . import passkeys as P

    def ok(data: Any = None, status: int = 200) -> JSONResponse:
        return JSONResponse({"ok": True, **(data or {})}, status_code=status, headers=headers)

    def err(error: str, status: int, **extra: Any) -> JSONResponse:
        return JSONResponse({"ok": False, "error": error, **extra}, status_code=status, headers=headers)

    def need_confirm(why: str = "") -> JSONResponse:
        return err("confirm", 403, confirm=True, message="Confirm it is you: this step changes how your server is protected.",
                   **({"why": why} if why else {}))

    def reader(request: Request) -> tuple[str, dict[str, Any]] | JSONResponse:
        got = signed_in(request)
        return got if got is not None else err("signed out", 401)

    async def writer(request: Request) -> tuple[str, dict[str, Any], dict[str, Any]] | JSONResponse:
        got = signed_in(request)
        if got is None:
            return err("signed out", 401)
        if not d.same_origin(request):
            return err("This request did not come from this page.", 403)
        if not hmac.compare_digest(request.headers.get("x-csrf", "").encode(), got[1]["csrf"].encode()):
            return err("expired", 403, message="This page has expired. Reload it and try again.")
        try:
            body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        except ValueError:
            body = {}
        return got[0], got[1], body if isinstance(body, dict) else {}

    def route(path: str, methods: list[str]):
        return mcp.custom_route(f"/dashboard/api{path}", methods=methods)

    def version() -> str:
        try:
            return importlib.metadata.version("icloud-mcp-server")
        except importlib.metadata.PackageNotFoundError:
            return "unknown"

    def via_text(rec: dict[str, Any]) -> str:
        via = str(rec.get("via", ""))
        return "the owner password" if via == "password" else via

    def entry_tag(e: dict[str, Any]) -> tuple[str, str]:
        """A small tag and one line of explanation for the entries that need one: an edit, a send that is not certain."""
        action, delivery = e.get("action"), (e.get("details") or {}).get("delivery")
        if action == "approval.edited":
            return "Edited", "The text changed. The log keeps no text."
        if action == "approval.edit_undone":
            return "Edited", "Back to what was asked for."
        if action == "approval.withdrawn":
            why = REASON_WORDS.get(str((e.get("details") or {}).get("reason")), "")
            return "Withdrawn", "Withdrawn by your assistant" + (f": {why}." if why else ".") + " Nothing was sent."
        if action == "approval.revised":
            return "Changed", "The target changed after you looked. Nothing was sent; it waits with the new details."
        if action == "approval.discarded" and (e.get("details") or {}).get("reason") == "target_changed":
            return "Not sent", "The target changed after you looked. Nothing was sent."
        if action == "approval.discarded" and (e.get("details") or {}).get("reason") == "not_allowed":
            return "Not sent", "Your lists no longer allow someone it would reach. Nothing was sent."
        if action == "approval.sent" and delivery in ("unconfirmed", "failed"):
            if (e.get("details") or {}).get("kind") == "external" and (e.get("details") or {}).get("action_type") != "imessage.send":
                return "Unclear", "It may or may not have gone through. Nothing is sent again."
            return "Unclear", "Messages did not confirm it. Nothing is sent again."
        return "", ""

    def entry(e: dict[str, Any]) -> dict[str, Any]:
        """'You' / 'Someone' / the app's name, and what it did, starting lower-case: 'signed in with a passkey'."""
        actor = e.get("actor") or "?"
        text = re.sub(r"\bimessage\b", "iMessage", str(e.get("summary") or e.get("action", "")))      # older lines were written in lower case
        tag, note = entry_tag(e)
        who = "Your assistant" if e.get("action") == "approval.withdrawn" else "You" if actor == "owner" else ("Someone" if actor == "unknown" else actor)
        return {"t": e.get("t"), "time": d.fmt_time(e.get("t")), "who": who,
                "what": text[:1].lower() + text[1:], "ok": e.get("ok", True), "error": (e.get("details") or {}).get("error", ""),
                "tag": tag, "note": note}

    # -- the activity table: what each entry did, to what, and its details (the log holds names, addresses and hashes, no text)
    actions = {"all": ("All actions", ()), "approvals": ("Approvals", ("approval.",)), "agents": ("Agent requests", ("tool.",)),
               "settings": ("Settings", ("settings.", "accounts.", "push.", "server.", "owner_password.")),
               "signins": ("Sign-ins", ("signin.", "passkey.", "app."))}
    labels = {"approval.sent": "Approved", "approval.discarded": "Discarded", "approval.edited": "Edited", "approval.edit_undone": "Edited",
              "approval.withdrawn": "Withdrawn", "approval.revised": "Changed", "approval.expired": "Expired", "approval.failed": "Failed",
              "approval.file_added": "File added", "approval.file_removed": "File removed", "tool.queued": "Asked",
              "signin.passkey": "Sign-in", "signin.password": "Sign-in", "signin.access": "Sign-in", "signin.confirm": "Confirmed",
              "signin.signout": "Signed out", "passkey.added": "Passkey added", "passkey.removed": "Passkey removed",
              "app.connected": "App connected", "app.denied": "App refused", "app.signed_out": "App signed out",
              "app.signed_out_all": "App signed out", "settings.changed": "Setting", "accounts.changed": "Accounts",
              "push.added": "Notifications", "push.removed": "Notifications", "server.paused": "Paused", "server.resumed": "Resumed",
              "server.restart": "Restarted", "owner_password.changed": "Password"}
    warn_tags = ("Changed", "Unclear", "Withdrawn", "Expired", "Not sent")
    lead = re.compile(r"^(?:Approved|Discarded|Did not send|Could not send|Withdrew|Edited|Brought back the original text of|Added|Removed"
                      r"|Took the file for|Signed in (?:with|through)|Confirmed with|Connected|Refused to connect|Signed out(?: of)?"
                      r"|Turned (?:on|off) notifications for|Asked to|Changed|Reset|Paused|Resumed|Restarted|An upload added)\s+")
    results = {"confirmed": "Delivered", "sent": "Sent", "unconfirmed": "Not confirmed: it may or may not have gone through",
               "failed": "Failed: it may or may not have gone through"}
    reasons = {"target_changed": "The target changed after you looked", "not_allowed": "Your lists no longer allow someone it would reach",
               **REASON_WORDS}

    def actor_label(actor: str) -> str:
        return {"owner": "You", "server": "Server", "unknown": "Someone", "agent": "Upload"}.get(actor, actor or "?")

    def short(h: Any) -> str:
        h = str(h or "")
        return f"{h[:8]}…{h[-4:]}" if len(h) > 16 else h

    def when(t: float | None) -> str:
        if not t:
            return ""
        day, today_ = datetime.fromtimestamp(t, d.tz).date(), datetime.now(d.tz).date()
        name = "Today" if day == today_ else "Yesterday" if day == today_ - timedelta(days=1) else day.strftime("%a %d %b")
        return f"{name} {d.fmt_time(t)}"

    def target(e: dict[str, Any], diff: list[Any]) -> str:
        """What the entry acted on: its summary without the verb the action tag already says, and without the reason."""
        action, text = str(e.get("action", "")), str(e.get("summary") or e.get("action", ""))
        text = re.sub(r"\bimessage\b", "iMessage", text)
        if action.startswith("tool.") and action != "tool.queued":
            return text                            # an agent's own action: the verb is the news
        text = lead.sub("", text, count=1).replace(" (waiting for your approval)", "").removesuffix(" before sending")
        if diff or action in ("approval.revised", "approval.discarded", "approval.withdrawn"):
            text = text.split(": ", 1)[0]
        return text if text[1:2].isupper() else text[:1].upper() + text[1:]      # “iMessage …” keeps its small i

    def details(e: dict[str, Any]) -> list[list[str]]:
        x, rows = e.get("details") or {}, []
        add = lambda label, v: rows.append([label, str(v)]) if v not in (None, "", [], {}) else None  # noqa: E731
        add("Request", x.get("outbox_id"))
        add("Replaces request", x.get("replaced"))
        add("Version", x.get("version"))
        add("Action", x.get("action_type"))
        add("Tool", x.get("tool"))
        add("Sent to", ", ".join(map(str, x.get("recipients") or [])))
        add("Count", x.get("count"))
        add("To", x.get("destination"))
        add("Result", results.get(str(x.get("delivery")), x.get("delivery")))
        add("Why", reasons.get(str(x.get("reason")), x.get("reason")))
        add("Loosened", ", ".join(map(str, x.get("weakened") or [])))
        if x.get("new_sha256"):
            add("Hash before", short(x.get("sha256")))
            add("Hash after", short(x.get("new_sha256")))
        else:
            add("Hash", short(x.get("sha256")))
        add("Hash it replaced", short(x.get("previous_sha256")))
        add("Error", x.get("error"))
        add("Signed in with" if e.get("actor") == "owner" else "Client", via_text(e))
        return rows

    def row(e: dict[str, Any]) -> dict[str, Any]:
        """One line of the table: time, actor, action, target; opened, the details and what changed from what to what."""
        action, base = str(e.get("action", "")), entry(e)
        x = e.get("details") or {}
        diff = [{"label": str(c[0]), "old": str(c[1]), "new": str(c[2])} for c in (x.get("changes") or []) if isinstance(c, list) and len(c) == 3]
        name = base["tag"] if base["tag"] in ("Unclear", "Not sent") else labels.get(action, "Agent" if action.startswith("tool.") else action)
        if not e.get("ok", True):
            name = "Sign-in failed" if action.startswith("signin.") else "Failed" if name not in ("Unclear",) else name
        tone = "bad" if not e.get("ok", True) else "warn" if name in warn_tags else ""
        return {**base, "when": when(e.get("t")), "actor": actor_label(str(e.get("actor") or "")), "action": name, "tone": tone,
                "target": target(e, diff), "details": details(e), "diff": diff}

    # -- session -------------------------------------------------------------------------------------------------------
    @route("/session", ["GET"])
    async def session(request: Request) -> Response:
        got = signed_in(request)
        base = {"host": d.host, "password_login": d.password_login(), "passkeys": bool(d.passkeys.list()) and P.available(),
                "passkeys_available": P.available()}
        if got is None:
            return ok({"signed_in": False, **base})
        rec = got[1]
        return ok({"signed_in": True, **base, "csrf": rec["csrf"], "via": via_text(rec), "signed_in_at": rec.get("created"),
                   "recent": recent(rec), "version": version(), "timezone": s.default_timezone,
                   "settings": any(k == "settings" for k, _, _ in d.extra_nav)})

    @route("/signin", ["POST"])
    async def signin(request: Request) -> Response:
        """The owner password: a new session, or a fresh sign-in for the current one (confirming a protected step)."""
        if not d.same_origin(request):
            return err("This request did not come from this page.", 403)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        password = str((body or {}).get("password", "")) if isinstance(body, dict) else ""
        if not d.password_login():
            return err("Sign-in with the owner password is turned off. Use your passkey.", 403)
        if not provider.login_allowed():
            return err("Too many wrong passwords. Try again in 15 minutes" + (", or use your passkey." if d.passkeys.list() else "."), 429)
        if not hmac.compare_digest(password.encode(), s.owner_password.encode()):
            provider.record_failure()
            log.warning("Failed owner-password attempt on the dashboard")
            record_failure_line("signin.password", "Entered a wrong owner password",
                                details={"from": request.client.host if request.client else ""})
            return err("Incorrect password.", 401)
        token, rec = d.session(request)
        if rec is not None:
            if not hmac.compare_digest(request.headers.get("x-csrf", "").encode(), rec["csrf"].encode()):
                return err("expired", 403, message="This page has expired. Reload it and try again.")
            fresh = d.sessions.reauth(token, "password")
            audit.record("signin.confirm", actor="owner", via="password", summary="Confirmed with the owner password")
            resp = ok({"confirmed": True})
            return set_cookie(resp, fresh) if fresh else resp
        token, rec = d.sessions.create("password")
        audit.record("signin.password", actor="owner", via="password", summary="Signed in with the owner password")
        return set_cookie(ok(), token)

    @route("/signout", ["POST"])
    async def signout(request: Request) -> Response:
        got = await writer(request)
        if isinstance(got, Response):
            return got
        d.sessions.drop(got[0])
        audit.record("signin.signout", actor="owner", via=got[1].get("via", ""), summary="Signed out of the dashboard")
        resp = ok()
        resp.delete_cookie(d.cookie, path="/", secure=d.secure, httponly=True, samesite="strict")
        return resp

    # -- what waits ----------------------------------------------------------------------------------------------------
    def short_names(addresses: str) -> str:
        """'Segway Support <support@example.org>, bob@example.org' -> 'Segway Support, bob@example.org': the list reads as people."""
        from email.utils import getaddresses
        names = [n or a for n, a in getaddresses([addresses or ""]) if n or a]
        return ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")

    def sent_item(e: dict[str, Any]) -> dict[str, Any]:
        """An approval from the log as a row: the title, what it was and to whom, when."""
        text = str(e.get("summary") or "")
        m = re.match(r"Approved (.+?) “(.*)”$", text)
        rcpt = (e.get("details") or {}).get("recipients") or []
        return {"t": e.get("t"), "time": d.fmt_time(e.get("t")), "title": m.group(2) if m else text,
                "what": ("iMessage" if m.group(1) == "imessage" else m.group(1)[:1].upper() + m.group(1)[1:]) if m else "",
                # an action names who it reached as its type says it ("Hiking crew"), not as mail addresses to shorten
                "to": str(rcpt[0]) if rcpt and (e.get("details") or {}).get("kind") == "external" else short_names(", ".join(map(str, rcpt[:1]))),
                "unclear": (e.get("details") or {}).get("delivery") in ("unconfirmed", "failed")}

    def withdrawn_item(e: dict[str, Any]) -> dict[str, Any]:
        """A request the assistant took back, as a row: what Activity shows for it (title, kind, first recipient, reason), no text."""
        det = e.get("details") or {}
        m = re.match(r"Withdrew (.+?) “(.*)”$", str(e.get("summary") or ""))
        badge = (m.group(1) if m else "") or {"mail": "email"}.get(str(det.get("kind")), "request")
        why = REASON_WORDS.get(str(det.get("reason")), "")
        return {"t": e.get("t"), "time": d.fmt_time(e.get("t")), "title": m.group(2) if m else "A queued request",
                "what": badge[:1].upper() + badge[1:], "to": short_names(", ".join(map(str, (det.get("recipients") or [])[:1]))),
                "why": why[:1].upper() + why[1:] + "." if why else ""}

    def label_of(src: Any, q: Any) -> str:
        return src.approve_label_for(q) if hasattr(src, "approve_label_for") else src.approve_label

    def to_of(v: dict[str, Any]) -> str:
        return next((val for k, val in v["rows"] if k in ("To", "Will be delivered to", "Guests", "Conversation")), "")

    def summary(src: Any, q: Any, v: dict[str, Any]) -> dict[str, Any]:
        return {"key": f"{src.kind}:{q.id}", "kind": src.kind, **({"kind_label": src.kind_label(q)} if hasattr(src, "kind_label") else {}), "badge": v["badge"], "title": v["title"], "to": v.get("label") or short_names(to_of(v)),
                "by": getattr(q, "requested_by", None) or "", "created_at": q.created_at, "expires_at": q.expires_at,
                "warning": bool(v["warnings"] or v["needs_override"]), "blocked": bool(v.get("blocked")), "group": bool(v.get("group")),
                **({"tags": v["tags"]} if v.get("tags") else {}), **({"to_shown": True} if v.get("log_to") else {}),
                **({"subject": v["subject"]} if v.get("subject") else {}),
                **({"held": True} if v.get("held") else {}),
                **({"action_type": q.action_type} if getattr(q, "action_type", None) else {})}

    def described(only: str = "") -> list[tuple[Any, Any, dict[str, Any]]]:
        out = []
        for src in d.sources.values():
            items = [q for q in src.pending() if not only or f"{src.kind}:{q.id}" == only]
            if not items:
                continue
            ctx = src.contexts(items, prune=not only)       # the whole queue: drop what is kept for items that have gone
            out += [(src, q, src.describe(q, ctx.get(q.id))) for q in items]
        out.sort(key=lambda w: w[1].expires_at)
        return out

    @route("/today", ["GET"])
    async def today(request: Request) -> Response:
        got = reader(request)
        if isinstance(got, Response):
            return got
        waiting = await asyncio.to_thread(described)
        day = datetime.now(d.tz).date()
        sent = [e for e in await asyncio.to_thread(audit.entries, 300, ("approval.sent",))
                if datetime.fromtimestamp(e.get("t", 0), d.tz).date() == day]
        n, fingerprint = await asyncio.to_thread(d.fingerprint)
        taken_back = [withdrawn_item(e) for e in await asyncio.to_thread(audit.entries, 100, ("approval.withdrawn",))
                      if time.time() - e.get("t", 0) < _WITHDRAWN_NOTICE_SECONDS]
        return ok({"waiting": [summary(*w) for w in waiting], "v": fingerprint, "paused": d.paused(),
                   "withdrawn": taken_back,           # each request the assistant took back in the last 24 hours: Home lists them, and lets go of each on time
                   "sent_today": [sent_item(e) for e in sent[:10]],
                   "recent_burst": sum(time.time() - q.created_at < 3600 for _, q, _ in waiting)})

    def waiting_by_kind() -> dict[str, int]:
        """How many wait, per kind. Requests of the external source count under their type's badge, as `external:<badge>`."""
        out: dict[str, int] = {}
        for k, src in d.sources.items():
            if not hasattr(src, "kind_label"):
                out[k] = len(src.pending())
                continue
            out.update({f"{k}:{label}": 0 for label in src.kind_labels()})
            for q in src.pending():
                key = f"{k}:{src.kind_label(q)}"
                out[key] = out.get(key, 0) + 1
        return out

    @route("/state", ["GET"])
    async def state(request: Request) -> Response:
        got = reader(request)
        if isinstance(got, Response):
            return got
        n, fingerprint = await asyncio.to_thread(d.fingerprint)
        kinds = await asyncio.to_thread(waiting_by_kind)
        mac = bridge_state()
        return ok({"waiting": n, "v": fingerprint, "paused": d.paused(), "kinds": kinds,
                   "mac": "off" if not mac.get("set_up") else ("online" if mac.get("online") else "offline")})

    def versions_of(src: Any, q: Any) -> list[dict[str, Any]] | None:
        """The versions of a request as the card lists them (package 2c), oldest first, each with what changed against the one
        before; None for a source without versions or a request that has only one."""
        box = getattr(src, "box", None)
        if box is None or not hasattr(box, "versions"):
            return None
        vs = box.versions(q.id)
        if len(vs) < 2:
            return None
        looks: list[dict[str, Any] | None] = []
        for v in vs:
            try:
                looks.append(src.describe(dc_replace(q, raw=v["raw"], sha256=v["sha256"])) if v["raw"] is not None else None)
            except Exception:  # noqa: BLE001  -- a version that cannot be read is listed without its content
                looks.append(None)
        out = []
        for i, v in enumerate(vs):
            cur, prev = looks[i], looks[i - 1] if i else None
            changes = []
            if cur is not None and prev is not None:
                before = dict(prev["rows"])
                changes = [{"label": k, "old": before.get(k, ""), "new": val} for k, val in cur["rows"] if before.get(k) != val]
                now = dict(cur["rows"])                          # a row the new version no longer has (an emptied field) changed too
                changes += [{"label": k, "old": val, "new": ""} for k, val in prev["rows"] if k not in now]
                if cur["text"] != prev["text"]:
                    changes.append({"label": "Text", "old": prev["text"], "new": cur["text"]})
            out.append({"n": v["n"], "by": v["author"], "at": v["created_at"], "changes": changes,
                        "rows": [[k, val] for k, val in (cur or {}).get("rows", [])], "text": (cur or {}).get("text", "")})
        return out

    def mail_parts(q: Any) -> Any:
        return email.message_from_bytes(q.raw, policy=policy.default)

    @route("/items/{key}", ["GET"])
    async def item(request: Request) -> Response:
        got = reader(request)
        if isinstance(got, Response):
            return got
        key = request.path_params["key"]
        found = await asyncio.to_thread(described, key)
        if not found:
            return err("This is no longer waiting. It was sent, discarded or it expired.", 404, gone=True)
        src, q, v = found[0]
        out = {**summary(src, q, v), "rows": [[k, val] for k, val in v["rows"]], "text": v["text"], "notes": v["notes"],
               "warnings": v["warnings"], "needs_override": v["needs_override"], "sha": q.sha256,
               **({"text_style": v["text_style"]} if v.get("text_style") else {}), **({"confirm": v["confirm"]} if v.get("confirm") else {}),
               **({"text_caption": v["text_caption"]} if v.get("text_caption") else {}),
               "approve_label": label_of(src, q), **({"approvable": False} if v.get("approvable") is False else {}),
               **({"held": v["held"]} if v.get("held") else {}), "files": [], "missing": [], "original": None,
               "edit": await asyncio.to_thread(T.edit_info, src, q),
               **({"row_tags": v["row_tags"]} if v.get("row_tags") else {}), **({"edit_note": v["edit_note"]} if v.get("edit_note") else {}),
               "account": mail_account(s) if src.kind == "mail" else ""}
        if hasattr(src, "files_of") and (f := await asyncio.to_thread(src.files_of, q)) is not None:     # package 6b: a type's files
            out["files"] = [{**x, "preview": x.get("content_type") in _PREVIEW_TYPES} for x in f["files"]]
            out["missing"], out["max_bytes"] = f["missing"], f["max_bytes"]
        if isinstance(v.get("original"), dict):                   # what the request answers, as its type read it (6b)
            out["original"] = {k: v["original"].get(k) for k in ("label", "subject", "from", "unavailable") if k in v["original"]}
        if versions := await asyncio.to_thread(versions_of, src, q):
            out["versions"] = versions
            if versions[-1]["by"] == "service":
                out["changed"] = versions[-1]["changes"]
            elif versions[-1]["by"] == "owner":                  # package 2d: the rows the owner changed are marked, without a warning
                out["edited_rows"] = [c["label"] for c in versions[-1]["changes"] if c["label"] != "Text"]
        if src.kind == "mail":
            dq = await asyncio.to_thread(src.mail.describe_queued, q, src.contexts([q], prune=False).get(q.id))
            added = {x["index"]: x["asked"] for x in dq["added_files"]}
            out["files"] = [{"index": a["index"], "filename": a["filename"], "size": a["size"], "content_type": a["content_type"],
                             "added": a["index"] in added, "asked": added.get(a["index"], ""),
                             "preview": a["content_type"] in _PREVIEW_TYPES} for a in dq["attachments"]]
            out["missing"] = [{"index": w["index"], "filename": w["filename"]} for w in dq["pending_files"]]
            out["max_bytes"] = s.max_attachment_bytes
            if orig := dq.get("original"):
                frm = ", ".join(a.get("email", "") for a in orig.get("from") or [] if isinstance(a, dict))
                out["original"] = {"subject": orig.get("subject") or "", "from": frm, "unavailable": bool(orig.get("unavailable"))}
        return ok(out)

    @route("/items/{key}/files/{index}", ["GET"])
    async def item_file(request: Request) -> Response:
        """One attachment of a waiting mail, for the preview. Only images, PDF and plain text are sent, inline, with a CSP
        that lets nothing in them run; the file never leaves this server for anywhere else."""
        got = reader(request)
        if isinstance(got, Response):
            return got
        kind, _, item_id = request.path_params["key"].partition(":")
        src = d.sources.get(kind) if kind in ("mail", "external") else None
        q = next((x for x in src.pending() if x.id == item_id), None) if src else None
        if q is None:
            return err("This is no longer waiting.", 404)
        try:
            index = int(request.path_params["index"])
        except ValueError:
            return err("No such file.", 404)
        if kind == "external":                                   # a type's files (package 6b)
            try:
                got = await asyncio.to_thread(src.file_content, q, index)
            except Exception:  # noqa: BLE001
                got = None
            if got is None:
                return err("No such file.", 404)
            data, ctype = got
        else:
            from .mail import iter_attachment_parts
            parts = list(iter_attachment_parts(mail_parts(q)))
            if not 0 <= index < len(parts):
                return err("No such file.", 404)
            part = parts[index]
            ctype = part.get_content_type()
            data = part.get_payload(decode=True) or b"" if ctype in _PREVIEW_TYPES else b""
        if ctype not in _PREVIEW_TYPES:
            return err("This kind of file is not shown here.", 415)
        return Response(data, media_type=ctype, headers={**headers, "Content-Disposition": "inline",
                                                         "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'self'; sandbox",
                                                         "X-Frame-Options": "SAMEORIGIN",   # the app shows it in its own frame
                                                         "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})

    @route("/items/{key}/{action}", ["POST"])
    async def act(request: Request) -> Response:
        got = await writer(request)
        if isinstance(got, Response):
            return got
        _, rec, body = got
        action = request.path_params["action"]
        kind, _, item_id = request.path_params["key"].partition(":")
        src = d.sources.get(kind)
        q = next((x for x in src.pending() if x.id == item_id), None) if src else None
        if action not in ("approve", "discard"):
            return err("Unknown action.", 404)
        if q is None or not hmac.compare_digest(str(body.get("sha", "")).encode(), q.sha256.encode()):
            return err("That item is no longer waiting or has changed. Nothing was done.", 409)
        ctx = await asyncio.to_thread(src.contexts, [q], False)
        v = await asyncio.to_thread(src.describe, q, ctx.get(q.id))
        who = {"actor": "owner", "via": rec.get("via", "")}
        if action == "discard":
            await asyncio.to_thread(src.discard, q)
            audit.record("approval.discarded", **who, summary=f"Discarded {v['badge'].lower()} “{v['title']}”", kind=src.kind, outbox_id=q.id,
                         action_type=getattr(q, "action_type", None), sha256=q.sha256)
            return ok({"note": "Discarded. Nothing was sent."})
        if v.get("approvable") is False:
            return err("This cannot be approved. Discard it.", 409)
        if v.get("held"):
            return err(f"Not sent: {v['held']}", 409)
        if v["needs_override"] and body.get("override") is not True:
            return err("Not sent: confirm that you still want it sent despite the warning.", 422)
        if v.get("blocked"):
            return err("Not sent: a file is still missing. Add it first.", 409)
        try:
            note = await asyncio.to_thread(src.release, q)
        except Revised as e:                       # checked again just before sending: the service has new values; a new version waits
            audit.record("approval.revised", actor="server", summary=f"Did not send {v['badge'].lower()} “{v['title']}”: the target changed, "
                         f"it waits as version {e.n}", kind=src.kind, outbox_id=q.id, action_type=getattr(q, "action_type", None),
                         sha256=q.sha256, new_sha256=e.sha256, reason="target_changed", version=e.n)
            return err(str(e), 409, state="waiting", revised=True, version=e.n)
        except Rechecked as e:                     # checked again just before sending: not the target the owner saw; it ends here
            why = "your lists no longer allow it" if e.reason == "not_allowed" else "the target changed"
            audit.record("approval.discarded", actor="server", summary=f"Did not send {v['badge'].lower()} “{v['title']}”: {why}",
                         kind=src.kind, outbox_id=q.id, action_type=getattr(q, "action_type", None), sha256=q.sha256, reason=e.reason)
            return err(str(e), 409, gone=True, state="discarded")
        except Exception as e:  # noqa: BLE001  -- release() puts the item back; the owner can retry or discard
            log.warning("Release of %s %s failed: %s", src.kind, q.id, e)
            audit.record("approval.failed", **who, ok=False, summary=f"Could not send {v['badge'].lower()} “{v['title']}”", kind=src.kind, outbox_id=q.id,
                         action_type=getattr(q, "action_type", None), sha256=q.sha256)
            return err(f"Not sent (still waiting): {for_owner(str(e))}", 502)
        audit.record("approval.sent", **who, summary=f"Approved {v['badge'].lower()} “{v['title']}”", kind=src.kind,
                     outbox_id=q.id, delivery=getattr(note, "delivery", None), action_type=getattr(q, "action_type", None), sha256=q.sha256,
                     **({"refused": note.refused} if getattr(note, "refused", 0) else {}),       # package 6a: recipients the server refused
                     # D4-2: a type the server registers itself names whom it reached as its type says it, and only that
                     recipients=([v["label"]] if v.get("label") else []) if v.get("log_to")
                     else [val for k, val in v["rows"] if k in ("To", "Guests", "Will be delivered to")])
        # "state" only when the send is not certain; nothing is sent again from here
        return ok({"note": note, **({"state": "unclear"} if getattr(note, "delivery", None) in ("unconfirmed", "failed") else {})})

    # -- the files a waiting request carries (package 6b) -------------------------------------------------------------------
    # The owner adds a file the request waits for on its card, or replaces or removes one added before; an agent with a shell PUTs
    # it to /files/<token> (the upload_url of its answer). That route is outside /dashboard, so a sign-in proxy in front can let
    # it through: the token is its only key. The paths are those mail used before 6b, so links and the app stay as they were.
    def external_src() -> Any:
        return d.sources.get("external")

    async def form_writer(request: Request) -> tuple[str, dict[str, Any], Any] | JSONResponse:
        got = signed_in(request)
        if got is None:
            return err("signed out", 401)
        if not d.same_origin(request):
            return err("This request did not come from this page. Nothing was changed.", 403)
        form = await request.form()
        if not hmac.compare_digest(str(form.get("csrf", "")).encode(), got[1]["csrf"].encode()):
            return err("This page has expired. Reload it and try again.", 403)
        return got[0], got[1], form

    def what_of(q: Any) -> str:
        src = external_src()
        try:
            return src.describe(q)["badge"].lower()
        except Exception:  # noqa: BLE001
            return "request"

    @mcp.custom_route("/dashboard/attach/{item_id}/file", methods=["POST"])
    async def attach_file(request: Request) -> Response:
        got = await form_writer(request)
        if isinstance(got, Response):
            return got
        _, rec, form = got
        up, src = form.get("file"), external_src()
        if src is None or not hasattr(up, "read"):
            return err("Choose a file first.", 400)
        limit = max([r.files.max_bytes for r in src.registry.registrations() if r.files is not None] or [0])
        data = await up.read(limit + 1)
        try:
            q, info = await asyncio.to_thread(src.put_file, data, filename=up.filename, content_type=up.content_type,
                                              item_id=request.path_params["item_id"], index=int(str(form.get("index", "-1"))))
        except (LookupError, ValueError, RuntimeError) as e:
            return err(str(e).strip("'\""), 409)
        audit.record("approval.file_added", actor="owner", via=rec.get("via", ""), kind="external", outbox_id=q.id,
                     action_type=getattr(q, "action_type", None), summary=f"Added {info['filename']} to a waiting {what_of(q)}")
        return ok({"sha": q.sha256, "index": info["index"], "filename": info["filename"], "size": info["size"], "asked": info["asked"],
                   "other_name": info["filename"] != info["asked"], "waiting": [w["filename"] for w in info["waiting"]]})

    @mcp.custom_route("/dashboard/attach/{item_id}/remove", methods=["POST"])
    async def attach_remove(request: Request) -> Response:
        got = await form_writer(request)
        if isinstance(got, Response):
            return got
        _, rec, form = got
        src = external_src()
        try:
            if src is None:
                raise LookupError("There is no added file here to remove: the message was sent, discarded or has expired.")
            q, info = await asyncio.to_thread(src.remove_file, request.path_params["item_id"], int(str(form.get("index", "-1"))))
        except (LookupError, ValueError, RuntimeError) as e:
            return err(str(e).strip("'\""), 409)
        audit.record("approval.file_removed", actor="owner", via=rec.get("via", ""), kind="external", outbox_id=q.id,
                     action_type=getattr(q, "action_type", None), summary=f"Took the file for {info['asked']} out of a waiting {what_of(q)}")
        return ok({"sha": q.sha256, "index": info["index"], "asked": info["asked"], "waiting": [w["filename"] for w in info["waiting"]]})

    @mcp.custom_route("/files/{token}", methods=["PUT"])
    async def file_put(request: Request) -> Response:
        """An agent's upload: the raw bytes of one file a waiting request asked for. The token from the agent's result is the key."""
        src = external_src()
        kinds = [r.files for r in src.registry.registrations() if r.files is not None] if src is not None else []
        if not kinds:
            return err("No file can be added on this server.", 404)
        limit = max(f.max_bytes for f in kinds)
        named = next((n for f in kinds if (n := getattr(f, "limit_name", ""))), "")
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > limit:
                return err(f"The file is larger than {limit} bytes" + (f" ({named})." if named else "."), 413)
        try:
            q, info = await asyncio.to_thread(src.put_file, bytes(body), filename=request.query_params.get("filename"),
                                              content_type=request.headers.get("content-type"), token=request.path_params["token"])
        except LookupError:
            return err("No file is waiting here: it was already added, or the message was sent, discarded or has expired.", 404)
        except (ValueError, RuntimeError) as e:
            return err(str(e), 400)
        audit.record("approval.file_added", actor="agent", kind="external", outbox_id=q.id, action_type=getattr(q, "action_type", None),
                     summary=f"An upload added {info['filename']} to a waiting {what_of(q)}")
        d.push.notify("file", f"external:{q.id}")
        left = [w["filename"] for w in info["waiting"]]
        return ok({"filename": info["filename"], "size": info["size"], "still_waiting": left,
                   "next": "Upload the other files too." if left else info.get("next") or "It now waits for the owner's approval. It is not sent yet."})

    # -- activity ------------------------------------------------------------------------------------------------------
    @route("/activity", ["GET"])
    async def activity(request: Request) -> Response:
        """Filters, combined: days (1, 7, 30 or how long the log keeps), action (a group of actions; `filter` is the older
        name), actor (one actor of the entries in that time and group). The newest first, at most 1000."""
        got = reader(request)
        if isinstance(got, Response):
            return got
        qp = request.query_params
        keep = int(s.audit_keep_days or 90)
        ranges = [(n, f"{n} days" if n > 1 else "24 hours") for n in sorted({n for n in (1, 7, 30) if n < keep} | {keep})]
        try:
            days_ = int(qp.get("days", 7))
        except ValueError:
            days_ = 7
        days_ = days_ if days_ in dict(ranges) else min(7, keep)
        which = qp.get("action") or qp.get("filter") or "all"
        which = which if which in actions else "all"
        since = time.time() - days_ * 86400
        found = await asyncio.to_thread(audit.entries, 1000, actions[which][1], since)
        chain = await asyncio.to_thread(audit.verify)
        seen = {}
        for e in found:
            seen.setdefault(str(e.get("actor") or ""), actor_label(str(e.get("actor") or "")))
        who = qp.get("actor", "all")
        who = who if who in seen else "all"
        entries = [e for e in found if who == "all" or str(e.get("actor") or "") == who]
        today_ = datetime.now(d.tz).date()
        days: dict[Any, list[dict[str, Any]]] = {}
        for e in entries:
            days.setdefault(datetime.fromtimestamp(e.get("t", 0), d.tz).date(), []).append(e)
        out = []
        for day, items in days.items():
            label = "Today" if day == today_ else "Yesterday" if day == today_ - timedelta(days=1) else day.strftime("%a %d %b %Y")
            out.append({"day": label, "entries": [row(e) for e in items]})
        return ok({"filter": which, "filters": [[k, label] for k, (label, _) in actions.items()], "days": out,
                   "range": days_, "ranges": [[n, label] for n, label in ranges],
                   "actor": who, "actors": [["all", "Everyone"]] + sorted(([k, v] for k, v in seen.items()), key=lambda kv: (kv[0] != "owner", kv[1].lower())),
                   "more": len(found) >= 1000, "log_on": bool(s.audit_log), "keep_days": s.audit_keep_days,
                   "chain": {"ok": chain["ok"], "entries": chain.get("entries", 0), "broken_at": chain.get("broken_at")}})

    # -- security ------------------------------------------------------------------------------------------------------
    @route("/security", ["GET"])
    async def security(request: Request) -> Response:
        got = reader(request)
        if isinstance(got, Response):
            return got
        rec = got[1]
        return ok({"passkeys": [{"id": k.id, "name": k.name, "created_at": k.created_at, "last_used_at": k.last_used_at}
                                for k in d.passkeys.list()],
                   "passkeys_available": P.available(),
                   "apps": [{"id": a["id"], "name": a["name"], "host": a["host"], "last_used": a["last_used"]} for a in provider.connected_apps()],
                   "password_login": d.password_login(), "via": via_text(rec), "signed_in_at": rec.get("created"), "recent": recent(rec)})

    @route("/security/passkeys/{kid}/remove", ["POST"])
    async def passkey_remove(request: Request) -> Response:
        got = await writer(request)
        if isinstance(got, Response):
            return got
        rec = got[1]
        if not recent(rec):
            return need_confirm()
        if len(d.passkeys.list()) == 1 and not s.owner_password_login:
            return err("This is your only passkey and password sign-in is off. Add another passkey first.", 409)
        key = d.passkeys.remove(request.path_params["kid"])
        if key:
            audit.record("passkey.removed", actor="owner", via=rec.get("via", ""), summary=f"Removed passkey “{key.name}”")
        return ok({"note": f"Removed “{key.name}”." if key else "That passkey was already gone."})

    @route("/security/apps/{cid}/sign-out", ["POST"])
    async def app_sign_out(request: Request) -> Response:
        got = await writer(request)
        if isinstance(got, Response):
            return got
        rec = got[1]
        if not recent(rec):
            return need_confirm()
        cid = request.path_params["cid"]
        name = next((a["name"] for a in provider.connected_apps() if a["id"] == cid), "an app")
        if not provider.sign_out(cid):
            return err("That app was not signed in.", 404)
        audit.record("app.signed_out", actor="owner", via=rec.get("via", ""), summary=f"Signed out {name}")
        return ok({"note": f"Signed out {name}."})

    @route("/security/apps/sign-out-all", ["POST"])
    async def app_sign_out_all(request: Request) -> Response:
        got = await writer(request)
        if isinstance(got, Response):
            return got
        rec = got[1]
        if not recent(rec):
            return need_confirm()
        n = provider.sign_out_all()
        audit.record("app.signed_out_all", actor="owner", via=rec.get("via", ""), summary=f"Signed out all apps ({n})")
        return ok({"note": f"Signed out {n} app{'s' if n != 1 else ''}."})

    @route("/security/password", ["POST"])
    async def change_password(request: Request) -> Response:
        got = await writer(request)
        if isinstance(got, Response):
            return got
        _, rec, body = got
        if not recent(rec):
            return need_confirm()
        value = str(body.get("password", ""))
        if problem := owner_password_problem(value, s.bridge_token):
            return err(problem, 400)
        from .admin import save_override
        save_override(s.data_dir, "owner_password", value)
        log.warning("Owner password changed from the dashboard; it applies after a restart")
        audit.record("owner_password.changed", actor="owner", via=rec.get("via", ""), summary="Changed the owner password")
        return ok({"note": "Saved. The new password applies after the server restarts."})

    # -- server --------------------------------------------------------------------------------------------------------
    def bridge_state() -> dict[str, Any]:
        """Whether the Mac helper is connected right now (the bridge sees its polls), and its machine's name if it said one."""
        bridge = getattr(mcp, "_icloud_bridge", None)
        if not s.bridge_token or bridge is None:
            return {"set_up": False}
        st = bridge.status()
        ago = st.get("last_seen_seconds_ago")
        return {"set_up": True, "online": bool(st.get("online")), "last_seen_at": None if ago is None else time.time() - ago,
                "name": str((st.get("helper") or {}).get("host") or (st.get("helper") or {}).get("name") or "")}

    @route("/server", ["GET"])
    async def server(request: Request) -> Response:
        got = reader(request)
        if isinstance(got, Response):
            return got
        health = d.last_health
        areas = []
        for a, r in ((health or {}).get("areas") or {}).items():
            if r.get("actions"):                     # an area that reports per action (the gateway connector): one line each
                areas += [{"area": x["name"], "level": x["level"], "ok": x["level"] != "problem", "ms": 0, "error": x["text"][:160],
                           "note": {"problem": "Bypassable", "warning": "Not checked"}.get(x["level"], "Fine")} for x in r["actions"]]
                continue
            areas.append({"area": a.replace("_", " ").capitalize(), "ok": bool(r.get("ok")), "level": "ok" if r.get("ok") else "problem",
                          "ms": r.get("ms", 0), "error": "" if r.get("ok") else (r.get("error") or "")[:160]})
        areas.sort(key=lambda x: {"problem": 0, "warning": 1}.get(x["level"], 2))     # problems first, then warnings (H2)
        return ok({"version": version(), "paused": d.paused(), "checked_at": (health or {}).get("_at"), "areas": areas,
                   "tools": len(mcp._tool_manager.list_tools()), "apps": [a["name"] for a in provider.connected_apps()],
                   "bridge": bridge_state(), "host": d.host})

    @route("/server/check", ["POST"])
    async def check(request: Request) -> Response:
        got = await writer(request)
        if isinstance(got, Response):
            return got
        fn = getattr(mcp, "_icloud_health", None)
        if fn is not None:
            try:
                r = await asyncio.wait_for(asyncio.to_thread(fn), timeout=45)
            except asyncio.TimeoutError:
                r = {"ok": False, "areas": {"check": {"ok": False, "error": "took longer than 45 s"}}}
            d.last_health = {**r, "_at": time.time()}
        return ok({"note": "Checked."})

    @route("/server/pause", ["POST"])
    async def pause(request: Request) -> Response:
        got = await writer(request)
        if isinstance(got, Response):
            return got
        _, rec, body = got
        want = body.get("paused") is True
        from .admin import _write_private
        if want:
            _write_private(d.pause_file, time.strftime("%Y-%m-%dT%H:%M:%S") + "\n")
        elif os.path.exists(d.pause_file):
            os.unlink(d.pause_file)
        log.warning("Owner %s the server from the dashboard", "paused" if want else "resumed")
        audit.record("server.paused" if want else "server.resumed", actor="owner", via=rec.get("via", ""),
                     summary="Paused all tools" if want else "Resumed all tools")
        return ok({"paused": want, "note": "Paused. Tools now answer that the server is paused." if want else "Resumed."})

    # -- notifications (push.py) ---------------------------------------------------------------------------------------
    @route("/push", ["GET"])
    async def push_state(request: Request) -> Response:
        got = reader(request)
        if isinstance(got, Response):
            return got
        return ok({"public_key": d.push.public_key, "devices": d.push.devices(), "https": s.public_url.startswith("https://")})

    @route("/push/subscribe", ["POST"])
    async def push_subscribe(request: Request) -> Response:
        got = await writer(request)
        if isinstance(got, Response):
            return got
        _, rec, body = got
        sub = body.get("subscription") if isinstance(body.get("subscription"), dict) else {}
        label = str(body.get("label", "") or "This device")
        try:
            added = await asyncio.to_thread(d.push.add, sub, label)
        except ValueError as e:
            return err(str(e), 400)
        audit.record("push.added", actor="owner", via=rec.get("via", ""), summary=f"Turned on notifications for {label}")
        return ok({**added, "note": "Notifications are on for this device."})

    @route("/push/{device}/remove", ["POST"])
    async def push_remove(request: Request) -> Response:
        got = await writer(request)
        if isinstance(got, Response):
            return got
        rec = got[1]
        label = next((x["label"] for x in d.push.devices() if x["id"] == request.path_params["device"]), "a device")
        if not await asyncio.to_thread(d.push.remove, request.path_params["device"]):
            return err("That device does not get notifications.", 404)
        audit.record("push.removed", actor="owner", via=rec.get("via", ""), summary=f"Turned off notifications for {label}")
        return ok({"note": f"No more notifications for {label}."})

    @route("/push/test", ["POST"])
    async def push_test(request: Request) -> Response:
        got = await writer(request)
        if isinstance(got, Response):
            return got
        counts = await asyncio.to_thread(d.push.send, "test", "", str(got[2].get("device", "")))
        if not any(counts.values()):
            return err("No device gets notifications yet.", 404)
        if counts["ok"]:
            return ok({"note": "Sent. It should arrive in a few seconds.", **counts})
        return err("The push service did not take it." + (" The device was removed; turn notifications on again." if counts["gone"] else ""), 502, **counts)

    d.api_writer, d.api_reader, d.api_ok, d.api_err, d.api_need_confirm = writer, reader, ok, err, need_confirm
    d.bridge_state = bridge_state
