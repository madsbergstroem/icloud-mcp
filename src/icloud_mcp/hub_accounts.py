"""Every account of this hub as agents see it (icloud_list_accounts): the mail, calendar and contacts accounts the owner connected,
and each chat channel with its accounts (iMessage on the Mac, and every service plugin through its `agent_accounts` hook).

The core assembles the answer; a plugin only lists its own accounts (plugins/__init__.py). Nothing secret leaves: no password, key,
token, server address or sign-in name, and for a plugin's accounts no address or number either. The log has counts only.
"""
from __future__ import annotations

import logging
from typing import Any

from . import accounts as A

log = logging.getLogger(__name__)

NOTE = ("Mail, calendar and contacts tools work on each area's default account (default_for). A chat account's id is what the "
        "chat tools take as account.")


def _provider(acct: dict[str, Any], address: str) -> str:
    """"iCloud", or the domain of the address for another provider (as the dashboard names it)."""
    if acct.get("provider") == "icloud":
        return "iCloud"
    return address.rsplit("@", 1)[-1] if "@" in address else A.PROVIDERS["other"]["label"]


def connected(s: Any) -> list[dict[str, Any]]:
    """The mail, calendar and contacts accounts: id, provider, address, the areas it holds, the areas it is the default of, read only."""
    data = A.current(s)
    defaults = data["defaults"]
    out = []
    for a in data["accounts"]:
        address = str(a.get("email_address") or a.get("username") or "")
        areas = [x for x in A.AREAS if x in (a.get("areas") or [])]
        out.append({"id": str(a.get("id", "")), "provider": _provider(a, address), "address": address, "areas": areas,
                    "default_for": [x for x in A.AREAS if defaults.get(x) == a.get("id")],
                    "read_only": bool(a.get("read_only")) or s.read_only or (bool(areas) and all(x in s.read_only_areas for x in areas))})
    return out


def _channel(hub: Any, prefix: str) -> dict[str, Any]:
    return {"chat_prefix": prefix, "reading": prefix in hub.backends(), "sending": prefix in hub.senders(),
            "archiving": prefix in hub.archivers()}


def channels(s: Any, plugins: list[Any], hub: Any) -> list[dict[str, Any]]:
    """iMessage when it is read or written here, then every plugin set up here, each with what agents can do and its accounts."""
    out: list[dict[str, Any]] = []
    plugin_prefixes = {p.capabilities.messages.prefix for p in plugins if p.capabilities.messages is not None}
    if "imessage" not in plugin_prefixes and ("imessage" in hub.backends() or "imessage" in hub.senders()):
        accts = [{"account": a.account, "name": a.name} for a in hub.backend_accounts("imessage")]
        out.append({"service": "imessage", "name": "iMessage", "capabilities": ["messages"], **_channel(hub, "imessage"), "accounts": accts})
    for p in plugins:
        row: dict[str, Any] = {"service": p.id, "name": p.name, "capabilities": p.capabilities.names()}
        if p.capabilities.messages is not None:
            row.update(_channel(hub, p.capabilities.messages.prefix))
        if p.agent_accounts is not None:
            try:
                state, refs = p.agent_accounts(s)
            except Exception as e:  # noqa: BLE001  -- one plugin must not take the list down
                log.warning("hub accounts: plugin %s failed (%s)", p.id, type(e).__name__)
                state, refs = "unreadable", []
            row["accounts"] = [{"account": r.account, "network": r.network, "name": r.name} for r in refs]
            if state != "ok":
                row["problem"] = state
        out.append(row)
    return out


def for_agents(s: Any, plugins: list[Any], hub: Any) -> dict[str, Any]:
    accts, chans = connected(s), channels(s, plugins, hub)
    log.info("hub accounts: accounts=%d channels=%d channel_accounts=%d", len(accts), len(chans),
             sum(len(c.get("accounts", [])) for c in chans))
    return {"accounts": accts, "channels": chans, "note": NOTE}
