"""Settings on the dashboard: every setting of this server in one place, with where its value comes from.

The owner can change what the server does (areas, mail, calendar, contacts, the Mac helper, safety, the dashboard itself).
Where and how it listens, signs apps in and reaches its services stays in the environment and is shown read-only: a browser
session must never open a new OAuth redirect, a port or a bridge (config.DASHBOARD_EDITABLE is the list).

Changed values go to DATA_DIR/settings.json (mode 600) and win over the environment from the next start. Before anything is
saved the server builds its configuration with the new values and refuses a change it could not start with. A change that
weakens protection (sending directly, allowing invitations, emptying Trash, a wider allowlist, turning the log off...) asks
for the owner's passkey or password again. DASHBOARD_SETTINGS=view shows everything without changing it; =off ignores
settings.json altogether.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from dataclasses import dataclass, field as dfield
from typing import Any, Callable
from zoneinfo import ZoneInfo

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from .config import SETTINGS_FILE, Settings, editable_keys

log = logging.getLogger(__name__)
_TRUE = ("1", "true", "yes", "on")


@dataclass
class Spec:
    key: str
    label: str
    kind: str = "bool"            # bool | int | text | list | choice | textarea
    help: str = ""
    default: str = ""
    choices: tuple[tuple[str, str], ...] = ()   # (value, label)
    unit: int = 1                 # shown value = stored / unit (bytes -> MiB, seconds -> hours)
    unit_label: str = ""
    loosens: Callable[[str, str], bool] | None = None   # (old, new) -> True when the change weakens protection
    min: int | None = None
    max: int | None = None
    section: str = ""             # groups fields into cards on a settings page
    hidden: bool = False          # set through another control (the sending choice)


def _on(v: str) -> bool:
    return str(v).strip().lower() in _TRUE


def _to_true(old: str, new: str) -> bool:
    return not _on(old) and _on(new)


def _to_false(old: str, new: str) -> bool:
    return _on(old) and not _on(new)


def _higher(old: str, new: str) -> bool:
    try:
        return int(new or 0) > int(old or 0)
    except ValueError:
        return True


def _lower(old: str, new: str) -> bool:
    try:
        return int(new or 0) < int(old or 0)
    except ValueError:
        return True


def _wider(old: str, new: str) -> bool:
    """An allowlist is wider when it becomes empty (= anyone) or gains an entry."""
    o = {x.strip().lower() for x in old.split(",") if x.strip()}
    n = {x.strip().lower() for x in new.split(",") if x.strip()}
    return bool(o) and (not n or not n <= o)


def _changed(old: str, new: str) -> bool:
    return old.strip() != new.strip()


_INVITE_LEVEL = {"false": 0, "": 0, "approve": 1, "true": 2}


def _invites_open(old: str, new: str) -> bool:
    """Off < after approval < allowed: each step up lets agents invite people more freely."""
    return _INVITE_LEVEL.get(new.strip().lower(), 2) > _INVITE_LEVEL.get(old.strip().lower(), 2)


def _entries(v: str) -> set[str]:
    return {x.strip().lower() for x in v.split(",") if x.strip()}


def _removes_entry(old: str, new: str) -> bool:
    """A blocklist (chats never shown, people never messaged) is weaker as soon as an entry leaves it."""
    return not _entries(old) <= _entries(new)


def _adds_entry(old: str, new: str) -> bool:
    """A list that grants something (addresses that count as the owner's) is wider as soon as it gains an entry."""
    return not _entries(new) <= _entries(old)


def _more_history(old: str, new: str) -> bool:
    """An age limit where 0 means everything: it widens when it goes to 0 or above a limit that was set."""
    try:
        o, n = int(old or 0), int(new or 0)
    except ValueError:
        return True
    return o != 0 and (n == 0 or n > o)


@dataclass
class Group:
    slug: str
    title: str
    lede: str
    specs: list[Spec] = dfield(default_factory=list)
    needs_bridge: bool = False
    summary: Callable[[Callable[[str], str]], str] | None = None   # one line for the Settings list, from (key -> value)


def _plugin_groups() -> list[Group]:
    from .plugins import settings_groups
    return settings_groups()


CORE_GROUPS: list[Group] = [
    Group("accounts", "Sender", "Who your mail comes from and how it is signed.", [
        Spec("ICLOUD_EMAIL_ADDRESS", "From address", "text", "Your address or an alias; mail you send comes from it.",
             loosens=_changed),
        Spec("ICLOUD_DISPLAY_NAME", "Sender name", "text", "The name recipients see."),
        Spec("EMAIL_SIGNATURE", "Signature", "textarea", "Plain text added to the end of mail your agents send."),
    ], summary=lambda v: (f"Sends as {v('ICLOUD_DISPLAY_NAME')} " if v("ICLOUD_DISPLAY_NAME") else "Sends ")
                         + (f"from {v('ICLOUD_EMAIL_ADDRESS')}" if v("ICLOUD_EMAIL_ADDRESS") else "from the sign-in address")),
    Group("areas", "Areas and tools", "What your agents can reach at all. A switched-off area has no tools.", [
        Spec("ENABLE_MAIL", "Mail", default="true", loosens=_to_true),
        Spec("ENABLE_CALENDAR", "Calendar", default="true", loosens=_to_true),
        Spec("ENABLE_CONTACTS", "Contacts", default="true", loosens=_to_true),
        Spec("TOOLS", "Only these tools", "list", "Empty means every tool of the areas above. 'essential' is a small set; you can also "
             "name areas (mail, calendar) or tool names.", loosens=_changed),
    ], summary=lambda v: (", ".join(n for n, k in (("Mail", "ENABLE_MAIL"), ("Calendar", "ENABLE_CALENDAR"), ("Contacts", "ENABLE_CONTACTS"))
                                    if _on(v(k))) or "Nothing") + " on" + (f" · only {v('TOOLS')}" if v("TOOLS") else "")),
    Group("mail", "Mail", "How agents send and clean up mail.", [
        Spec("ALLOW_SEND", "Agents may send mail", default="true", loosens=_to_true, section="Sending", hidden=True),
        Spec("SEND_REQUIRES_APPROVAL", "Send only after my approval", default="true", loosens=_to_false, section="Sending", hidden=True),
        Spec("SEND_ALLOWLIST", "Only send to", "list", "Addresses or domains such as @example.org. Empty means anyone.", loosens=_wider,
             section="Sending"),
        Spec("MAX_RECIPIENTS", "Recipients per message", "int", default="25", min=1, max=500, loosens=_higher, section="Sending"),
        Spec("SAVE_SENT_COPY", "Keep a copy in Sent", default="true", section="Sending"),
        Spec("OUTBOX_TTL_SECONDS", "Waits for approval", "int", "After this long an unapproved message is dropped, never sent.",
             default="86400", unit=3600, unit_label="hours", min=1, max=720, section="Approval queue"),
        Spec("OUTBOX_MAX", "Messages waiting at most", "int", default="20", min=1, max=500, loosens=_higher, section="Approval queue"),
        Spec("MAX_ATTACHMENT_BYTES", "Largest attachment", "int", default=str(5 * 1024 * 1024), unit=1024 * 1024, unit_label="MiB",
             min=1, max=100, loosens=_higher, section="Reading and cleanup"),
        Spec("MAIL_MAX_AGE_DAYS", "Only mail from the last", "int", "0 means the whole mailbox.", default="0", min=0, max=36500,
             unit_label="days", loosens=_more_history, section="Reading and cleanup"),
        Spec("MAX_BODY_CHARS", "Longest text shown to agents", "int", default="30000", min=1000, max=500000, unit_label="characters",
             loosens=_higher, section="Reading and cleanup"),
        Spec("ALLOW_PERMANENT_DELETE", "Allow emptying Trash", default="false",
             help="Mail deleted from Trash cannot be recovered.", loosens=_to_true, section="Reading and cleanup"),
    ], summary=lambda v: ("Off (read-only)" if _on(v("READ_ONLY")) else "Drafts only" if not _on(v("ALLOW_SEND"))
                          else "Sends after your approval" if _on(v("SEND_REQUIRES_APPROVAL")) else "Sends directly")
                         + f" · attachments up to {int(v('MAX_ATTACHMENT_BYTES') or 0) // 1048576} MiB"),
    Group("calendar", "Calendar", "Time zone, where new events go, and whether agents may invite people.", [
        Spec("DEFAULT_TIMEZONE", "Time zone", "text", "An IANA name such as Europe/Amsterdam, for times without an offset.", default="UTC",
             section="General"),
        Spec("DEFAULT_CALENDAR", "Calendar for new events", "text", "Empty: “Calendar” or “Home”, else the first.", section="General"),
        Spec("OWNER_ADDRESSES", "My other addresses", "list", "Aliases, so invitations to them count as yours.", loosens=_adds_entry,
             section="General"),
        Spec("ALLOW_CALENDAR_INVITES", "Invitations to other people", "choice",
             "Events with guests make iCloud email them: invitations, updates and cancellations.", default="false",
             choices=(("false", "Off"), ("approve", "After my approval"), ("true", "Allowed")), loosens=_invites_open, section="Invitations"),
        Spec("INVITE_ALLOWLIST", "Only invite", "list", "Addresses or domains. Empty means anyone.", loosens=_wider, section="Invitations"),
        Spec("MAX_ATTENDEES", "Guests per event", "int", default="10", min=1, max=200, loosens=_higher, section="Invitations"),
    ], summary=lambda v: f"{v('DEFAULT_TIMEZONE') or 'UTC'} · invitations "
                         + {"approve": "after your approval", "true": "allowed"}.get(v("ALLOW_CALENDAR_INVITES").lower(), "off")),
    Group("contacts", "Contacts", "What agents may change on contact cards.", [
        Spec("CONTACTS_ALLOW_EMAIL_CHANGES", "Agents may add or change addresses and numbers", default="true",
             help="Off stops a planted address on a real contact.", loosens=_to_true),
    ], summary=lambda v: "Agents may change addresses and numbers" if _on(v("CONTACTS_ALLOW_EMAIL_CHANGES")) else "Addresses and numbers are protected"),
    Group("helper", "Mac helper", "Reminders, Notes, iCloud Drive, Maps, Messages and Health through your Mac.", [
        Spec("ENABLE_REMINDERS", "Reminders", default="false", loosens=_to_true, section="Areas"),
        Spec("ENABLE_NOTES", "Notes", default="false", loosens=_to_true, section="Areas"),
        Spec("ENABLE_DRIVE", "iCloud Drive", default="false", loosens=_to_true, section="Areas"),
        Spec("ENABLE_MAPS", "Maps", default="false", loosens=_to_true, section="Areas"),
        Spec("ENABLE_IMESSAGE", "Read Messages", default="false", loosens=_to_true, section="Areas"),
        Spec("ENABLE_HEALTH", "Health", default="false", loosens=_to_true, section="Areas"),
        Spec("IMESSAGE_ALLOW_SEND", "Agents may send iMessages", default="false", loosens=_to_true, section="Messages"),
        Spec("IMESSAGE_SEND_REQUIRES_APPROVAL", "Send iMessages only after my approval", default="true", loosens=_to_false, section="Messages"),
        Spec("IMESSAGE_SEND_ALLOWLIST", "Who may receive an iMessage", "list", "Empty means nobody, * anyone.", loosens=_changed, section="Messages"),
        Spec("IMESSAGE_NEVER_SEND", "Never send to", "list", loosens=_removes_entry, section="Messages"),
        Spec("IMESSAGE_HIDDEN_CHATS", "Chats agents never see", "list", loosens=_removes_entry, section="Messages"),
        Spec("IMESSAGE_VISIBLE_CHATS", "Only these chats", "list", "Empty means every chat except the hidden ones.", loosens=_changed, section="Messages"),
        Spec("IMESSAGE_MAX_AGE_DAYS", "Only messages from the last", "int", "0 means the whole history.", default="0", min=0, max=36500, unit_label="days",
             loosens=_more_history, section="Messages"),
        Spec("IMESSAGE_HIDE_SHORT_CODES", "Hide service senders", default="true", help="Banks, deliveries, one-time codes.", loosens=_to_false, section="Messages"),
        Spec("SHORTCUTS_ALLOW", "Shortcuts agents may run", "list", "Exact names; the Mac keeps its own list too.", loosens=_changed, section="Shortcuts and timing"),
        Spec("BRIDGE_JOB_TIMEOUT_SECONDS", "Wait for the Mac", "int", default="60", min=5, max=600, unit_label="seconds", section="Shortcuts and timing"),
    ], needs_bridge=True, summary=lambda v: (", ".join(n for n, k in (("Reminders", "ENABLE_REMINDERS"), ("Notes", "ENABLE_NOTES"),
                                                                       ("Drive", "ENABLE_DRIVE"), ("Maps", "ENABLE_MAPS"),
                                                                       ("Messages", "ENABLE_IMESSAGE"), ("Health", "ENABLE_HEALTH"))
                                                    if _on(v(k))) or "Nothing") + " on"),
    Group("safety", "Safety", "Switches that limit everything at once.", [
        Spec("READ_ONLY", "Read-only server", default="false", help="Nothing can be sent, moved, changed or deleted.", loosens=_to_false),
    ], summary=lambda v: "Read-only: nothing can change" if _on(v("READ_ONLY")) else "Agents may make changes"),
    Group("dashboard", "Dashboard and log", "How this dashboard signs you in and how long activity is kept.", [
        Spec("OWNER_PASSWORD_LOGIN", "Allow sign-in with the owner password", default="true",
             help="Turn it off once you have a passkey.", loosens=_to_true, section="Sign-in"),
        Spec("DASHBOARD_SESSION_MINUTES", "Sign out after", "int", "Minutes without use.", default="30", min=5, max=720, unit_label="minutes", loosens=_higher, section="Sign-in"),
        Spec("AUDIT_LOG", "Keep an activity log", default="true", loosens=_to_false, section="Activity log"),
        Spec("AUDIT_KEEP_DAYS", "Keep activity for", "int", default="90", min=1, max=3650, unit_label="days", loosens=_lower, section="Activity log"),
    ], summary=lambda v: ("Password sign-in on" if _on(v("OWNER_PASSWORD_LOGIN")) else "Passkey only")
                         + (f" · activity kept {v('AUDIT_KEEP_DAYS')} days" if _on(v("AUDIT_LOG")) else " · no activity log")),
]


def all_groups() -> list[Group]:
    """The core's groups and the group each service plugin brings as data (plugins/__init__.py)."""
    return CORE_GROUPS + _plugin_groups()


def specs() -> dict[str, Spec]:
    return {sp.key: sp for g in all_groups() for sp in g.specs}


GROUPS = all_groups()          # as found at import; the dashboard asks all_groups(), so a plugin found later counts too
SPECS = specs()
assert set(SPECS) <= editable_keys(), set(SPECS) - editable_keys()

# Shown read-only under "Server": (label, environment variable, how to show it)
SERVER_VIEW = [
    ("Address", [("Public address", "MCP_PUBLIC_URL", "text"), ("Listens on", ("MCP_HOST", "MCP_PORT"), "hostport"),
                 ("Extra allowed hosts", "MCP_EXTRA_ALLOWED_HOSTS", "text")]),
    ("App sign-in", [("Apps may return to", "OAUTH_ALLOWED_REDIRECT_HOSTS", "text"), ("Access token lifetime (s)", "ACCESS_TOKEN_TTL", "text"),
                     ("Refresh token lifetime (s)", "REFRESH_TOKEN_TTL", "text"),
                     ("Repeated refresh accepted for (s)", "REFRESH_TOKEN_GRACE_SECONDS", "text")]),
    ("Connections", [("IMAP", ("IMAP_HOST", "IMAP_PORT"), "hostport"), ("SMTP", ("SMTP_HOST", "SMTP_PORT"), "hostport"),
                     ("CalDAV", "CALDAV_URL", "text"), ("CardDAV", "CARDDAV_URL", "text"), ("Sign-in name", "ICLOUD_USERNAME", "text")]),
    ("Performance", [("Tool time limit (s)", "TOOL_TIMEOUT_SECONDS", "text"), ("Parallel tool calls", "TOOL_WORKERS", "text"),
                     ("Mail connections kept", "IMAP_POOL_SIZE", "text"), ("Calendar connections kept", "CALDAV_POOL_SIZE", "text")]),
    ("Other", [("Mac helper bridge", "BRIDGE_TOKEN", "set"), ("Menu bar admin API port", "ADMIN_PORT", "text"),
               ("Own safety screen", "SAFETY_SCREEN", "set"), ("Owner's rules for agents", "AGENT_NOTES_FILE", "set"),
               ("Data folder", "DATA_DIR", "text"), ("Log level", "LOG_LEVEL", "text")]),
]


class SettingsStore:
    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, SETTINGS_FILE)
        self._lock = threading.Lock()

    def read(self) -> dict[str, str]:
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if k in editable_keys() and isinstance(v, str)} if isinstance(data, dict) else {}

    def write(self, values: dict[str, str]) -> None:
        from .admin import _write_private
        with self._lock:
            _write_private(self.path, json.dumps(dict(sorted(values.items())), indent=1) + "\n")

    def update(self, set_: dict[str, str] | None = None, remove: tuple[str, ...] = ()) -> dict[str, str]:
        """Apply only these keys to what is on disk now, read under the lock, so two saves at once cannot lose a change."""
        from .admin import _write_private
        with self._lock:
            values = {k: v for k, v in {**self.read(), **(set_ or {})}.items() if k not in remove}
            _write_private(self.path, json.dumps(dict(sorted(values.items())), indent=1) + "\n")
            return values


