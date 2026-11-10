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

log = logging.getLogger(__name__)

# What the preview shows inline. Everything else is offered by name and size only: the browser never runs or renders it.
_PREVIEW_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp", "image/heic", "application/pdf", "text/plain")


def register_api(mcp: Any, d: Any, provider: Any, s: Any, audit: Any, *, signed_in: Callable, recent: Callable,
                 set_cookie: Callable, record_failure_line: Callable, headers: dict[str, str], filters: dict[str, Any],
                 for_owner: Callable[[str], str],
                 owner_password_problem: Callable[[str, str], str]) -> None:
    from . import passkeys as P

    def ok(data: Any = None, status: int = 200) -> JSONResponse:
        return JSONResponse({"ok": True, **(data or {})}, status_code=status, headers=headers)

    def err(error: str, status: int, **extra: Any) -> JSONResponse:
        return JSONResponse({"ok": False, "error": error, **extra}, status_code=status, headers=headers)

    def need_confirm() -> JSONResponse:
        return err("confirm", 403, confirm=True, message="Confirm it is you: this step changes how your server is protected.")

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

    def entry(e: dict[str, Any]) -> dict[str, Any]:
        """'You' / 'Someone' / the app's name, and what it did, starting lower-case: 'signed in with a passkey'."""
        actor = e.get("actor") or "?"
        text = str(e.get("summary") or e.get("action", ""))
        return {"t": e.get("t"), "time": d.fmt_time(e.get("t")), "who": "You" if actor == "owner" else ("Someone" if actor == "unknown" else actor),
                "what": text[:1].lower() + text[1:], "ok": e.get("ok", True), "error": (e.get("details") or {}).get("error", "")}

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
                "what": (m.group(1)[:1].upper() + m.group(1)[1:]) if m else "", "to": short_names(", ".join(map(str, rcpt[:1])))}

    def label_of(src: Any, q: Any) -> str:
        return src.approve_label_for(q) if hasattr(src, "approve_label_for") else src.approve_label

    def to_of(v: dict[str, Any]) -> str:
        return next((val for k, val in v["rows"] if k in ("To", "Will be delivered to", "Guests", "Conversation")), "")

    def summary(src: Any, q: Any, v: dict[str, Any]) -> dict[str, Any]:
        return {"key": f"{src.kind}:{q.id}", "kind": src.kind, "badge": v["badge"], "title": v["title"], "to": short_names(to_of(v)) if src.kind != "calendar" else to_of(v),
                "by": getattr(q, "requested_by", None) or "", "created_at": q.created_at, "expires_at": q.expires_at,
                "warning": bool(v["warnings"] or v["needs_override"]), "blocked": bool(v.get("blocked"))}

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
        return ok({"waiting": [summary(*w) for w in waiting], "v": fingerprint, "paused": d.paused(),
                   "sent_today": [sent_item(e) for e in sent[:10]],
                   "recent_burst": sum(time.time() - q.created_at < 3600 for _, q, _ in waiting)})

    @route("/state", ["GET"])
    async def state(request: Request) -> Response:
        got = reader(request)
        if isinstance(got, Response):
            return got
        n, fingerprint = await asyncio.to_thread(d.fingerprint)
        return ok({"waiting": n, "v": fingerprint, "paused": d.paused()})

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
               "approve_label": label_of(src, q), "files": [], "missing": [], "original": None}
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
        src = d.sources.get("mail")
        q = next((x for x in src.pending() if x.id == item_id), None) if src and kind == "mail" else None
        if q is None:
            return err("This is no longer waiting.", 404)
        from .mail import iter_attachment_parts
        try:
            index = int(request.path_params["index"])
        except ValueError:
            return err("No such file.", 404)
        parts = list(iter_attachment_parts(mail_parts(q)))
        if not 0 <= index < len(parts):
            return err("No such file.", 404)
        part = parts[index]
        ctype = part.get_content_type()
        if ctype not in _PREVIEW_TYPES:
            return err("This kind of file is not shown here.", 415)
        data = part.get_payload(decode=True) or b""
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
            audit.record("approval.discarded", **who, summary=f"Discarded {v['badge'].lower()} “{v['title']}”", kind=src.kind, outbox_id=q.id)
            return ok({"note": "Discarded. Nothing was sent."})
        if v["needs_override"] and body.get("override") is not True:
            return err("Not sent: confirm that you still want it sent despite the warning.", 422)
        if v.get("blocked"):
            return err("Not sent: a file is still missing. Add it first.", 409)
        try:
            note = await asyncio.to_thread(src.release, q)
        except Exception as e:  # noqa: BLE001  -- release() puts the item back; the owner can retry or discard
            log.warning("Release of %s %s failed: %s", src.kind, q.id, e)
            audit.record("approval.failed", **who, ok=False, summary=f"Could not send {v['badge'].lower()} “{v['title']}”", kind=src.kind, outbox_id=q.id)
            return err(f"Not sent (still waiting): {for_owner(str(e))}", 502)
        audit.record("approval.sent", **who, summary=f"Approved {v['badge'].lower()} “{v['title']}”", kind=src.kind,
                     outbox_id=q.id, delivery=getattr(note, "delivery", None),
                     recipients=[val for k, val in v["rows"] if k in ("To", "Guests", "Will be delivered to")])
        return ok({"note": note})

    # -- activity ------------------------------------------------------------------------------------------------------
    @route("/activity", ["GET"])
    async def activity(request: Request) -> Response:
        got = reader(request)
        if isinstance(got, Response):
            return got
        which = request.query_params.get("filter", "all")
        which = which if which in filters else "all"
        entries = await asyncio.to_thread(audit.entries, 300, filters[which][1])
        chain = await asyncio.to_thread(audit.verify)
        today_ = datetime.now(d.tz).date()
        days: dict[Any, list[dict[str, Any]]] = {}
        for e in entries:
            days.setdefault(datetime.fromtimestamp(e.get("t", 0), d.tz).date(), []).append(e)
        out = []
        for day, items in days.items():
            label = "Today" if day == today_ else "Yesterday" if day == today_ - timedelta(days=1) else day.strftime("%a %d %b %Y")
            out.append({"day": label, "entries": [entry(e) for e in items]})
        return ok({"filter": which, "filters": [[k, label] for k, (label, _) in filters.items()], "days": out,
                   "log_on": bool(s.audit_log), "keep_days": s.audit_keep_days,
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
        areas = [{"area": a.replace("_", " ").capitalize(), "ok": bool(r.get("ok")), "ms": r.get("ms", 0),
                  "error": "" if r.get("ok") else (r.get("error") or "")[:160]} for a, r in ((health or {}).get("areas") or {}).items()]
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
