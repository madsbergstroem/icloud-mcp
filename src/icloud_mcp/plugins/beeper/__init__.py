"""The Beeper plugin: chat networks (WhatsApp, Signal, Telegram ...) through Beeper, behind the LLM gateway.

It offers the `messages` capability for sending: a request to a `beeper:` chat is queued for the owner's approval, and after the
approval the core calls this plugin's executor once (gateway.py has the client, the action type, the executor and the sender).
Reading is the same capability's backend (reading.py): off until the owner turns it on, live through the read key, nothing
stored, with the owner's lists, maximum age and service-sender rule as for iMessage. Whoever runs the server sets it up with BEEPER_* in the environment. The owner sets
three rules on the dashboard, and a chat must pass all three: the list (BEEPER_SEND_ALLOWLIST, `*` for any chat), the networks
(BEEPER_SEND_NETWORKS, `*` for all) and group chats (BEEPER_SEND_GROUPS, off by default; a group must be on the list by its id
even then, `*` never covers one). The recipient picker names the chats on the list and finds new ones with the read key, on the
gateway's server entry BEEPER_READ_SERVER; nothing of it is stored, and the read key never sends.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from .. import Capabilities, ChatRef, Listing, MessagesCapability, Plugin
from . import gateway as G
from . import reading as R

_STATES = {"no_read_key": "Accounts not readable: no read key", "unreachable": "Gateway not reachable",
           "refused": "Accounts not readable: the gateway refused the read key", "unreadable": "Accounts not readable: unexpected answer from Beeper"}


ALLOW, NETWORKS, GROUPS = "BEEPER_SEND_ALLOWLIST", "BEEPER_SEND_NETWORKS", "BEEPER_SEND_GROUPS"


def _split(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def _settings_group() -> Any:
    from ...settings import Group, Spec, _changed, _more_history, _removes_entry, _to_false, _to_true
    return Group("beeper", "Beeper", "Chats on WhatsApp, Signal, Telegram and more that your assistant may write to, after your approval.", [
        Spec(ALLOW, "Chats that may be written to", "list", "Chat ids as Beeper gives them. Empty means nobody, * every chat except groups.",
             loosens=_changed, section="Sending"),
        Spec(NETWORKS, "Networks that may be written to", "list", "Network names as Beeper shows them. * means every network, empty none.",
             default="*", loosens=_changed, section="Sending"),
        Spec(GROUPS, "Group chats", default="false", help="Only groups on the list by name; * never covers a group.", loosens=_to_true, section="Sending"),
        Spec(R.READ, "Read chats", default="false", help="Off: your assistant sees no Beeper chats.", loosens=_to_true, section="Reading"),
        Spec(R.VISIBLE, "Only these chats", "list", "Empty means every chat except the hidden ones.", loosens=_changed, section="Reading"),
        Spec(R.HIDDEN, "Chats agents never see", "list", loosens=_removes_entry, section="Reading"),
        Spec(R.MAX_AGE, "Only messages from the last", "int", "0 means the whole history.", default="0", min=0, max=36500,
             unit_label="days", loosens=_more_history, section="Reading"),
        Spec(R.SHORT_CODES, "Hide service senders", default="true", help="Chats named by a short number: banks, deliveries, codes.",
             loosens=_to_false, section="Reading"),
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



def _policy(s: Any) -> Callable[[Mapping[str, Any]], str | None]:
    """Beyond the list (the core checks that): the chat's network must be allowed, and a group needs group chats on and its own id
    on the list. Read when a request is queued and again when it is approved; the answer is written for the agent."""
    nets = {x.casefold() for x in _split(s.plugin_value(NETWORKS, "*"))}
    groups_on = s.plugin_value(GROUPS, "false").strip().lower() in ("1", "true", "yes", "on")
    listed = {x.casefold() for x in s.beeper_send_allowlist}

    def check(p: Mapping[str, Any]) -> str | None:
        network = str(p.get("network") or "")
        if "*" not in nets and network.casefold() not in nets:
            return (f"Not permitted: writing on {network or 'this network'} is off. Tell the owner; only the owner can choose the networks "
                    "that may be written to.")
        if p.get("is_group"):
            if not groups_on:
                return "Not permitted: group chats are off. Tell the owner; only the owner can turn group chats on."
            if str(p.get("chat_id", "")).strip().casefold() not in listed:
                return "Not permitted: a group chat must be on the owner's list by name. Tell the owner; only the owner can add it."
        return None
    return check


_readers: dict[int, tuple[Any, Any]] = {}       # one chats reader per server (its answers are kept a short while)


def _chats(s: Any) -> Any:
    got = _readers.get(id(s))
    if got is None or got[0] is not s:
        got = _readers[id(s)] = (s, G.chats_reader(s))
    return got[1]


def _ref(row: Mapping[str, Any]) -> ChatRef:
    return ChatRef(row["id"], row.get("title", ""), row.get("network", ""), bool(row.get("group")), row.get("people"),
                   row.get("last_activity", ""), bool(row.get("found", True)))


def _search(s: Any, query: str) -> tuple[str, list[ChatRef]]:
    got = _chats(s).search(query, 20 if query else 10)
    return got["state"], [_ref(r) for r in got["chats"]]


def _resolve(s: Any, ids: list[str]) -> tuple[str, list[ChatRef]]:
    got = _chats(s).chats(ids)
    return got["state"], [_ref(r) for r in got["chats"]]


def _networks(s: Any) -> list[str]:
    return list(dict.fromkeys(n["network"] for n in G.accounts_reader(s).read()["networks"]))


def plugin() -> Plugin:
    return Plugin(
        id="beeper", name="Beeper",
        status="Set up by whoever runs this server",
        limits="Messages are read live and never stored. Attachments, reactions and edits are not shown.",
        description="Chat networks such as WhatsApp, Signal and Telegram.",
        capabilities=Capabilities(messages=MessagesCapability(
            prefix="beeper",
            action_type=G.ACTION_TYPE,
            executor=lambda s: G.BeeperExecutor(G.connect(s)),
            sender=lambda s, external: G.BeeperSender(G.connect(s), external, s),
            allowed=lambda s: s.beeper_send_allowlist,
            policy=_policy,
            search_chats=_search, resolve=_resolve, networks=_networks,
            recipients_key=ALLOW, networks_key=NETWORKS, groups_key=GROUPS,
            sending_on=G.configured,
            backend=R.backend, reading_on=R.reading_on,
            reading_keys={"on": R.READ, "hidden": R.HIDDEN, "visible": R.VISIBLE, "max_age": R.MAX_AGE, "short_codes": R.SHORT_CODES},
        )),
        settings_keys=frozenset({ALLOW, NETWORKS, GROUPS, R.READ, R.HIDDEN, R.VISIBLE, R.MAX_AGE, R.SHORT_CODES}),
        env_keys=frozenset({"BEEPER_READ_SERVER", "BEEPER_ACCOUNTS_TOOL"}),
        settings_group=_settings_group,
        enabled=lambda s: bool(s.beeper_gateway_url),
        secrets=lambda s: (s.beeper_exec_key, s.beeper_read_key),
        health=_health,
        accounts=_accounts,
    )
