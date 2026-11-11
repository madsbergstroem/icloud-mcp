"""`messages_send_request`: the one tool that asks the owner to approve a message, for iMessage and for Beeper chats. Every check the
two tools it replaced had stays here under the new name: the owner's lists, the assistant that is never written to, an SMS-only
chat, local mode, approval, the owner's edit, taking a request back, what approval_status says, a pause, a log with no content. On
top of that: the id prefix picks the channel, and the old tool names are gone from everything an agent reads. What is stored did
not change; tests/test_send_requests_golden.py pins that."""
import asyncio
import copy
import dataclasses
import json

import pytest
from mcp import Client
from mcp.types import Implementation

import messages_fakes as g
from icloud_mcp.bridge import BridgeError
from icloud_mcp.config import Settings
from icloud_mcp.contacts import ContactsService
from icloud_mcp.server import create_server
from test_beeper import GROUP, KEY, SINGLE, URL, WORDS, gateway, hub  # noqa: F401  (fixtures)
from test_dashboard import api, client, signed_in, web  # noqa: F401  (web is a fixture)
from test_tool_surface import served_text

CHATS = {"chats": [
    {"chat_id": "chat900", "name": "Hiking crew", "group": True, "participants": ["+31600000002", "anna@example.org"], "services": ["iMessage"]},
    {"chat_id": "anna@example.org", "name": "", "group": False, "participants": ["anna@example.org"], "services": ["iMessage", "SMS"]},
    {"chat_id": "bot@example.org", "name": "", "group": False, "participants": ["bot@example.org"], "services": ["iMessage"]},
    {"chat_id": "secret@example.org", "name": "", "group": False, "participants": ["secret@example.org"], "services": ["iMessage"]},
    {"chat_id": "+31611111111", "name": "", "group": False, "participants": ["+31611111111"], "services": ["SMS"]},
]}
ENV = dict(ENABLE_IMESSAGE="true", IMESSAGE_ALLOW_SEND="true", IMESSAGE_SEND_ALLOWLIST="anna@example.org,+31600000002,+31611111111",
           IMESSAGE_NEVER_SEND="bot@example.org", IMESSAGE_HIDDEN_CHATS="secret@example.org", BRIDGE_TOKEN="t" * 40)
BEEPER = dict(BEEPER_GATEWAY_URL=URL, BEEPER_EXEC_KEY=KEY, BEEPER_SEND_ALLOWLIST="*")
TEXT = "Running late, start without me. ÜBER-café"


class Mac:
    """A made-up Mac helper for the send path: the conversations above, a send that can end in any way, every call written down."""

    def __init__(self):
        self.calls, self.send_status = [], "sent"

    def call(self, op, args=None):
        self.calls.append((op, copy.deepcopy(args)))
        if op == "imessage_read":
            c = next((x for x in CHATS["chats"] if x["chat_id"] == args["chat_id"]), None)
            if c is None:
                raise BridgeError(f"no conversation with chat_id '{args['chat_id']}' (take it from imessage_list_chats)")
            return {"chat": {**c, "participants": sorted(c["participants"])}, "messages": [], "complete": True}
        if op == "imessage_chats":
            return copy.deepcopy(CHATS)
        if op == "imessage_search":
            if args.get("chat_id") and not any(x["chat_id"] == args["chat_id"] for x in CHATS["chats"]):
                raise BridgeError(f"no conversation with chat_id '{args['chat_id']}'")
            return {"matches": [], "scanned": 0, "complete": True}
        if op == "imessage_send":
            return {"status": self.send_status, "message_id": "m1", "delivered": True}
        raise AssertionError(op)

    def sent(self):
        return [a for op, a in self.calls if op == "imessage_send"]


def launch(web, monkeypatch, **extra):
    monkeypatch.setattr(ContactsService, "_people", lambda self: [])
    app, mcp, *_ = web(**{**ENV, **extra})
    mac = Mac()
    if hasattr(mcp, "_icloud_bridge"):                                  # a server with no Mac area on has no helper to ask
        mcp._icloud_bridge.call = mac.call
    return app, mcp, mac


