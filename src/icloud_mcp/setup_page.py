"""Connecting an account: what the dashboard's "Connect a service" and each service's page send, checked and merged.

The owner connects iCloud or another provider (IMAP, SMTP, CalDAV, CardDAV) in Settings, turns an area on or off for an account,
replaces its password, sets it to read only or disconnects it. Every change goes through connect(): the fields are checked, the
account signs in with the provider before anything is stored (accounts.check), and accounts.put merges it into the file, so an
area always comes from one account. The routes are in dashboard_accounts.py; they need the dashboard session and, on the same
request, the owner's identity from Tailscale or Cloudflare Access (access_sso.py), so nobody else (an admin, a chat, an agent)
ever handles the person's passwords.

The old address /setup leads to that page in the dashboard. Passwords are never logged or shown.
"""
from __future__ import annotations

import re
from typing import Any

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from . import access_sso
from . import accounts as A
from .auth import _HEADERS
from .config import Settings

_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
AREA_NAMES = {"mail": "Mail", "calendar": "Calendar", "contacts": "Contacts"}
CONNECT_PATH = "/dashboard#/connect"


def areas_text(areas: list[str]) -> str:
    names = [AREA_NAMES[a] for a in A.AREAS if a in areas]
    return " and ".join([", ".join(names[:-1]), names[-1]]) if len(names) > 1 else "".join(names)


def server_problem(ep: dict[str, str]) -> str | None:
    """What is wrong with the servers someone entered for another provider, or None."""
    if not (ep["imap"] or ep["smtp"] or ep["caldav"] or ep["carddav"]):
        return "Enter at least one server."
    if bool(ep["imap"]) != bool(ep["smtp"]):
        return "Mail needs both servers: IMAP to receive and SMTP to send."
    for key, port in (("imap", 993), ("smtp", 587)):
        if ep[key] and not A.host_port(ep[key], port)[0]:
            return f"“{ep[key]}” is not a server address. For example: {key}.example.org:{port}"
    for key in ("caldav", "carddav"):
        if ep[key] and not re.fullmatch(r"https://[A-Za-z0-9.-]+\.[A-Za-z]{2,}(:\d{2,5})?(/\S*)?", ep[key]):
            return f"“{ep[key]}” is not an https address. For example: https://dav.example.org/"
    return None


def failure(acct: dict[str, Any], failed: list[str], kinds: dict[str, str] | None = None) -> dict[str, Any]:
    """Why the sign-in check failed, in words the person can act on: refused (address or password), unreachable (not their fault)
    or another error. {"head", "reasons", "entered"}; nothing of the provider's own answer, which can quote the account."""
    icloud = acct.get("provider") == "icloud"
    who = "Apple" if icloud else "Your provider"
    found = {(kinds or {}).get(a, "other") for a in failed}
    areas = areas_text([a if a != "smtp" else "mail" for a in failed])
    if found == {"unreachable"}:
        return {"head": f"{who} cannot be reached from this server right now ({areas}). It is not your address or password, "
                        "and nothing was saved.",
                "reasons": ["Try again in a few minutes. If it still fails, tell whoever runs this server: it may not be allowed "
                            "to reach this provider."], "entered": ""}
    if failed == ["smtp"] and found == {"refused"}:
        return {"head": f"{who} lets your assistant read but not send. Nothing was saved.",
                "reasons": ["Some providers allow sending from mail apps only after you turn it on in their settings.",
                            "Or turn on Read only."], "entered": ""}
    if "refused" in found:
        head = f"{who} did not accept the sign-in: the address and password do not match. Nothing was saved."
    else:
        head = f"Signing in to {'Apple' if icloud else 'your provider'} failed on a technical error ({areas}). Nothing was saved."
    reasons = (["Use the address of your Apple Account. On iPhone it is in Settings under your name.",
                "Use an app-specific password (16 letters), not your Apple Account password.",
                "Changing your Apple Account password revokes every app-specific password. Create a new one."] if icloud else
               ["Check the user name and password. Many providers use the full email address as the user name.",
                "Your provider may want an app password for mail apps.",
                "Check the server addresses in your provider's help pages."])
    return {"head": head, "reasons": reasons, "entered": str(acct.get("username", ""))}


