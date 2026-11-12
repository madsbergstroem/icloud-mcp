"""The dashboard's connected services: the saved accounts (iCloud, another provider), the networks behind Beeper, and the changes
the owner makes to an account on its page in Settings.

GET /dashboard/api/accounts shows each account's provider, address, areas, whether it is read only, its servers (another
provider only) and which areas it is the default account of, and whether this request may connect or change accounts. No
password or token leaves this route.

`plugins` lists every service plugin (plugins/__init__.py): its name, status line, limits, Connect card text and whether it is
set up here; for one that is, what its page lists (Beeper: the networks it is signed in to, read live and kept a few minutes) and
what its messages capability does. The page draws any plugin from this data; no plugin brings code of its own to the dashboard.
A listing that cannot be read says why in `problem`, and the page stands either way.

Changing accounts (connect, an area on or off, password, read only, disconnect, which calendars) needs the dashboard session and,
on the same request, the owner's identity from Tailscale or Cloudflare Access (access_sso.py), as the /setup page did. Every
change is checked by setup_page.connect, written to accounts.json, and then the server restarts (about half a minute). Saving
also drops ENABLE_MAIL, ENABLE_CALENDAR and ENABLE_CONTACTS from the dashboard's settings, so the accounts decide which areas
are on.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

from . import access_sso
from . import accounts as A
from .plugins import Plugin, enabled_plugins, load_plugins
from . import setup_page as SP

log = logging.getLogger(__name__)
_AREA_LABELS = {"mail": "Mail", "calendar": "Calendar", "contacts": "Contacts"}
_AREA_SWITCHES = ("ENABLE_MAIL", "ENABLE_CALENDAR", "ENABLE_CONTACTS")
RESTART_NOTE = "Saved. The server restarts and is back in about half a minute."


def _name(provider: str, address: str) -> str:
    """What the service is called in the list: "iCloud", or the domain of the address for another provider."""
    if provider == "icloud":
        return "iCloud"
    return address.rsplit("@", 1)[-1] if "@" in address else A.PROVIDERS["other"]["label"]


def listing(s: Any) -> list[dict[str, Any]]:
    data = A.current(s)
    defaults = data["defaults"]
    out = []
    for a in data["accounts"]:
        provider = a.get("provider", "other")
        address = str(a.get("email_address") or a.get("username") or "")
        out.append({"id": str(a.get("id", "")), "provider": provider,
                    "label": A.PROVIDERS.get(provider, A.PROVIDERS["other"])["label"],
                    "address": address,
                    "areas": [_AREA_LABELS[x] for x in A.AREAS if x in (a.get("areas") or [])],
                    "default_for": [_AREA_LABELS[x] for x in A.AREAS if defaults.get(x) == a.get("id")],
                    "read_only": bool(a.get("read_only")),
                    "sign_in": "App-specific password" if provider == "icloud" else "Password",
                    "from_environment": a.get("id") == "env"})
    return out


def services(s: Any) -> list[dict[str, Any]]:
    """The list as the service pages need it: listing() plus area keys, what the account could serve, its servers, its name."""
    data = A.current(s)
    raw = {str(a.get("id", "")): a for a in data["accounts"]}
    out = []
    for row in listing(s):
        a = raw[row["id"]]
        out.append({**row, "name": _name(row["provider"], row["address"]), "uses": [x for x in A.AREAS if x in (a.get("areas") or [])],
                    "possible": list(A.possible_areas(a)),
                    "servers": A.endpoints(a) if row["provider"] == "other" else None,
                    "sender": {"email_address": a.get("email_address") or "", "display_name": a.get("display_name") or ""}})
    return out


def plugin_listing(s: Any, p: Plugin, listing: Any, on: bool) -> dict[str, Any]:
    """A plugin as Settings draws it. `listing` is the function its accounts() built for this server (or None)."""
    m = p.capabilities.messages
    out: dict[str, Any] = {"id": p.id, "name": p.name, "status": p.status, "limits": p.limits, "description": p.description,
                           "connection": p.connection.kind, "enabled": on, "capabilities": p.capabilities.names()}
    if not on:
        return out
    if listing is not None:
        try:
            got = listing()
            out["listing"] = {"label": got.label, "items": got.items, "problem": got.problem, "note": got.note, "state": got.state}
        except Exception as e:  # noqa: BLE001  -- the page must stand whatever a plugin does
            log.warning("plugin %s: listing failed (%s)", p.id, type(e).__name__)
            out["listing"] = {"label": "", "items": [], "problem": "Could not be read just now.", "note": "", "state": "unreadable"}
    if m is not None:
        group = p.settings_group() if p.settings_group is not None else None
        out["messages"] = {"prefix": m.prefix, "sending": bool(m.sending_on(s) and m.action_type is not None),
                           "reading": m.backend is not None and (m.reading_on is None or bool(m.reading_on(s))),
                           "settings": group.slug if group is not None else "",
                           "picker": bool(m.recipients_key), "networks_key": m.networks_key, "groups_key": m.groups_key,
                           "reading_keys": dict(m.reading_keys)}
    return out


def mail_account(s: Any) -> str:
    """The name of the provider mail is sent from ('iCloud'), for the From line of a waiting mail; '' for another provider."""
    a = A.by_area(A.current(s)).get("mail")
    if not a:
        return ""
    return "" if a.get("provider", "other") == "other" else A.PROVIDERS.get(a["provider"], A.PROVIDERS["other"])["label"]


def register_accounts_api(mcp: Any, d: Any) -> None:
    here = {p.id for p in enabled_plugins(d.s)}
    listings = {p.id: p.accounts(d.s) for p in load_plugins() if p.id in here and p.accounts is not None}   # built once per server

    def plugins() -> list[dict[str, Any]]:
        return [plugin_listing(d.s, p, listings.get(p.id), p.id in here) for p in load_plugins()]
    access = access_sso.from_settings(d.s)

    async def owner(request: Request) -> tuple[str, str] | None:
        """(identity, "Tailscale" or "Cloudflare Access") of the owner on this request, or None."""
        return await access.owner(request) if access is not None else None

    async def why_not(request: Request) -> str:
        """Why this request may not connect or change accounts, or "" when it may."""
        if access is None:
            return ("Connecting a service needs the owner's sign-in through Tailscale or Cloudflare Access, and this server has "
                    "neither (OWNER_TAILSCALE_LOGINS or OWNER_ACCESS_EMAILS). Whoever runs it can set one up.")
        if not await owner(request):
            return "Open the dashboard through Tailscale on your own device, so the server knows it is you."
        return ""

    @mcp.custom_route("/dashboard/api/accounts", methods=["GET"])
    async def accounts_list(request: Request) -> Response:
        got = d.api_reader(request)
        if isinstance(got, Response):
            return got
        listed = await asyncio.to_thread(plugins)       # a plugin's client may run a loop of its own
        why = await why_not(request)
        via = "" if why else (await owner(request) or ("", ""))[1]
        presets = [{"id": k, **v} for k, v in A.SERVER_PRESETS.items()]   # they only fill in the servers on the connect form
        return d.api_ok({"accounts": services(d.s), "plugins": listed, "connect": {"allowed": not why, "why": why, "via": via,
                                                                                   "presets": presets}})

    async def guarded(request: Request) -> tuple[dict[str, Any], dict[str, Any]] | Response:
        got = await d.api_writer(request)
        if isinstance(got, Response):
            return got
        if why := await why_not(request):
            return d.api_err(why, 403)
        return got[1], got[2]

    def save(data: dict[str, Any], rec: dict[str, Any], summary: str) -> Response:
        A.save(d.s.data_dir, data)
        from .settings import SettingsStore
        store = SettingsStore(d.s.data_dir)
        if any(k in store.read() for k in _AREA_SWITCHES):
            store.update(remove=_AREA_SWITCHES)          # the accounts decide which areas are on
        d.audit.record("accounts.changed", actor="owner", via=rec.get("via", ""), summary=summary)
        log.warning("Owner changed the connected accounts from the dashboard; restarting")
        threading.Timer(1.0, d.exit_fn).start()           # after the answer has gone out; the container brings the server back
        return d.api_ok({"note": RESTART_NOTE, "restarting": True})

    async def change(rec: dict[str, Any], fields: dict[str, Any], summary: Any) -> Response:
        data = A.current(d.s)
        new, acct = await SP.connect(d.s, data, fields)
        if new is None:
            return d.api_err(acct["message"], 400, **({"failure": acct["failure"]} if "failure" in acct else {}))
        return save(new, rec, summary(acct))

    def body_fields(body: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
        return {k: body[k] for k in keys if k in body}

    @mcp.custom_route("/dashboard/api/accounts/connect", methods=["POST"])
    async def accounts_connect(request: Request) -> Response:
        got = await guarded(request)
        if isinstance(got, Response):
            return got
        rec, body = got
        fields = body_fields(body, ("provider", "username", "password", "areas", "read_only", "email_address", "display_name",
                                    "imap", "smtp", "caldav", "carddav"))
        return await change(rec, fields, lambda a: f"Connected {A.PROVIDERS[a['provider']]['label']} for {SP.areas_text(a['areas'])}")

    @mcp.custom_route("/dashboard/api/accounts/{acct}/change", methods=["POST"])
    async def accounts_change(request: Request) -> Response:
        got = await guarded(request)
        if isinstance(got, Response):
            return got
        rec, body = got
        fields = {"id": request.path_params["acct"], **body_fields(body, ("password", "areas", "read_only", "email_address", "display_name"))}
        what = ", ".join(n for k, n in (("password", "password"), ("areas", "areas"), ("read_only", "read only"),
                                        ("email_address", "sender"), ("display_name", "sender")) if k in body)
        return await change(rec, fields, lambda a: f"Changed {A.PROVIDERS[a['provider']]['label']}: {what or 'nothing'}")

    @mcp.custom_route("/dashboard/api/accounts/{acct}/remove", methods=["POST"])
    async def accounts_remove(request: Request) -> Response:
        got = await guarded(request)
        if isinstance(got, Response):
            return got
        rec, _ = got
        data = A.current(d.s)
        gone = next((a for a in data["accounts"] if a.get("id") == request.path_params["acct"]), None)
        if gone is None:
            return d.api_err("There is no such account.", 404)
        data = dict(data, accounts=[a for a in data["accounts"] if a is not gone],
                    calendars_visible=[] if "calendar" in gone["areas"] else data["calendars_visible"])
        return save(data, rec, f"Disconnected {A.PROVIDERS.get(gone.get('provider'), A.PROVIDERS['other'])['label']}")

    @mcp.custom_route("/dashboard/api/accounts/{acct}/calendars", methods=["GET"])
    async def calendars_get(request: Request) -> Response:
        got = d.api_reader(request)
        if isinstance(got, Response):
            return got
        data = A.current(d.s)
        acct = A.by_area(data).get("calendar")
        if acct is None or acct.get("id") != request.path_params["acct"]:
            return d.api_err("This service does not serve the calendar.", 404)
        result = await A.check(d.s, acct, ["calendar"])
        if result["failed"]:
            return d.api_err(SP.failure(acct, result["failed"], result.get("kinds"))["head"].replace(" Nothing was saved.", ""), 502)
        from .cal import calendar_path
        visible = {calendar_path(u) for u in data["calendars_visible"]}
        return d.api_ok({"calendars": [{"id": c["id"], "name": c["name"], "visible": not visible or calendar_path(c["id"]) in visible}
                                       for c in result["calendars"]], "why": await why_not(request)})

    @mcp.custom_route("/dashboard/api/accounts/{acct}/calendars", methods=["POST"])
    async def calendars_set(request: Request) -> Response:
        got = await guarded(request)
        if isinstance(got, Response):
            return got
        rec, body = got
        data = A.current(d.s)
        acct = A.by_area(data).get("calendar")
        if acct is None or acct.get("id") != request.path_params["acct"]:
            return d.api_err("This service does not serve the calendar.", 404)
        chosen = [str(c) for c in (body.get("visible") or []) if isinstance(c, str) and c.startswith("https://")]
        if not chosen:
            return d.api_err("Choose at least one calendar.", 400)
        before = acct.get("id")
        data = dict(data, calendars_visible=chosen)
        if before == "env":                                    # the deployment's own account gets a real id once it is saved
            data = A.put(data, dict(acct), "env")
        return save(data, rec, f"Chose {len(chosen)} calendar{'s' if len(chosen) != 1 else ''} for the assistant")


# -- the recipient picker, for any plugin that sends messages --------------------------------------------------------------------
def _split(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


LISTS = ("recipients", "hidden", "visible")       # the chat lists the picker edits: who may be written to; chats never read; only these


def _list_key(m: Any, which: str) -> str:
    return m.recipients_key if which == "recipients" else m.reading_keys.get(which, "")


def _rules(s: Any, m: Any, which: str = "recipients") -> dict[str, Any]:
    """A plugin's rules as they stand in Settings now (saved values win over the environment; they apply after a restart). `which`
    picks the chat list: the recipients, or a reading list (hidden, visible), which has no `*`."""
    from .settings import SettingsStore, current
    stored = SettingsStore(s.data_dir).read()
    value = lambda key, dflt="": current(key, stored)[0] if key else dflt      # noqa: E731
    allow = _split(value(_list_key(m, which)))
    return {"list": which, "ids": [x for x in allow if x != "*"], "any": which == "recipients" and "*" in allow,
            "networks": _split(value(m.networks_key, "*")) if m.networks_key else ["*"],
            "groups": value(m.groups_key, "false").strip().lower() in ("1", "true", "yes", "on") if m.groups_key else False,
            "keys": {"recipients": _list_key(m, which), "networks": m.networks_key, "groups": m.groups_key}}


def _wire(c: Any, added: set[str] | None = None) -> dict[str, Any]:
    out = {"id": c.id, "title": c.title, "network": c.network, "group": c.group, "people": c.people,
           "last_activity": c.last_activity, "found": c.found}
    if added is not None:
        out["added"] = c.id.casefold() in added
    return out


def register_picker_api(mcp: Any, d: Any) -> None:
    """GET /dashboard/api/plugins/<id>/recipients: the chats on the plugin's list by name (a chat its service no longer knows says so),
    with the network and group rules and the networks to choose from. GET .../find?q=: chats found by name, or the most recent,
    each marked when it is on the list. GET .../resolve?id=: one chat, checked before it is added. Read only: the page changes the
    rules through /dashboard/api/settings/<group> like every other setting. Nothing here is stored, and names are never logged."""
    here = {p.id: p for p in enabled_plugins(d.s) if p.capabilities.messages is not None and p.capabilities.messages.recipients_key}

    def found(request: Request) -> Any:
        p = here.get(request.path_params["pid"])
        return (p, p.capabilities.messages) if p is not None else (None, None)

    def which(request: Request, m: Any) -> str | None:
        """?list=: recipients (the default), hidden or visible; None for a list this plugin does not have."""
        w = str(request.query_params.get("list", "recipients"))
        return w if w in LISTS and _list_key(m, w) else None

    @mcp.custom_route("/dashboard/api/plugins/{pid}/recipients", methods=["GET"])
    async def picker_recipients(request: Request) -> Response:
        got = d.api_reader(request)
        if isinstance(got, Response):
            return got
        p, m = found(request)
        if p is None:
            return d.api_err("No such service here.", 404)
        if (w := which(request, m)) is None:
            return d.api_err("No such list here.", 404)
        rules = _rules(d.s, m, w)
        (state, chats), nets = await asyncio.gather(asyncio.to_thread(m.resolve, d.s, rules["ids"]), asyncio.to_thread(m.networks, d.s))
        return d.api_ok({**rules, "state": state, "chats": [_wire(c) for c in chats], "all_networks": nets, "name": p.name})

    @mcp.custom_route("/dashboard/api/plugins/{pid}/find", methods=["GET"])
    async def picker_find(request: Request) -> Response:
        got = d.api_reader(request)
        if isinstance(got, Response):
            return got
        p, m = found(request)
        if p is None:
            return d.api_err("No such service here.", 404)
        q = str(request.query_params.get("q", "")).strip()[:100]
        state, chats = await asyncio.to_thread(m.search_chats, d.s, q)
        if (w := which(request, m)) is None:
            return d.api_err("No such list here.", 404)
        on = {x.casefold() for x in _rules(d.s, m, w)["ids"]}
        return d.api_ok({"state": state, "query": q, "chats": [_wire(c, on) for c in chats]})

    @mcp.custom_route("/dashboard/api/plugins/{pid}/resolve", methods=["GET"])
    async def picker_resolve(request: Request) -> Response:
        got = d.api_reader(request)
        if isinstance(got, Response):
            return got
        p, m = found(request)
        if p is None:
            return d.api_err("No such service here.", 404)
        cid = str(request.query_params.get("id", "")).strip()[:300]
        state, chats = await asyncio.to_thread(m.resolve, d.s, [cid] if cid else [])
        if state != "ok" or not chats or not chats[0].found:
            return d.api_err("That chat cannot be found any more. Nothing was added.", 404, state=state)
        return d.api_ok({"chat": _wire(chats[0])})
