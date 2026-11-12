"""The Beeper plugin: chat networks (WhatsApp, Signal, Telegram ...) through Beeper, behind the LLM gateway.

It offers the `messages` capability for sending: a request to a `beeper:` chat is queued for the owner's approval, and after the
approval the core calls this plugin's executor once (gateway.py has the client, the action type, the executor and the sender).
Reading chats is planned and not built yet. Whoever runs the server sets it up with BEEPER_* in the environment; the owner changes
only who may be written to (BEEPER_SEND_ALLOWLIST) on the dashboard.
"""
from __future__ import annotations

from typing import Any

from .. import Capabilities, Listing, MessagesCapability, Plugin
from . import gateway as G

_STATES = {"no_read_key": "Accounts not readable: no read key", "unreachable": "Gateway not reachable",
           "refused": "Accounts not readable: the gateway refused the read key", "unreadable": "Accounts not readable: unexpected answer from Beeper"}


def _settings_group() -> Any:
    from ...settings import Group, Spec, _changed
    return Group("beeper", "Beeper", "Chats on WhatsApp, Signal, Telegram and more that your assistant may write to, after your approval.", [
        Spec("BEEPER_SEND_ALLOWLIST", "Chats that may be written to", "list", "Chat ids as Beeper gives them. Empty means nobody, * every chat.",
             loosens=_changed, section="Sending"),
    ], summary=lambda v: ("Any chat" if "*" in [x.strip() for x in v("BEEPER_SEND_ALLOWLIST").split(",")]
                          else f"{len([x for x in v('BEEPER_SEND_ALLOWLIST').split(',') if x.strip()])} chats" if v("BEEPER_SEND_ALLOWLIST").strip()
                          else "Nobody may be written to"))


def _accounts(s: Any) -> Any:
    """What the service page lists: each network Beeper is signed in to (read live with the read key, kept a few minutes)."""
    reader = G.accounts_reader(s)

    def listing() -> Listing:
        got = reader.read()
        items = [{"title": n["network"], "sub": n["user"], "detail": f"Account ID {n['account_id']}"} for n in got["networks"]]
        problem = _STATES.get(got["state"], "") or ("" if items else "Beeper lists no connected network.")
        return Listing("Networks", items, problem,
                       ("Tap a network to see its account ID. " if items else "") + "New networks are added in the Beeper app.", got["state"])
    return listing


def _health(s: Any) -> dict[str, Any]:
    if not s.beeper_read_key:
        return {"ok": True, "checked": False, "note": "No read key: the gateway was not asked."}
    got = G.accounts_reader(s).read()
    return {"ok": got["state"] == "ok", "networks": len(got["networks"]), **({} if got["state"] == "ok" else {"error": _STATES.get(got["state"], got["state"])})}



def plugin() -> Plugin:
    return Plugin(
        id="beeper", name="Beeper",
        status="Set up by whoever runs this server",
        limits="Reading chats is not built yet; your assistant can only ask to send.",
        description="Chat networks such as WhatsApp, Signal and Telegram.",
        capabilities=Capabilities(messages=MessagesCapability(
            prefix="beeper",
            action_type=G.ACTION_TYPE,
            executor=lambda s: G.BeeperExecutor(G.connect(s)),
            sender=lambda s, external: G.BeeperSender(G.connect(s), external, s),
            allowed=lambda s: s.beeper_send_allowlist,
            sending_on=G.configured,
        )),
        settings_keys=frozenset({"BEEPER_SEND_ALLOWLIST"}),
        settings_group=_settings_group,
        enabled=lambda s: bool(s.beeper_gateway_url),
        secrets=lambda s: (s.beeper_exec_key, s.beeper_read_key),
        health=_health,
        accounts=_accounts,
    )