def _draft(fields: dict[str, Any], data: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(the account as it would be saved, the account it changes or None for a new one)."""
    acct_id = str(fields.get("id") or "")
    if acct_id:
        before = next((a for a in data["accounts"] if a.get("id") == acct_id), None)
        return (dict(before), before) if before is not None else (None, None)
    p = str(fields.get("provider", ""))
    if p not in A.PROVIDERS:
        return None, None
    acct: dict[str, Any] = {"id": "", "provider": p, "username": "", "areas": [], "read_only": False}
    if p == "other":
        acct.update({k: "" for k in ("imap", "smtp", "caldav", "carddav")})
    return acct, None


async def connect(settings: Settings, data: dict[str, Any], fields: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Check and merge one account change. `fields` holds what changes: for a new account provider, username, password, areas,
    read_only, email_address, display_name and, for another provider, imap, smtp, caldav and carddav; for a change, the account's
    id and only what changes. Returns (the accounts to save, the account) or (None, {"message", maybe "failure"})."""
    acct, before = _draft(fields, data)
    if acct is None:
        return None, {"message": "There is no such account. Nothing was saved."}
    new = before is None
    p = acct["provider"]
    if p == "other" and new:
        acct.update({k: str(fields.get(k, "")).strip() for k in ("imap", "smtp", "caldav", "carddav")})
        if problem := server_problem(A.endpoints(acct)):
            return None, {"message": problem}
    for k, limit in (("username", 254), ("email_address", 254), ("display_name", 200)):
        if k in fields:
            acct[k] = str(fields.get(k) or "").strip()[:limit]
    if "read_only" in fields:
        acct["read_only"] = bool(fields["read_only"])
    if "areas" in fields:
        wanted = fields["areas"] if isinstance(fields["areas"], list) else []
        acct["areas"] = [a for a in A.AREAS if a in wanted and a in A.possible_areas(acct)]
    if not _EMAIL.fullmatch(acct.get("username", "")) or len(acct["username"]) > 254:
        return None, {"message": "That does not look like an email address."}
    if acct.get("email_address") and not _EMAIL.fullmatch(acct["email_address"]):
        return None, {"message": "“Send as” is not an email address."}
    if new and not acct["areas"]:
        return None, {"message": "Choose at least one of mail, calendar and contacts."}
    raw = str(fields.get("password") or "")
    if raw.strip() or new:
        acct["password"], problem = A.normalise_password(p, raw)
        if problem:
            return None, {"message": problem}
    # Sign in before saving: everything for a new account or a new password, otherwise only what this change adds
    old = before or {}
    if new or raw.strip():
        check = list(acct["areas"])
    else:
        check = [a for a in acct["areas"] if a not in (old.get("areas") or [])]
        if old.get("read_only") and not acct.get("read_only") and "mail" in acct["areas"] and "mail" not in check:
            check.append("mail")
    if check:
        result = await A.check(settings, acct, check)
        if result["failed"]:
            f = failure(acct, result["failed"], result.get("kinds"))
            return None, {"message": f["head"], "failure": f}
    return A.put(data, acct, "" if new else str(old.get("id", ""))), acct


def register_setup_routes(mcp: Any, settings: Settings, **_: Any) -> None:
    """/setup, the old address of the setup assistant, leads to "Connect a service" in the dashboard. Without an owner identity
    (Tailscale or Cloudflare Access) there is nothing to connect with, and the address stays unknown, as before."""
    if access_sso.from_settings(settings) is None:
        return

    @mcp.custom_route("/setup", methods=["GET"])
    async def setup_get(_: Request) -> Response:
        return RedirectResponse(CONNECT_PATH, status_code=303, headers=_HEADERS)