async def call(mcp, **args):
    return json.loads((await mcp.call_tool("messages_send_request", args)).content[0].text)


async def refused(mcp, **args):
    with pytest.raises(Exception) as e:
        await mcp.call_tool("messages_send_request", args)
    return str(e.value)


def waiting(mcp, kind="imessage"):
    return mcp._icloud_outboxes[kind].pending()


async def approve(app, key, edit=None):
    async with client(app) as c:
        h = api(await signed_in(c))
        sha = (await c.get(f"/dashboard/api/items/{key}")).json()["sha"]
        if edit is not None:
            r = await c.put(f"/dashboard/api/items/{key}/text", json={"sha": sha, "text": edit}, headers=h)
            assert r.status_code == 200, r.text
            sha = (await c.get(f"/dashboard/api/items/{key}")).json()["sha"]
        r = await c.post(f"/dashboard/api/items/{key}/approve", json={"sha": sha}, headers=h)
        return r.status_code, r.json()


# -- asking ------------------------------------------------------------------------------------------------------------------
async def test_a_message_to_an_address_or_to_a_chat_waits_for_the_owner_and_nothing_reaches_the_mac(web, monkeypatch):
    app, mcp, mac = launch(web, monkeypatch)
    by_address = await call(mcp, text=TEXT, handle="Anna@Example.org")
    in_group = await call(mcp, text=TEXT, chat_id="imessage:chat900")
    for got in (by_address, in_group):
        assert got["status"] == "queued_for_owner_approval" and got["sent"] is False
        assert got["approve_at"].endswith(f"/dashboard#/item/imessage:{got['outbox_id']}") and "approval_status" in got["notice"]
    assert by_address["to"]["handle"] == "anna@example.org" and in_group["to"]["chat_id"] == "chat900" and in_group["to"]["group"] is True
    assert len(waiting(mcp)) == 2 and mac.sent() == []


async def test_an_identical_request_that_still_waits_is_the_same_request(web, monkeypatch):
    _, mcp, _ = launch(web, monkeypatch)
    first = await call(mcp, text=TEXT, handle="anna@example.org")
    again = await call(mcp, text=TEXT, handle="anna@example.org")
    other = await call(mcp, text=TEXT + "!", handle="anna@example.org")
    assert again["outbox_id"] == first["outbox_id"] and other["outbox_id"] != first["outbox_id"] and len(waiting(mcp)) == 2


async def test_the_owners_lists_decide_who_may_be_asked_for(web, monkeypatch):
    app, mcp, mac = launch(web, monkeypatch)
    assert "IMESSAGE_NEVER_SEND" in await refused(mcp, text="do this", chat_id="imessage:bot@example.org")
    assert "IMESSAGE_NEVER_SEND" in await refused(mcp, text="do this", handle="bot@example.org")
    assert "No conversation" in await refused(mcp, text="hi", chat_id="imessage:secret@example.org")          # hidden: the same as one that is not there
    assert "No conversation" in await refused(mcp, text="hi", handle="secret@example.org")
    assert "No conversation" in await refused(mcp, text="hi", chat_id="imessage:nobody@example.org")
    assert waiting(mcp) == [] and mac.sent() == []


async def test_a_group_needs_every_participant_on_the_list_and_an_empty_list_means_nobody(web, monkeypatch):
    _, mcp, _ = launch(web, monkeypatch, IMESSAGE_SEND_ALLOWLIST="anna@example.org")
    assert "Not on IMESSAGE_SEND_ALLOWLIST: +31600000002" in await refused(mcp, text="hi all", chat_id="imessage:chat900")
    _, mcp, _ = launch(web, monkeypatch, IMESSAGE_SEND_ALLOWLIST="")
    assert "empty, so nobody" in await refused(mcp, text="hi", handle="anna@example.org")
    _, mcp, _ = launch(web, monkeypatch, IMESSAGE_SEND_ALLOWLIST="*")
    assert (await call(mcp, text="hi all", chat_id="imessage:chat900"))["status"] == "queued_for_owner_approval"
    assert waiting(mcp) != []