def current(key: str, stored: dict[str, str]) -> tuple[str, str]:
    """(value, source) where source is 'dashboard', 'environment' or 'default'."""
    if key in stored:
        return stored[key], "dashboard"
    env = os.environ.get(key)
    if env is not None and env.strip() != "":
        return env.strip(), "environment"
    known = specs()
    return known[key].default if key in known else "", "default"


def parse(sp: Spec, form: Any) -> str:
    """The submitted value as the environment would hold it. Raises ValueError with a message for the owner."""
    raw = form.get(sp.key)
    if sp.kind == "bool":
        return "true" if raw is not None and str(raw) in ("1", "true", "on") else "false"
    v = str(raw or "").strip()
    if sp.kind == "int":
        try:
            n = float(v.replace(",", ".")) if v else 0
        except ValueError:
            raise ValueError(f"{sp.label}: enter a number.") from None
        if (sp.min is not None and n < sp.min) or (sp.max is not None and n > sp.max):
            raise ValueError(f"{sp.label}: between {sp.min} and {sp.max}{' ' + sp.unit_label if sp.unit_label else ''}.")
        return str(int(round(n * sp.unit)))
    if sp.kind == "choice":
        if v not in {c for c, _ in sp.choices}:
            raise ValueError(f"{sp.label}: pick one of the options.")
        return v
    if sp.kind == "list":
        return ",".join(x.strip() for x in v.replace("\n", ",").split(",") if x.strip())
    if sp.kind == "textarea":
        return v.replace("\r\n", "\n").replace("\n", "\\n")[:2000]
    return v[:300]


