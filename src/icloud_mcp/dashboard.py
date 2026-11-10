"""The owner's dashboard: one place in the browser for approvals, activity, connected apps, passkeys and the server's state.

It replaces typing the owner password on every visit to /outbox with a signed-in session. Sign-in is a passkey (WebAuthn,
see passkeys.py) or the owner password; the owner can turn the password off once a passkey exists. Steps that weaken
protection (removing a passkey, changing the password, signing apps out) ask again when the last sign-in is more than five
minutes old.

Sessions: a random token in a cookie (`HttpOnly`, `Secure`, `SameSite=Strict`, `__Host-` prefix on https), stored only as a
SHA-256 hash in DATA_DIR/sessions.json (mode 600), so a restart keeps you signed in and a copy of the file signs nobody in.
They end after DASHBOARD_SESSION_MINUTES without use (30) and after 12 hours in any case. Every form carries a per-session
CSRF token and every POST must come from this server's own origin.

Pages are rendered on the server and work without scripts. Three scripts are served from here (CSP `script-src 'self'`):
/dashboard/passkey.js calls the browser's passkey API; /dashboard/dashboard.js keeps the pages current (it asks
/dashboard/state.json what is waiting and, on Overview and Approvals, loads the page's own address again when that
changes), sends and discards without reloading the page (a discard can be undone for 5 seconds before it is sent to the
server), and registers a service worker so the dashboard can be installed as an app; /dashboard/install-hint.js shows
the Overview's tip on how to install it. The worker caches nothing: every page comes from the server.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import passkeys as P
from .approvals import flagged
from .audit import AuditLog
from . import textedit as T
from .config import Settings, owner_password_problem
from .mail import MailError

log = logging.getLogger(__name__)

SESSIONS_FILE = "sessions.json"
_ABSOLUTE = 12 * 3600          # a session never lasts longer than this
_STEP_UP = 300                 # seconds a sign-in counts as recent for sensitive steps
_BODY_PREVIEW = 20000
_STATIC = Path(__file__).parent / "static"

_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "same-origin",       # browsers then send our own Origin on form posts (no-referrer makes it "null")
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": ("default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; img-src 'self' data:; "
                                "manifest-src 'self'; worker-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"),
}
_LIVE = "x-live"               # header of the page's own background refresh; it does not count as using the dashboard


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------------------------------------------------
class Sessions:
    def __init__(self, data_dir: str, idle_minutes: int):
        self.path = Path(data_dir) / SESSIONS_FILE
        self.idle = max(5, idle_minutes) * 60
        self._lock = threading.Lock()
        self._items: dict[str, dict[str, Any]] = {}
        try:
            data = json.loads(self.path.read_text())
            self._items = {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            pass
        self._prune()

    def _prune(self) -> None:
        now = time.time()
        self._items = {k: v for k, v in self._items.items()
                       if now - v.get("last", 0) < self.idle and now - v.get("created", 0) < _ABSOLUTE}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".sessions-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._items, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def create(self, via: str) -> tuple[str, dict[str, Any]]:
        token = secrets.token_urlsafe(32)
        now = time.time()
        rec = {"created": now, "last": now, "auth_at": now, "via": via, "csrf": secrets.token_urlsafe(24)}
        with self._lock:
            self._prune()
            self._items[_h(token)] = rec
            self._save()
        return token, rec

    def get(self, token: str, touch: bool = True) -> dict[str, Any] | None:
        """touch=False looks a session up without counting it as use, so a page left open does not stay signed in."""
        if not token:
            return None
        with self._lock:
            self._prune()
            rec = self._items.get(_h(token))
            if rec is None:
                return None
            if touch and time.time() - rec["last"] > 60:          # write at most once a minute while browsing
                rec["last"] = time.time()
                self._save()
            return rec

    def reauth(self, token: str, via: str) -> str | None:
        """Confirming who is there issues a new session token; the old one stops working. The CSRF value stays, because a
        change held for confirmation (settings.py) is filed under it."""
        with self._lock:
            rec = self._items.pop(_h(token), None)
            if rec is None:
                return None
            rec["auth_at"], rec["via"] = time.time(), via
            fresh = secrets.token_urlsafe(32)
            self._items[_h(fresh)] = rec
            self._save()
            return fresh

    def drop(self, token: str) -> None:
        with self._lock:
            if self._items.pop(_h(token), None) is not None:
                self._save()

    def drop_all(self) -> None:
        with self._lock:
            self._items.clear()
            self._save()


# ---------------------------------------------------------------------------------------------------------------------
# Approval sources: one interface for queued mail, iMessages and (with ALLOW_CALENDAR_INVITES=approve) calendar changes
# ---------------------------------------------------------------------------------------------------------------------
class ApprovalSource:
    kind = ""
    badge = ""
    approve_label = "Approve"

    def pending(self) -> list[Any]:
        raise NotImplementedError

    def describe(self, q: Any, context: Any = None) -> dict[str, Any]:
        """{'title', 'badge', 'rows': [(label, value)], 'text', 'notes': [str], 'warnings': [str], 'needs_override': bool}"""
        raise NotImplementedError

    def contexts(self, items: list[Any], prune: bool = True) -> dict[str, Any]:
        """prune=True only with the whole queue: it forgets what is kept for requests not in items."""
        return {}

    def release(self, q: Any) -> str:
        """Do it; return a one-line result for the page. Raises on failure (and puts the item back)."""
        raise NotImplementedError

    def discard(self, q: Any) -> None:
        raise NotImplementedError

    # Editing the text before approving (textedit.py). A source that has no text of its own keeps these answers.
    text_limit = 10000

    def text_block(self, q: Any) -> str:
        """Why the text cannot be edited, or "" when it can."""
        return "This kind of request has no text to edit."

    def text_of(self, q: Any) -> str:
        raise T.TextLocked(self.text_block(q))

    def clean_text(self, text: Any) -> str:
        return T.clean(text, allow_empty=False, limit=self.text_limit)

    def with_text(self, q: Any, text: str) -> tuple[bytes, dict[str, Any] | None]:
        """The stored message with this text, and the changed followup (None when it stays)."""
        raise T.TextLocked(self.text_block(q))


class MailApprovals(ApprovalSource):
    kind, badge, approve_label = "mail", "Email", "Send"

    def __init__(self, mail: Any):
        self.mail = mail

    def pending(self):
        return self.mail.outbox.pending()

    def contexts(self, items, prune=True):
        return self.mail.approval_contexts(items, prune=prune) if items else {}

    def describe(self, q, context=None):
        d = self.mail.describe_queued(q, context)
        body = d["body"] if len(d["body"]) <= _BODY_PREVIEW else d["body"][:_BODY_PREVIEW] + "\n\n[... preview truncated; the full message is sent]"
        rows = [("From", d["from"]), ("To", d["to"]), ("Cc", d["cc"]), ("Bcc", d["bcc"]),
                ("Will be delivered to", ", ".join(d["envelope_recipients"]))]
        warnings = list((d.get("original") or {}).get("safety_warnings") or [])
        if d.get("agent_added_recipients"):
            warnings.append("A recipient's address was added to a contact by an agent, not by you: "
                            + ", ".join(d["agent_added_recipients"]))
        notes = []
        if orig := d.get("original"):
            frm = ", ".join(f"{a.get('name', '')} <{a.get('email', '')}>".strip() for a in orig.get("from") or [] if isinstance(a, dict))
            notes.append("In reply to: " + ("(the original message could not be read)" if orig.get("unavailable")
                                            else f"{orig.get('subject') or '(no subject)'} from {frm or '?'}"))
        if d["attachments"]:
            notes.append("Attachments: " + ", ".join(f"{a.get('filename')} ({a.get('size')} bytes)" for a in d["attachments"]))
        waiting = [w["filename"] for w in d.get("pending_files") or []]
        if waiting:
            notes.append("Waiting for: " + ", ".join(waiting))
        return {"title": d["subject"] or "(no subject)", "badge": "Email reply" if d.get("original") else "Email",
                "rows": [(k, v) for k, v in rows if v], "text": body, "notes": notes, "warnings": warnings,
                "needs_override": bool(flagged(d)), **({"blocked": "Add the file first", "files_link": f"/dashboard#/item/mail:{q.id}"} if waiting else {})}

    def release(self, q):
        r = self.mail.release(q.id)
        return f"Sent to {', '.join(r.get('recipients', []))}." + (f" {r['warning']}" if r.get("warning") else "")

    def discard(self, q):
        self.mail.outbox.claim(q.id)

    @property
    def text_limit(self) -> int:
        return self.mail.s.max_body_chars

    def text_block(self, q):
        return T.mail_block(q.raw)

    def text_of(self, q):
        return T.mail_text(q.raw)

    def with_text(self, q, text):
        return T.mail_with_text(q.raw, text), None


_AGENT_HINT = "Run icloud_check_health to see which service is failing."


def for_owner(message: str) -> str:
    """Service errors are written for agents. On the dashboard the owner has a button for the same check."""
    return message.replace(_AGENT_HINT, "Check now under Settings › Server shows which service is failing.")


def event_when(start: str | None, end: str | None, tz_name: str) -> str:
    """'Fri 20 Nov 2026, 19:00–21:00' in the server's time zone, from the ISO times stored with a held change. All-day events
    show the date only; anything unparsable is shown as stored."""
    def parse(v: str | None) -> datetime | None:
        try:
            return datetime.fromisoformat(v) if v else None
        except ValueError:
            return None
    a, b = parse(start), parse(end)
    if a is None:
        return " – ".join(x for x in (start, end) if x)
    if len(start or "") == 10:                                   # an all-day event: a date, no time
        last = b - timedelta(days=1) if b and len(end or "") == 10 else None   # DTEND of an all-day event is exclusive
        day = a.strftime("%a %d %b %Y")
        return day if last is None or last <= a else f"{day} – {last.strftime('%a %d %b %Y')}"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = None
    if tz is not None and a.tzinfo is not None:
        a, b = a.astimezone(tz), (b.astimezone(tz) if b and b.tzinfo else b)
    out = a.strftime("%a %d %b %Y, %H:%M")
    if b is None:
        return out
    return out + ("–" + b.strftime("%H:%M") if b.date() == a.date() else " – " + b.strftime("%a %d %b %Y, %H:%M"))


class CalendarApprovals(ApprovalSource):
    """Calendar changes that make iCloud email other people (ALLOW_CALENDAR_INVITES=approve)."""
    kind, badge = "calendar", "Calendar"
    _LABELS = {"create": ("Invitation", "Add and invite"), "update": ("Event update", "Save and notify"),
               "delete": ("Cancellation", "Delete and notify"), "cancel": ("Cancellation", "Cancel and notify"),
               "reply": ("Reply", "Send answer")}

    def __init__(self, cal: Any):
        self.cal = cal

    def pending(self):
        return self.cal.outbox.pending()

    def describe(self, q, context=None):
        m = q.followup or {}
        badge, _ = self._LABELS.get(m.get("op"), ("Calendar", "Approve"))
        when = event_when(m.get("start"), m.get("end"), self.cal.s.default_timezone)
        rows = [("When", when), ("Calendar", m.get("calendar") or ""), ("Where", m.get("location") or "")]
        if m.get("op") == "reply":
            rows.append(("To", m.get("organizer") or ""))
        else:
            rows.append(("Guests", ", ".join(q.recipients)))
        effect = {"create": "Approving adds the event to your calendar, and iCloud emails an invitation to",
                  "update": "Approving saves the change, and iCloud emails an update to",
                  "delete": "Approving deletes the event, and iCloud emails a cancellation to",
                  "cancel": "Approving cancels this occurrence, and iCloud emails a cancellation to",
                  "reply": "Approving sends your answer to"}.get(m.get("op"), "Approving emails")
        who = m.get("organizer") if m.get("op") == "reply" else f"{len(q.recipients)} people"
        notes = [f"{effect} {who}.",
                 "Nothing is in your calendar yet." if m.get("op") == "create" else
                 "If the event changes in your calendar before you approve, nothing is written."]
        return {"title": m.get("summary") or "(no title)", "badge": badge, "rows": [(k, v) for k, v in rows if v],
                "text": m.get("description") or "", "notes": notes, "warnings": [], "needs_override": False}

    def approve_label_for(self, q) -> str:
        return self._LABELS.get((q.followup or {}).get("op"), ("", "Approve"))[1]

    def release(self, q):
        claimed = self.cal.outbox.claim(q.id)
        if claimed is None:
            raise RuntimeError("It is no longer waiting.")
        try:
            return self.cal.release(claimed)
        except Exception:
            self.cal.outbox.restore(claimed)
            raise

    def discard(self, q):
        self.cal.outbox.claim(q.id)

    text_limit = T.CALENDAR_MAX_CHARS

    def text_block(self, q):
        return T.calendar_block(q.raw, q.followup)

    def text_of(self, q):
        return T.calendar_text(q.raw)

    def clean_text(self, text):
        return T.clean(text, allow_empty=True, limit=self.text_limit)      # an empty description is allowed

    def with_text(self, q, text):
        return T.calendar_with_text(q.raw, text), {**(q.followup or {}), "description": text[:4000]}


class Delivered(str):
    """The one-line result of an iMessage release, with what Messages reported (`sent`, `unconfirmed`, `failed`)."""
    delivery: str | None

    def __new__(cls, text: str, delivery: str | None = None):
        obj = super().__new__(cls, text)
        obj.delivery = delivery
        return obj


class IMessageApprovals(ApprovalSource):
    kind, badge, approve_label = "imessage", "iMessage", "Send"

    def __init__(self, imessage: Any, box: Any):
        self.imessage, self.box = imessage, box

    def pending(self):
        return self.box.pending()

    def describe(self, q, context=None):
        d = self.imessage.describe_queued(q)
        warnings = ["A group conversation: everyone in it receives this."] if d["group"] else []
        label = d["name"] or d["people"] or d["chat_id"] or ""      # a named chat by its name; the people stay in the card
        rows = ([("Chat with", d["people"])] if d["people"] else []) + ([("Conversation", d["chat_id"])] if d["group"] and d["chat_id"] else [])
        return {"title": f"iMessage to {label}", "label": label, "badge": "iMessage", "rows": rows, "text": d["text"],
                "notes": ["Sent from your Mac once you approve."], "warnings": warnings, "needs_override": False, "group": bool(d["group"])}

    def release(self, q):
        r = self.imessage.release(self.box, q.id)
        word = {"sent": "Sent", "failed": "Not delivered", "unconfirmed": "Handed to Messages (not yet confirmed)"}.get(r.get("status"), str(r.get("status")))
        return Delivered(f"{word}: iMessage to {r.get('to', '')}. {r.get('note', '')}".strip(), r.get("status"))

    def discard(self, q):
        self.box.claim(q.id)

    text_limit = T.IMESSAGE_MAX_CHARS

    def text_block(self, q):
        return ""

    def text_of(self, q):
        return T.imessage_text(q.raw)

    def clean_text(self, text):
        return T.clean(text, allow_empty=False, limit=self.text_limit, strip=True)    # as imessage_send_message does

    def with_text(self, q, text):
        return T.imessage_with_text(q.raw, text), None


# ---------------------------------------------------------------------------------------------------------------------
# Page frame and components
#
# Every page follows the same structure, so nothing floats:
#   top bar and tab bar (phones), or from 860 px a tinted frame: the sidebar and a bar above the page with this server's
#   address, its state and Sign out, around one white sheet that holds the page
#   page head: title, one line of context (the finding that matters most) and at most the page's one primary action
#   cards: a head (title, at most one status chip, never a button), a body (rows, text, fields), a foot (all of the
#   card's buttons, right-aligned, the main action last; full width on phones)
#   row actions (remove a passkey, sign out an app) sit at the right end of their row, all the same size
# ---------------------------------------------------------------------------------------------------------------------
_CSS = (_STATIC / "dashboard.css").read_text()
_ASSETS = {     # served under /dashboard/ to anyone (the manifest and the worker are fetched without the session cookie)
    "dashboard.css": "text/css", "install-hint.js": "text/javascript",      # dashboard.css styles the connect page (/login)
    "manifest.webmanifest": "application/manifest+json", "sw.js": "text/javascript",
}

# The icons travel in the page and in the manifest, not as requests of their own. iOS fetches /apple-touch-icon.png and
# browsers fetch the favicon and the manifest's icons without the session cookie, so behind a sign-in proxy (Cloudflare
# Access, an SSO gateway, a Tailscale serve) they are answered with the proxy's login page and the phone falls back to a
# grey letter. Costs ~55 KB a page (the manifest is cached by its version); the routes stay for everyone else. Replacing
# the files in static/ is still all it takes to use your own artwork.
def _data_uri(name: str, ctype: str, fallback: str) -> str:
    f = _STATIC / name
    return f"data:{ctype};base64,{base64.b64encode(f.read_bytes()).decode()}" if f.is_file() else fallback


_APPLE_ICON = _data_uri("icon-180.png", "image/png", "/apple-touch-icon.png")
_FAVICON = _data_uri("favicon.ico", "image/x-icon", "/favicon.ico")


def _manifest() -> bytes:
    m = json.loads((_STATIC / "manifest.webmanifest").read_text())
    for icon in m.get("icons", []):
        src = icon.get("src", "")
        if src.startswith("/") and "/" not in src[1:]:          # one of ours, e.g. /icon-192.png
            icon["src"] = _data_uri(src[1:], icon.get("type") or "image/png", src)
    return json.dumps(m, indent=2).encode()


_MANIFEST = _manifest()
_ASSET_VERSION = hashlib.sha256(b"".join(_MANIFEST if name == "manifest.webmanifest" else (_STATIC / name).read_bytes()
                                         for name in sorted(_ASSETS))).hexdigest()[:10]

_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{}</svg>'
_ICONS = {
    "overview": '<rect x="3.5" y="3.5" width="7" height="7" rx="1.5"></rect><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"></rect><rect x="3.5" y="13.5" width="7" height="7" rx="1.5"></rect><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"></rect>',
    "approvals": '<path d="M4 13l2.2-8h11.6L20 13v6H4z"></path><path d="M4 13h5l1 2h4l1-2h5"></path>',
    "activity": '<circle cx="12" cy="12" r="8.5"></circle><path d="M12 7.5V12l3 2"></path>',
    "settings": '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"></path><circle cx="15" cy="7" r="2"></circle><circle cx="9" cy="17" r="2"></circle>',
    "security": '<path d="M12 3l8 3v6c0 4.8-3.4 8-8 9-4.6-1-8-4.2-8-9V6z"></path>',
    "key": '<circle cx="8" cy="15" r="4"></circle><path d="M11 12l9-9M17 6l3 3M14 9l2 2"></path>',
}


def _esc(v: Any) -> str:
    return html.escape("" if v is None else str(v))


class Dashboard:
    def __init__(self, mcp: Any, provider: Any, s: Settings, audit: AuditLog, sources: list[ApprovalSource],
                 exit_fn: Callable[[], None] | None = None):
        self.mcp, self.provider, self.s, self.audit = mcp, provider, s, audit
        self.sources = {src.kind: src for src in sources}
        self.sessions = Sessions(s.data_dir, s.dashboard_session_minutes)
        self.passkeys = P.PasskeyStore(s.data_dir, s.public_url)
        self.exit_fn = exit_fn or (lambda: os._exit(0))
        self.secure = s.public_url.startswith("https://")
        self.cookie = "__Host-icloud-dashboard" if self.secure else "icloud-dashboard"
        self.origin = "{0.scheme}://{0.netloc}".format(urlsplit(s.public_url))
        self.host = urlsplit(s.public_url).netloc
        self.tz = self._tz()
        self.last_health: dict[str, Any] | None = None
        self.pause_file = os.path.join(s.data_dir, "paused")
        self.extra_nav: list[tuple[str, str, str]] = []        # (key, label, path) added by other parts (settings)
        self.flush_failure_lines: Callable[..., None] = lambda now=None: None   # set by register_dashboard
        self.has_icon = (_STATIC / "icon-32.png").is_file()

    def _tz(self):
        try:
            return ZoneInfo(self.s.default_timezone)
        except Exception:  # noqa: BLE001
            return ZoneInfo("UTC")

    # -- helpers ---------------------------------------------------------------------------
    def waiting(self) -> int:
        return sum(len(src.pending()) for src in self.sources.values())

    def fingerprint(self) -> tuple[int, str]:
        """How many requests wait, and a value that changes when any of them arrives, leaves or changes (state.json)."""
        keys = [f"{src.kind}:{q.id}:{q.sha256}" for src in self.sources.values() for q in src.pending()]
        return len(keys), hashlib.sha256("\n".join(sorted(keys) + [str(self.paused())]).encode()).hexdigest()[:16]

    def paused(self) -> bool:
        return os.path.exists(self.pause_file)

    def password_login(self) -> bool:
        """The owner password signs in unless the owner turned it off, and never when that would leave no way in."""
        return self.s.owner_password_login or not self.passkeys.list()

    def session(self, request: Request) -> tuple[str, dict[str, Any] | None]:
        token = request.cookies.get(self.cookie, "")
        return token, self.sessions.get(token, touch=request.headers.get(_LIVE) != "1")

    def same_origin(self, request: Request) -> bool:
        origin = request.headers.get("origin")
        if origin:
            return origin == self.origin
        ref = request.headers.get("referer")
        return bool(ref) and ref.startswith(self.origin + "/")

    @staticmethod
    def time_left(q: Any) -> str:
        mins = max(1, round((q.expires_at - time.time()) / 60))
        if mins >= 90:
            return f"{round(mins / 60)} hours"
        if mins >= 60:
            return "1 hour"
        return "1 minute" if mins == 1 else f"{mins} minutes"

    def fmt_time(self, t: float | None, with_day: bool = False) -> str:
        if not t:
            return "never"
        d = datetime.fromtimestamp(t, self.tz)
        return d.strftime("%d %b, %H:%M") if with_day else d.strftime("%H:%M")

    def ago(self, t: float | None) -> str:
        if not t:
            return "never"
        s = max(0, time.time() - t)
        for unit, n in (("day", 86400), ("hour", 3600), ("min", 60)):
            if s >= n:
                v = int(s // n)
                return f"{v} {unit}{'s' if v != 1 and unit != 'min' else ''} ago"
        return "just now"


def register_dashboard(mcp: Any, provider: Any, s: Settings, audit: AuditLog, sources: list[ApprovalSource],
                       exit_fn: Callable[[], None] | None = None) -> Dashboard:
    d = Dashboard(mcp, provider, s, audit, sources, exit_fn)
    js = (_STATIC / "passkey.js").read_bytes()
    host = (urlsplit(s.public_url).hostname or "").lower()
    if not d.secure and host not in ("localhost", "127.0.0.1", "::1") and not host.endswith(".localhost"):
        log.warning("MCP_PUBLIC_URL is plain http on %s: the dashboard cookie cannot be Secure or __Host-. Use https.", host)
    failures: dict[str, list[float]] = {}               # action -> [window start, recorded, not recorded]
    window = 600

    def flush_failure_lines(now: float | None = None) -> None:
        """Close every window that has ended: one line counts what was not recorded, also when the flood has stopped."""
        now = time.time() if now is None else now
        for action, (start, _written, dropped) in list(failures.items()):
            if now - start >= window:
                if dropped:
                    audit.record(action, actor="unknown", ok=False,
                                 summary=f"{int(dropped)} more like this in {window // 60} minutes were not recorded")
                del failures[action]

    def record_failure_line(action: str, summary: str, **details: Any) -> None:
        """Failed sign-ins from anyone: at most 5 lines per kind and 10 minutes, then one line that counts the rest."""
        now = time.time()
        flush_failure_lines(now)
        start, written, dropped = failures.get(action, [now, 0, 0])
        if written < 5:
            audit.record(action, actor="unknown", ok=False, summary=summary, **details)
            written += 1
        else:
            if not dropped:                                  # the first one held back: close the window on time
                try:
                    asyncio.get_running_loop().call_later(max(0.0, start + window - now) + 1, flush_failure_lines)
                except RuntimeError:                         # no event loop (called outside a request): the next call flushes
                    pass
            dropped += 1
        failures[action] = [start, written, dropped]

    d.flush_failure_lines = flush_failure_lines

    # -- guards --------------------------------------------------------------------------------------------------------
    def signed_in(request: Request) -> tuple[str, dict[str, Any]] | None:
        token, rec = d.session(request)
        return (token, rec) if rec is not None else None

    def to_signin(_: Request) -> Response:
        return JSONResponse({"ok": False, "error": "signed out"}, status_code=401, headers=_HEADERS)

    async def checked_form(request: Request) -> tuple[str, dict[str, Any], Any] | Response:
        """A POST from a signed-in owner, from this origin, with the session's CSRF token."""
        got = signed_in(request)
        if got is None:
            return to_signin(request)
        if not d.same_origin(request):
            return JSONResponse({"ok": False, "error": "This request did not come from this page. Nothing was changed."}, status_code=403, headers=_HEADERS)
        form = await request.form()
        if not hmac.compare_digest(str(form.get("csrf", "")).encode(), got[1]["csrf"].encode()):
            return JSONResponse({"ok": False, "error": "This page has expired. Reload it and try again."}, status_code=403, headers=_HEADERS)
        return got[0], got[1], form

    def recent(rec: dict[str, Any]) -> bool:
        return time.time() - rec.get("auth_at", 0) < _STEP_UP

    def set_cookie(resp: Response, token: str) -> Response:
        resp.set_cookie(d.cookie, token, max_age=_ABSOLUTE, httponly=True, secure=d.secure, samesite="strict", path="/")
        return resp

    # -- static ----------------------------------------------------------------------------------------------------------
    @mcp.custom_route("/dashboard/passkey.js", methods=["GET"])
    async def passkey_js(_: Request) -> Response:
        return Response(js, media_type="text/javascript", headers={**_HEADERS, "Cache-Control": "public, max-age=3600"})

    def _asset_route(name: str, ctype: str) -> None:
        body = _MANIFEST if name == "manifest.webmanifest" else (_STATIC / name).read_bytes()
        # Pages link the others with ?v=<content hash>, so they can be kept a day; the worker is always checked for changes.
        headers = {**_HEADERS, "Cache-Control": "no-cache" if name == "sw.js" else "public, max-age=86400"}
        if name == "sw.js":
            headers["Service-Worker-Allowed"] = "/dashboard"        # its scope includes /dashboard itself, not only /dashboard/…

        @mcp.custom_route(f"/dashboard/{name}", methods=["GET"])
        async def serve_asset(_: Request) -> Response:
            return Response(body, media_type=ctype, headers=headers)

    for _name, _ctype in _ASSETS.items():
        _asset_route(_name, _ctype)

    # -- the app (static/app) and the libraries it loads (static/vendor) -------------------------------------------------
    app_dir, vendor_dir = _STATIC / "app", _STATIC / "vendor"
    app_v = hashlib.sha256(b"".join(f.read_bytes() for f in sorted(app_dir.glob("*")) if f.is_file())).hexdigest()[:10] \
        if app_dir.is_dir() else "0"
    types = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".html": "text/html"}
    app_csp = _HEADERS["Content-Security-Policy"] + "; frame-src 'self'"

    def static_file(base: Path, rel: str, cache: str) -> Response:
        """A file under base, by a relative path that cannot leave it; anything else is a 404."""
        try:
            path = (base / rel).resolve()
            path.relative_to(base.resolve())
        except (ValueError, OSError):
            return Response("Not found", status_code=404, headers=_HEADERS)
        if not path.is_file() or path.suffix not in types or path.name.startswith("."):
            return Response("Not found", status_code=404, headers=_HEADERS)
        return Response(path.read_bytes(), media_type=types[path.suffix], headers={**_HEADERS, "Cache-Control": cache})

    @mcp.custom_route("/dashboard/vendor/{rel:path}", methods=["GET"])
    async def vendor_file(request: Request) -> Response:
        # versioned paths (ionic/9.0.10/...), so a browser may keep them; the chunks Ionic loads lazily come from here too
        return static_file(vendor_dir, request.path_params["rel"], "public, max-age=31536000, immutable")

    @mcp.custom_route("/dashboard/app/{rel:path}", methods=["GET"])
    async def app_file(request: Request) -> Response:
        if request.path_params["rel"] == "index.html":
            return Response("Not found", status_code=404, headers=_HEADERS)
        return static_file(app_dir, request.path_params["rel"], "no-cache")   # modules import each other without a version

    def app_shell() -> Response:
        # The icons ride in the page: iOS fetches the home-screen icon without the session, and a sign-in proxy in front
        # (tailscale serve, Cloudflare Access) would answer that with its own page
        html_ = ((app_dir / "index.html").read_text().replace("{{v}}", app_v).replace("{{asset_v}}", _ASSET_VERSION)
                 .replace("{{apple_icon}}", _APPLE_ICON).replace("{{favicon}}", _FAVICON))
        return HTMLResponse(html_, headers={**_HEADERS, "Content-Security-Policy": app_csp})

    @mcp.custom_route("/dashboard", methods=["GET"])
    async def app_page(request: Request) -> Response:
        """The app loads for anyone (it holds no data); it asks /dashboard/api/session who is signed in. Behind tailscale
        serve the owner is signed in on the way, as on /dashboard/signin."""
        if signed_in(request) is None and access is not None and (found := await access.owner(request)):
            who, how = found
            token, _ = d.sessions.create(f"{how} ({who})")
            audit.record("signin.access", actor="owner", via=f"{how} ({who})", summary=f"Signed in through {how}")
            return set_cookie(app_shell(), token)
        return app_shell()

    @mcp.custom_route("/dashboard/app", methods=["GET"])
    async def app_old_address(_: Request) -> Response:
        return RedirectResponse("/dashboard", status_code=303, headers=_HEADERS)

    @mcp.custom_route("/dashboard/state.json", methods=["GET"])
    async def live_state(request: Request) -> Response:
        """What the page asks every few seconds: how many requests wait, and a fingerprint that changes when any of them
        arrives, leaves or changes. Asking does not keep the session alive (dashboard.js sends the x-live header)."""
        if signed_in(request) is None:
            return JSONResponse({"error": "signed out"}, status_code=401, headers=_HEADERS)
        n, fingerprint = await asyncio.to_thread(d.fingerprint)
        return JSONResponse({"waiting": n, "v": fingerprint}, headers=_HEADERS)

    # -- sign in: the app asks /dashboard/api; behind tailscale serve or Access the identity signs in on the way ----------
    from . import access_sso
    access = access_sso.from_settings(s)        # fork: behind Cloudflare Access or tailscale serve the owner's identity signs the dashboard in

    @mcp.custom_route("/dashboard/signin", methods=["GET"])
    async def signin_get(_: Request) -> Response:
        return RedirectResponse("/dashboard", status_code=303, headers=_HEADERS)

    # -- passkey JSON endpoints (passkey.js) ---------------------------------------------------------------------------
    async def json_body(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    @mcp.custom_route("/dashboard/passkey/options", methods=["POST"])
    async def passkey_options(request: Request) -> Response:
        if not d.same_origin(request):
            return JSONResponse({"error": "forbidden"}, status_code=403, headers=_HEADERS)
        purpose = str((await json_body(request)).get("purpose", ""))
        try:
            if purpose == "signin":
                # Not behind the password lock: a passkey cannot be guessed, and ten wrong passwords from anyone would lock the owner out
                return JSONResponse(d.passkeys.authentication_options(), headers=_HEADERS)

            if purpose == "register":
                got = signed_in(request)
                if got is None or not hmac.compare_digest(request.headers.get("x-csrf", "").encode(), got[1]["csrf"].encode()):
                    return JSONResponse({"error": "Sign in first."}, status_code=401, headers=_HEADERS)
                if not recent(got[1]):
                    return JSONResponse({"error": "confirm", "confirm": True}, status_code=403, headers=_HEADERS)
                return JSONResponse(d.passkeys.registration_options(), headers=_HEADERS)
        except P.PasskeyError as e:
            return JSONResponse({"error": str(e)}, status_code=400, headers=_HEADERS)
        return JSONResponse({"error": "unknown purpose"}, status_code=400, headers=_HEADERS)

    @mcp.custom_route("/dashboard/passkey/verify", methods=["POST"])
    async def passkey_verify(request: Request) -> Response:
        if not d.same_origin(request):
            return JSONResponse({"error": "forbidden"}, status_code=403, headers=_HEADERS)
        data = await json_body(request)
        purpose, ticket = str(data.get("purpose", "")), str(data.get("ticket", ""))
        credential = data.get("credential") if isinstance(data.get("credential"), dict) else {}
        nxt = "/dashboard"
        if purpose == "signin":
            try:
                key = await asyncio.to_thread(d.passkeys.authenticate, ticket, credential)
            except P.PasskeyError as e:
                # not counted towards the password lockout: junk passkey requests must not lock the owner's password out
                record_failure_line("signin.passkey", "Tried to sign in with a passkey that did not verify")
                return JSONResponse({"error": str(e)}, status_code=401, headers=_HEADERS)
            token, rec = d.session(request)
            if rec is not None:
                fresh = d.sessions.reauth(token, f"passkey “{key.name}”")
                audit.record("signin.confirm", actor="owner", via=f"passkey “{key.name}”", summary=f"Confirmed with passkey “{key.name}”")
                resp = JSONResponse({"next": nxt}, headers=_HEADERS)
                return set_cookie(resp, fresh) if fresh else resp
            token, rec = d.sessions.create(f"passkey “{key.name}”")
            audit.record("signin.passkey", actor="owner", via=f"passkey “{key.name}”", summary=f"Signed in with passkey “{key.name}”")
            return set_cookie(JSONResponse({"next": nxt}, headers=_HEADERS), token)
        if purpose == "register":
            got = signed_in(request)
            if got is None or not hmac.compare_digest(request.headers.get("x-csrf", "").encode(), got[1]["csrf"].encode()) or not recent(got[1]):
                return JSONResponse({"error": "Sign in again first."}, status_code=401, headers=_HEADERS)
            try:
                key = await asyncio.to_thread(d.passkeys.register, ticket, credential, str(data.get("name", "")))
            except P.PasskeyError as e:
                return JSONResponse({"error": str(e)}, status_code=400, headers=_HEADERS)
            audit.record("passkey.added", actor="owner", via=got[1].get("via", ""), summary=f"Added passkey “{key.name}”")
            return JSONResponse({"next": "/dashboard#/settings"}, headers=_HEADERS)
        return JSONResponse({"error": "unknown purpose"}, status_code=400, headers=_HEADERS)

    # -- the pages before the app: their addresses still lead to the same place ----------------------------------------
    def to_app(fragment: str = "") -> Response:
        return RedirectResponse("/dashboard" + fragment, status_code=303, headers=_HEADERS)

    def item_fragment(key: str) -> str:
        return f"#/item/{key}" if re.fullmatch(r"[a-z]+:[A-Za-z0-9_-]+", key or "") else ""

    @mcp.custom_route("/dashboard/approvals", methods=["GET"])
    async def approvals_get(request: Request) -> Response:
        return to_app(item_fragment(request.query_params.get("item", "")))

    @mcp.custom_route("/dashboard/activity", methods=["GET"])
    async def activity_get(_: Request) -> Response:
        return to_app("#/activity")

    @mcp.custom_route("/dashboard/security", methods=["GET"])
    async def security_get(_: Request) -> Response:
        return to_app("#/settings")

    # -- files a waiting mail needs ------------------------------------------------------------------------------------
    # An agent can queue a mail with a file it cannot pass itself (mail.py: placeholders). The owner adds it on the attach
    # page (the link in the agent's result) and approves there; an agent with a shell PUTs it to /files/<token>. That route
    # is outside /dashboard so a sign-in proxy in front can let it through: the token is its only key.
    mail_src = d.sources.get("mail")

    @mcp.custom_route("/dashboard/attach/{item_id}", methods=["GET"])
    async def attach_get(request: Request) -> Response:
        return to_app(item_fragment("mail:" + request.path_params["item_id"]))

    @mcp.custom_route("/dashboard/attach/{item_id}/file", methods=["POST"])
    async def attach_post(request: Request) -> Response:
        got = await checked_form(request)
        if isinstance(got, Response):
            return got
        _, rec, form = got
        item_id = request.path_params["item_id"]
        up = form.get("file")

        async def failed(note: str, code: int) -> Response:
            return JSONResponse({"ok": False, "error": note}, status_code=code, headers=_HEADERS)

        if mail_src is None or not hasattr(up, "read"):
            return await failed("Choose a file first.", 400)
        data = await up.read(d.s.max_attachment_bytes + 1)
        try:
            q, info = await asyncio.to_thread(mail_src.mail.add_file, data, filename=up.filename, content_type=up.content_type,
                                              item_id=item_id, index=int(str(form.get("index", "-1"))))
        except (MailError, ValueError) as e:
            return await failed(str(e), 409)
        audit.record("approval.file_added", actor="owner", via=rec.get("via", ""), kind="mail",
                     summary=f"Added {info['filename']} to a waiting mail")
        return JSONResponse({"ok": True, "sha": q.sha256, "index": info["index"], "filename": info["filename"], "size": info["size"], "asked": info["asked"],
                             "other_name": info["filename"] != info["asked"], "waiting": [w["filename"] for w in info["waiting"]]},
                            headers=_HEADERS)

    @mcp.custom_route("/dashboard/attach/{item_id}/remove", methods=["POST"])
    async def attach_remove(request: Request) -> Response:
        got = await checked_form(request)
        if isinstance(got, Response):
            return got
        _, rec, form = got
        item_id = request.path_params["item_id"]
        try:
            if mail_src is None:
                raise MailError("Mail is off on this server.")
            q, info = await asyncio.to_thread(mail_src.mail.remove_file, item_id, int(str(form.get("index", "-1"))))
        except (MailError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=409, headers=_HEADERS)
        audit.record("approval.file_removed", actor="owner", via=rec.get("via", ""), kind="mail",
                     summary=f"Took the file for {info['asked']} out of a waiting mail")
        return JSONResponse({"ok": True, "sha": q.sha256, "index": info["index"], "asked": info["asked"],
                             "waiting": [w["filename"] for w in info["waiting"]]}, headers=_HEADERS)

    @mcp.custom_route("/files/{token}", methods=["PUT"])
    async def file_put(request: Request) -> Response:
        """An agent's upload: the raw bytes of one file a waiting mail asked for. The token from the agent's result is the key."""
        if mail_src is None:
            return JSONResponse({"ok": False, "error": "Mail is off on this server."}, status_code=404, headers=_HEADERS)
        limit, body = d.s.max_attachment_bytes, bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > limit:
                return JSONResponse({"ok": False, "error": f"The file is larger than {limit} bytes (MAX_ATTACHMENT_BYTES)."},
                                    status_code=413, headers=_HEADERS)
        try:
            q, info = await asyncio.to_thread(mail_src.mail.add_file, bytes(body), filename=request.query_params.get("filename"),
                                              content_type=request.headers.get("content-type"), token=request.path_params["token"])
        except MailError as e:
            gone = str(e).startswith("No file is waiting")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=404 if gone else 400, headers=_HEADERS)
        audit.record("approval.file_added", actor="agent", kind="mail", summary=f"An upload added {info['filename']} to a waiting mail")
        d.push.notify("file", f"mail:{q.id}")
        left = [w["filename"] for w in info["waiting"]]
        return JSONResponse({"ok": True, "filename": info["filename"], "size": info["size"], "still_waiting": left,
                             "next": ("Upload the other files too." if left else "The mail now waits for the owner's approval. It is not sent yet.")},
                            headers=_HEADERS)

    # -- activity ------------------------------------------------------------------------------------------------------
    _FILTERS = {"all": ("All", ()), "approvals": ("Approvals", ("approval.",)), "agents": ("Agents", ("tool.",)),
                "signins": ("Sign-ins", ("signin.", "passkey.", "app."))}

    @mcp.custom_route("/dashboard/activity.json", methods=["GET"])
    async def activity_json(request: Request) -> Response:
        got = signed_in(request)
        if got is None:
            return to_signin(request)
        entries = await asyncio.to_thread(audit.entries, 100000)
        return Response(json.dumps(list(reversed(entries)), ensure_ascii=False, indent=1), media_type="application/json",
                        headers={**_HEADERS, "Content-Disposition": 'attachment; filename="icloud-mcp-activity.json"'})

    # other parts (settings.py) build on the same guards
    d.signed_in, d.recent = signed_in, recent
    # Web Push: a note to the owner's devices when something new waits, or a waiting mail got its file (push.py)
    from .push import PushStore
    d.push = PushStore(s.data_dir, s.public_url)
    for src in d.sources.values():
        box = getattr(getattr(src, "mail", None), "outbox", None) or getattr(getattr(src, "cal", None), "outbox", None) or getattr(src, "box", None)
        if box is not None:
            box.on_new = (lambda q, kind=src.kind: d.push.notify(kind, f"{kind}:{q.id}"))
    from .dashboard_api import register_api
    register_api(mcp, d, provider, s, audit, signed_in=signed_in, recent=recent, set_cookie=set_cookie,
                 record_failure_line=record_failure_line, headers=_HEADERS, filters=_FILTERS, for_owner=for_owner,
                 owner_password_problem=owner_password_problem)
    from .dashboard_accounts import register_accounts_api
    from .textedit import register_text_api
    register_text_api(mcp, d, audit)
    register_accounts_api(mcp, d)
    return d


