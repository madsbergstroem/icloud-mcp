"""The dashboard's connected services: the saved accounts (iCloud, another provider), the networks behind Beeper, and the changes
the owner makes to an account on its page in Settings.

GET /dashboard/api/accounts shows each account's provider, address, areas, whether it is read only, its servers (another
provider only) and which areas it is the default account of, and whether this request may connect or change accounts. No
password or token leaves this route.

Where the Beeper gateway is set up, `beeper` lists the networks Beeper is signed in to (network, the owner's name there, Beeper's
account id), read live with the read key and kept in memory a few minutes (beeper.AccountsReader). Addresses and phone numbers in
Beeper's answer are never read. Without the read key, or when the gateway does not answer, `beeper.state` says why and the list
is empty; the page stands either way. Beeper is set up by whoever runs the server, not from here.

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
from . import beeper as B
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


_BEEPER_STATES = {"no_read_key": "Accounts not readable: no read key", "unreachable": "Gateway not reachable",
                  "refused": "Accounts not readable: the gateway refused the read key", "unreadable": "Accounts not readable: unexpected answer from Beeper"}


def beeper_listing(s: Any, reader: B.AccountsReader | None) -> dict[str, Any] | None:
    """The Beeper part of the list, or None where the gateway is not set up at all."""
    if reader is None or not s.beeper_gateway_url:
        return None
    got = reader.read()
    return {"state": got["state"], "problem": _BEEPER_STATES.get(got["state"], ""), "networks": got["networks"],
            "sending": B.configured(s), "reading": False}       # reading Beeper chats here is not built yet


def mail_account(s: Any) -> str:
    """The name of the provider mail is sent from ('iCloud'), for the From line of a waiting mail; '' for another provider."""
    a = A.by_area(A.current(s)).get("mail")
    if not a:
        return ""
    return "" if a.get("provider", "other") == "other" else A.PROVIDERS.get(a["provider"], A.PROVIDERS["other"])["label"]


def register_accounts_api(mcp: Any, d: Any) -> None:
    reader = B.accounts_reader(d.s) if d.s.beeper_gateway_url else None
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
        beeper = await asyncio.to_thread(beeper_listing, d.s, reader)      # the gateway client runs a loop of its own
        why = await why_not(request)
        via = "" if why else (await owner(request) or ("", ""))[1]
        presets = [{"id": k, **v} for k, v in A.SERVER_PRESETS.items()]   # they only fill in the servers on the connect form
        return d.api_ok({"accounts": services(d.s), "beeper": beeper, "connect": {"allowed": not why, "why": why, "via": via,
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