async def test_an_sms_only_chat_is_refused_and_the_hint_is_a_chat_id_the_tool_takes(web, monkeypatch):
    _, mcp, mac = launch(web, monkeypatch)
    msg = await refused(mcp, text="hi", chat_id="imessage:+31611111111")
    assert "exists only as SMS" in msg and "Nothing was sent or queued" in msg
    assert waiting(mcp) == [] and mac.sent() == []


async def test_only_what_a_request_for_beeper_can_use_is_accepted_for_imessage(web, monkeypatch):
    _, mcp, _ = launch(web, monkeypatch)
    for extra in ({"reply_to": "msg-1"}, {"request_id": "ask-1"}):
        assert "Beeper chats only" in await refused(mcp, text="hi", handle="anna@example.org", **extra)
    assert waiting(mcp) == []


async def test_the_chat_id_and_the_handle_come_alone(web, monkeypatch):
    _, mcp, _ = launch(web, monkeypatch)
    assert "exactly one of chat_id" in await refused(mcp, text="hi", chat_id="imessage:chat900", handle="anna@example.org")
    assert "exactly one of chat_id" in await refused(mcp, text="hi")
    assert "starts with its backend" in await refused(mcp, text="hi", chat_id="chat900")
    assert waiting(mcp) == []


async def test_the_mac_saying_there_is_no_conversation_is_reworded_to_name_a_tool_that_exists(web, monkeypatch):
    _, mcp, _ = launch(web, monkeypatch, IMESSAGE_SEND_ALLOWLIST="*", IMESSAGE_HIDE_SHORT_CODES="false")
    for name, args in (("messages_send_request", {"text": "hi", "chat_id": "imessage:chat404"}),
                       ("messages_read_chat", {"chat_id": "imessage:chat404"}), ("messages_search", {"query": "x", "chat_id": "imessage:chat404"})):
        with pytest.raises(Exception) as e:
            await mcp.call_tool(name, args)
        assert "messages_list_chats" in str(e.value) and "imessage_list_chats" not in str(e.value), name


def test_local_mode_never_sends_without_the_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(ContactsService, "_people", lambda self: [])
    for k, v in {**g.BASE_ENV, **ENV, "DATA_DIR": str(tmp_path)}.items():
        monkeypatch.setenv(k, v)
    mcp, provider = create_server(dataclasses.replace(Settings.from_env(), local_mode=True))
    assert provider is None
    mac = Mac()
    mcp._icloud_bridge.call = mac.call
    got = asyncio.run(call(mcp, text="Hi Anna", handle="anna@example.org"))
    assert got["status"] == "not_sent_needs_owner" and got["text"] == "Hi Anna" and mac.sent() == []


# -- what happens to a request after it is asked for -----------------------------------------------------------------------------
async def test_approving_sends_once_and_approval_status_says_so(web, monkeypatch):
    app, mcp, mac = launch(web, monkeypatch)
    got = await call(mcp, text=TEXT, handle="anna@example.org")
    code, body = await approve(app, f"imessage:{got['outbox_id']}")
    assert code == 200 and mac.sent() == [{"handle": "anna@example.org", "text": TEXT}]
    status = json.loads((await mcp.call_tool("approval_status", {"id": got["outbox_id"]})).content[0].text)
    assert status["kind"] == "imessage" and status["status"] == "sent" and TEXT not in json.dumps(status)
    assert waiting(mcp) == []


async def test_a_send_the_mac_did_not_confirm_is_unclear_and_never_repeated(web, monkeypatch):
    app, mcp, mac = launch(web, monkeypatch)
    mac.send_status = "unconfirmed"
    got = await call(mcp, text=TEXT, handle="anna@example.org")
    await approve(app, f"imessage:{got['outbox_id']}")
    status = json.loads((await mcp.call_tool("approval_status", {"id": got["outbox_id"]})).content[0].text)
    assert status["status"] == "unclear" and len(mac.sent()) == 1 and waiting(mcp) == []


