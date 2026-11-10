"""The dashboard's list of connected accounts: what /setup has saved, read only.

GET /dashboard/api/accounts shows each account's provider, address, areas, whether it is read only and which areas it is the
default account of. Changing an account stays on the /setup assistant, which the app links to. No password, token or server
setting leaves this route.
"""
from __future__ import annotations

from typing import Any

from starlette.responses import Response

from . import access_sso
from . import accounts as A

_AREA_LABELS = {"mail": "Mail", "calendar": "Calendar", "contacts": "Contacts"}


def listing(s: Any) -> list[dict[str, Any]]:
    data = A.current(s)
    defaults = data["defaults"]
    out = []
    for a in data["accounts"]:
        provider = a.get("provider", "other")
        out.append({"id": str(a.get("id", "")), "provider": provider,
                    "label": "Other provider" if provider == "other" else A.PROVIDERS.get(provider, A.PROVIDERS["other"])["label"],
                    "address": str(a.get("email_address") or a.get("username") or ""),
                    "areas": [_AREA_LABELS[x] for x in A.AREAS if x in (a.get("areas") or [])],
                    "default_for": [_AREA_LABELS[x] for x in A.AREAS if defaults.get(x) == a.get("id")],
                    "read_only": bool(a.get("read_only")),
                    "sign_in": "App-specific password" if provider in ("icloud", "gmail") else "Password",
                    "from_environment": a.get("id") == "env"})
    return out


def mail_account(s: Any) -> str:
    """The name of the provider mail is sent from ('iCloud', 'WEB.DE'), for the From line of a waiting mail; '' when unknown."""
    a = A.by_area(A.current(s)).get("mail")
    if not a:
        return ""
    return "" if a.get("provider", "other") == "other" else A.PROVIDERS.get(a["provider"], A.PROVIDERS["other"])["label"]


def register_accounts_api(mcp: Any, d: Any) -> None:
    @mcp.custom_route("/dashboard/api/accounts", methods=["GET"])
    async def accounts_list(request: Any) -> Response:
        got = d.api_reader(request)
        if isinstance(got, Response):
            return got
        # /setup exists only where an owner identity is configured (Tailscale logins or Cloudflare Access); elsewhere it is a 404
        return d.api_ok({"accounts": listing(d.s), "setup_url": "/setup" if access_sso.from_settings(d.s) is not None else ""})