def validate(new_values: dict[str, str], known_tools: set[str]) -> tuple[str, str] | None:
    """Would the server start with these values? Returns (setting, reason) when it would not, the reason in the owner's words;
    the setting is "" when the server cannot tell which one is wrong."""
    if tz := new_values.get("DEFAULT_TIMEZONE"):
        try:
            ZoneInfo(tz)
        except Exception:  # noqa: BLE001
            return "DEFAULT_TIMEZONE", f"“{tz}” is not a time zone name. Use one such as Europe/Amsterdam or America/New_York."
    if tools := new_values.get("TOOLS"):
        from .server import AREA_PRESETS, RENAMED
        unknown = [t for t in tools.split(",") if t and t != "essential" and t not in AREA_PRESETS and t not in RENAMED and t not in known_tools]
        if unknown:
            return "TOOLS", "Unknown tool names: " + ", ".join(unknown) + ". The server would not start with them."
    try:
        s = Settings.from_env(dashboard_values=new_values)
        s.validate_for_server()
    except SystemExit as e:
        return "", str(e)
    except ValueError as e:
        return "", f"A value does not parse: {e}"
    return None


def register_settings(d: Any, mcp: Any, s: Settings) -> None:
    """Adds Settings to the dashboard `d` (dashboard.Dashboard)."""
    from . import dashboard as D

    store = SettingsStore(s.data_dir)
    editable = s.dashboard_settings == "edit"
    d.extra_nav.append(("settings", "Settings", "/dashboard#/settings"))
    started_with = dict(s.settings_active and store.read() or {})
    state = {"restart_needed": False}

    def known_tools() -> set[str]:
        return {t.name for t in mcp._tool_manager.list_tools()}

    def group_summary(g: Group, stored: dict[str, str]) -> str:
        parts = []
        for sp in g.specs[:3]:
            v, _ = current(sp.key, stored)
            parts.append(f"{sp.label}: {display(sp, v)}")
        return " · ".join(parts)

    def display(sp: Spec, v: str) -> str:
        if sp.kind == "bool":
            return "on" if _on(v) else "off"
        if sp.kind == "choice":
            return dict(sp.choices).get(v.lower(), v)
        if sp.kind == "int" and sp.unit != 1:
            try:
                return f"{int(v) / sp.unit:g} {sp.unit_label}"
            except ValueError:
                return v
        if sp.kind == "int" and sp.unit_label:
            return f"{v} {sp.unit_label}"
        return v or "(empty)"

    def read_form(g: Group, form: dict[str, str], stored: dict[str, str]):
        if g.slug == "mail" and form.get("SEND_MODE") in ("drafts", "approval", "direct"):
            mode = form.get("SEND_MODE")
            form = {**form, "ALLOW_SEND": "true" if mode != "drafts" else "", "SEND_REQUIRES_APPROVAL": "true" if mode != "direct" else ""}
            form = {k: v for k, v in form.items() if v != ""}
        new, changes, looser, entered, typed, errors = dict(stored), [], [], {}, {}, {}
        for sp in g.specs:
            old, _src = current(sp.key, stored)
            try:
                val = parse(sp, form)
            except ValueError as e:
                typed[sp.key], errors[sp.key] = str(form.get(sp.key) or ""), str(e)
                continue
            entered[sp.key] = val
            if val == old:
                continue
            new[sp.key] = val
            changes.append((sp, old, val))
            if sp.loosens and sp.loosens(old, val):
                looser.append(sp)
        return new, changes, looser, entered, typed, errors

    def group_for(request: Request) -> Group | None:
        g = next((x for x in all_groups() if x.slug == request.path_params["slug"]), None)
        return g if g is not None and editable else None

    @mcp.custom_route("/dashboard/settings", methods=["GET"])
    async def settings_old_address(_: Request) -> Response:
        return RedirectResponse("/dashboard#/settings", status_code=303, headers=D._HEADERS)

    @mcp.custom_route("/dashboard/settings/{rest:path}", methods=["GET"])
    async def settings_old_pages(_: Request) -> Response:
        return RedirectResponse("/dashboard#/settings", status_code=303, headers=D._HEADERS)

    # -- JSON for the Ionic app (dashboard_api.py has the guards) ------------------------------------------------------
    def spec_json(sp: Spec, stored: dict[str, str]) -> dict[str, Any]:
        v, src = current(sp.key, stored)
        shown = v
        if sp.kind == "int" and sp.unit != 1:
            try:
                shown = f"{int(v) / sp.unit:g}"
            except ValueError:
                pass
        if sp.kind == "textarea":
            shown = v.replace("\\n", "\n")
        return {"key": sp.key, "label": sp.label, "kind": sp.kind, "help": sp.help, "value": _on(v) if sp.kind == "bool" else shown,
                "display": display(sp, v), "source": src, "section": sp.section, "choices": [list(c) for c in sp.choices],
                "min": sp.min, "max": sp.max, "unit_label": sp.unit_label, "set_here": sp.key in stored}

    def group_json(g: Group, stored: dict[str, str], full: bool) -> dict[str, Any]:
        set_up = not (g.needs_bridge and not s.bridge_token)
        out = {"slug": g.slug, "title": g.title, "lede": g.lede, "needs_mac": g.needs_bridge, "set_up": set_up,
               "summary": (g.summary(lambda k: current(k, stored)[0]) if g.summary else group_summary(g, stored)) if set_up
               else "Not set up. See the Mac helper guide."}
        if full:
            out["fields"] = [spec_json(sp, stored) for sp in g.specs if not sp.hidden]
            if g.slug == "mail":
                allow = current("ALLOW_SEND", stored)[0]
                appr = current("SEND_REQUIRES_APPROVAL", stored)[0]
                out["send_mode"] = "drafts" if not _on(allow) else ("approval" if _on(appr) else "direct")
        return out

    @mcp.custom_route("/dashboard/api/settings", methods=["GET"])
    async def api_settings(request: Request) -> Response:
        got = d.api_reader(request)
        if isinstance(got, Response):
            return got
        stored = store.read()
        server = [{"title": title, "rows": [[label, (":".join(os.environ.get(k, "") or "default" for k in key) if how == "hostport" else
                                                     ("set" if os.environ.get(key, "").strip() else "not set") if how == "set" else
                                                     (os.environ.get(key, "").strip() or "default"))] for label, key, how in items]}
                  for title, items in SERVER_VIEW]
        return d.api_ok({"editable": editable, "restart_needed": bool(state["restart_needed"] or stored != started_with),
                         "groups": [group_json(g, stored, True) for g in all_groups()], "server": server, "mac": d.bridge_state(),
                         "values": {sp.key: current(sp.key, stored)[0] for g in all_groups() for sp in g.specs}})

    @mcp.custom_route("/dashboard/api/settings/restart", methods=["POST"])
    async def api_restart(request: Request) -> Response:
        got = await d.api_writer(request)
        if isinstance(got, Response):
            return got
        rec = got[1]
        d.audit.record("server.restart", actor="owner", via=rec.get("via", ""), summary="Restarted the server to apply settings")
        log.warning("Owner asked for a restart from the dashboard")
        threading.Timer(0.7, d.exit_fn).start()
        return d.api_ok({"note": "Restarting. The server is back in about half a minute."})

    @mcp.custom_route("/dashboard/api/settings/{slug}", methods=["POST"])
    async def api_save(request: Request) -> Response:
        got = await d.api_writer(request)
        if isinstance(got, Response):
            return got
        _, rec, body = got
        g = group_for(request)
        if g is None:
            return d.api_err("These settings cannot be changed here.", 403)
        raw = body.get("values") if isinstance(body.get("values"), dict) else {}
        stored = store.read()
        # The app sends only what changed; every other field of the group keeps its current value, in the form's units
        form = {}
        for sp in g.specs:
            cur = spec_json(sp, stored)["value"]
            form[sp.key] = ("true" if cur else "false") if sp.kind == "bool" else str(cur)
        form.update({k: ("true" if v is True else "false" if v is False else str(v)) for k, v in raw.items()})
        new, changes, looser, _entered, _typed, errors = read_form(g, form, stored)
        if errors:
            return d.api_err("Not saved: " + next(iter(errors.values())), 400, fields=errors)
        if not changes:
            return d.api_ok({"note": "Nothing changed."})
        if looser and not d.recent(rec):
            return d.api_need_confirm()
        if problem := await asyncio.to_thread(validate, new, known_tools()):
            key, why = problem
            return d.api_err(f"Not saved: {why}", 400, fields={key: why} if key else {})
        store.update({sp.key: v for sp, _, v in changes})
        state["restart_needed"] = True
        d.audit.record("settings.changed", actor="owner", via=rec.get("via", ""), ok=True,
                       summary=f"Changed {g.title.lower()} settings: " + ", ".join(f"{sp.label} {display(sp, o)} → {display(sp, v)}" for sp, o, v in changes),
                       keys=[sp.key for sp, _, _ in changes], weakened=[sp.key for sp in looser])
        log.warning("Owner changed %s from the dashboard; they apply after a restart", ", ".join(sp.key for sp, _, _ in changes))
        return d.api_ok({"note": "Saved. It applies after a restart.", "restart_needed": True})

    @mcp.custom_route("/dashboard/api/settings/{slug}/reset", methods=["POST"])
    async def api_reset(request: Request) -> Response:
        got = await d.api_writer(request)
        if isinstance(got, Response):
            return got
        _, rec, body = got
        g = group_for(request)
        key = str(body.get("key", ""))
        stored = store.read()
        if g is None or key not in stored or key not in {sp.key for sp in g.specs}:
            return d.api_err("That setting was not changed here.", 404)
        sp = specs()[key]
        new = {k: v for k, v in stored.items() if k != key}
        back, _ = current(key, new)
        if sp.loosens and sp.loosens(stored[key], back) and not d.recent(rec):
            return d.api_need_confirm()
        if problem := await asyncio.to_thread(validate, new, known_tools()):
            return d.api_err(f"Not reset: {problem[1]}", 400)
        store.update(remove=(key,))
        state["restart_needed"] = True
        d.audit.record("settings.changed", actor="owner", via=rec.get("via", ""), summary=f"Reset {sp.label} to {display(sp, back)}", keys=[key])
        return d.api_ok({"note": f"{sp.label} now follows the server again ({display(sp, back)}). Restart to apply.", "restart_needed": True})