async def test_what_the_owner_edits_is_what_is_sent_and_approval_status_says_it_was_edited(web, monkeypatch):
    app, mcp, mac = launch(web, monkeypatch)
    got = await call(mcp, text=TEXT, handle="anna@example.org")
    key = f"imessage:{got['outbox_id']}"
    async with client(app) as c:
        h = api(await signed_in(c))
        sha = (await c.get(f"/dashboard/api/items/{key}")).json()["sha"]
        assert (await c.put(f"/dashboard/api/items/{key}/text", json={"sha": sha, "text": "Make it nine"}, headers=h)).status_code == 200
    assert json.loads((await mcp.call_tool("approval_status", {"id": got["outbox_id"]})).content[0].text)["edited"] is True
    await approve(app, key)
    assert mac.sent() == [{"handle": "anna@example.org", "text": "Make it nine"}]


async def test_the_agent_can_take_back_what_it_asked_for_and_only_the_client_that_asked_can(web, monkeypatch):
    app, mcp, mac = launch(web, monkeypatch)

    async def as_client(name, tool, args):
        async with Client(mcp, client_info=Implementation(name=name, version="1.0"), mode="legacy") as c:
            return await c.call_tool(tool, args)

    got = json.loads((await as_client("agent-a", "messages_send_request", {"text": TEXT, "handle": "anna@example.org"})).content[0].text)
    assert waiting(mcp)[0].requested_by == "agent-a"
    other = await as_client("agent-b", "approval_withdraw", {"id": got["outbox_id"]})
    assert "Another client queued this request" in other.content[0].text and len(waiting(mcp)) == 1
    back = json.loads((await as_client("agent-a", "approval_withdraw", {"id": got["outbox_id"], "reason": "mistake"})).content[0].text)
    assert back == {"id": got["outbox_id"], "withdrawn": True, "status": "withdrawn"} and waiting(mcp) == [] and mac.sent() == []


async def test_a_pause_stops_the_tool_and_the_owner_can_resume(web, monkeypatch, tmp_path):
    app, mcp, mac = launch(web, monkeypatch)
    (tmp_path / "paused").write_text("")
    assert "paused" in (await refused(mcp, text=TEXT, handle="anna@example.org")).lower() and waiting(mcp) == []
    assert json.loads((await mcp.call_tool("approval_status", {"id": "none"})).content[0].text)["status"] == "unknown"     # a read: it answers
    (tmp_path / "paused").unlink()
    assert (await call(mcp, text=TEXT, handle="anna@example.org"))["status"] == "queued_for_owner_approval"


async def test_the_activity_log_and_the_push_note_hold_no_text_and_no_chat_id(web, monkeypatch, caplog):
    import logging
    app, mcp, mac = launch(web, monkeypatch)
    pushed = []
    mcp._icloud_dashboard.push.notify = lambda kind, key="": pushed.append((kind, key))
    caplog.set_level(logging.DEBUG)
    got = await call(mcp, text=TEXT, chat_id="imessage:chat900")
    await refused(mcp, text=TEXT, chat_id="imessage:bot@example.org")
    audit = mcp._icloud_dashboard.audit.path.read_text()
    assert '"tool.queued"' in audit and "messages_send_request" in audit                            # the request is on record ...
    for secret in ("Running late", "ÜBER", "chat900", "Hiking crew"):                                   # ... and nothing of what it says
        assert secret not in audit and secret not in caplog.text, secret
    assert pushed == [("imessage", f"imessage:{got['outbox_id']}")]                                 # kind and id, no words


# -- the id prefix picks the channel ---------------------------------------------------------------------------------------------
async def test_the_prefix_sends_a_request_to_its_own_approval_flow_and_the_other_channel_is_not_touched(web, hub, gateway, monkeypatch):
    app, mcp, mac = launch(web, monkeypatch, **BEEPER)
    to_beeper = await call(mcp, text=WORDS, chat_id=f"beeper:{SINGLE}")
    to_phone = await call(mcp, text=TEXT, chat_id="imessage:chat900")
    assert to_beeper["approve_at"].endswith(f"#/item/external:{to_beeper['outbox_id']}") and to_beeper["chat"]["title"] == "Example Person"
    assert to_phone["approve_at"].endswith(f"#/item/imessage:{to_phone['outbox_id']}")
    assert [q.recipients for q in waiting(mcp, "external")] == [[SINGLE]]                           # Beeper gets the id without its prefix
    assert gateway.chat_calls == [{"chatID": SINGLE, "maxParticipantCount": 0}] and gateway.sent == []
    assert len(waiting(mcp)) == 1 and mac.sent() == []


async def test_a_beeper_chat_does_not_take_an_address_a_reply_or_a_key_from_imessage(web, hub, gateway, monkeypatch):
    app, mcp, _ = launch(web, monkeypatch, ENABLE_IMESSAGE="false", IMESSAGE_ALLOW_SEND="false", **BEEPER)
    assert "No channel here takes an address" in await refused(mcp, text="hi", handle="anna@example.org")
    assert "Sending to 'imessage' chats is not turned on here (it is on for: beeper)" in await refused(mcp, text="hi", chat_id="imessage:chat900")
    assert "Sending to 'other' chats is not turned on" in await refused(mcp, text="hi", chat_id="other:!x:example.org")
    got = await call(mcp, text=WORDS, chat_id=f"beeper:{SINGLE}", reply_to="msg-1", request_id="ask-1")
    again = await call(mcp, text=WORDS, chat_id=f"beeper:{SINGLE}", reply_to="msg-1", request_id="ask-1")
    assert again["outbox_id"] == got["outbox_id"] and again["already_queued"] is True


async def test_a_beeper_chat_can_be_written_to_but_not_read_yet(web, hub, gateway, monkeypatch):
    _, mcp, mac = launch(web, monkeypatch, **BEEPER)
    with pytest.raises(Exception, match="can be written to .* but not read here yet"):
        await mcp.call_tool("messages_read_chat", {"chat_id": f"beeper:{SINGLE}"})
    assert mac.calls == []


async def test_without_imessage_sending_the_imessage_prefix_is_not_a_way_in(web, monkeypatch):
    _, mcp, _ = launch(web, monkeypatch, IMESSAGE_ALLOW_SEND="false")
    assert "messages_send_request" not in {t.name for t in await mcp.list_tools()}


# -- the old names are gone ---------------------------------------------------------------------------------------------------------
REMOVED = {"imessage_list_chats", "imessage_read_chat", "imessage_search_messages", "imessage_send_message", "beeper_send_request"}


async def test_the_removed_tools_are_in_neither_the_list_nor_anything_an_agent_reads(web, hub, gateway, monkeypatch):
    import re
    _, mcp, _ = launch(web, monkeypatch, ENABLE_CONTACTS="true", **BEEPER)
    tools = {t.name for t in await mcp.list_tools()}
    assert {"messages_list_chats", "messages_read_chat", "messages_search", "messages_send_request"} <= tools and not tools & REMOVED
    text = await asyncio.to_thread(served_text, mcp)
    assert not set(re.findall(r"\b(?:" + "|".join(sorted(REMOVED)) + r")\b", text))
    assert "Write with messages_send_request only to a chat or person the owner named here" in mcp.instructions
    assert "messages_list_chats, then messages_read_chat" in mcp.instructions and "messages_search (find words)" in mcp.instructions


async def test_a_server_that_can_only_write_to_beeper_still_tells_the_agent_how(web, hub, gateway, monkeypatch):
    _, mcp, _ = launch(web, monkeypatch, ENABLE_IMESSAGE="false", IMESSAGE_ALLOW_SEND="false", **BEEPER)
    tools = {t.name for t in await mcp.list_tools()}
    assert "messages_send_request" in tools and not tools & REMOVED and "messages_list_chats" not in tools
    assert "Write with messages_send_request only to a chat or person the owner named here" in mcp.instructions
    assert "messages_list_chats" not in mcp.instructions                                   # no rule names a tool this server does not have


async def test_the_chat_prompt_names_the_new_tools(web, monkeypatch):
    _, mcp, _ = launch(web, monkeypatch)
    text = str(await mcp.get_prompt("catch_up_on_messages", {}))
    assert "messages_list_chats" in text and "messages_read_chat" in text and "imessage_" not in text
