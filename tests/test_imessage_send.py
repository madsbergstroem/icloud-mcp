"""Sending iMessages: off unless IMESSAGE_ALLOW_SEND, never to IMESSAGE_NEVER_SEND (the owner's own assistant), only to people on
IMESSAGE_SEND_ALLOWLIST (empty = nobody), and by default only after the owner approves it on /outbox, whatever the mail setting.
Approval re-checks the lists; a message the Mac never got stays queued, one it tried is never sent twice."""
import asyncio
import dataclasses
import json
import re

import httpx
import pytest

from icloud_mcp.bridge import BridgeError
from icloud_mcp.config import Settings
from icloud_mcp.imessage import IMessageError, IMessageService
from icloud_mcp.gate.actions import Rechecked
from icloud_mcp.gate.store import Store
from icloud_mcp.server import create_server

CHATS = {"chats": [{"chat_id": "anna@example.org", "participants": ["anna@example.org"], "name": ""},
                   {"chat_id": "chat900", "participants": ["anna@example.org", "+31600000002"], "name": "Hiking crew", "group": True},
                   {"chat_id": "bot@example.org", "participants": ["bot@example.org"], "name": ""}]}


def read_one(chats, args):
    """What the Mac's imessage_read answers with limit 1: the chat record (as imessage_chats has it), or the helper's error."""
    c = next((x for x in chats["chats"] if x["chat_id"] == args["chat_id"]), None)
    if c is None:
        raise BridgeError(f"no conversation with chat_id '{args['chat_id']}' (take it from imessage_list_chats)")
    return {"chat": {**c, "participants": sorted(c["participants"])}, "messages": [], "complete": True}


@pytest.fixture
def s(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path),
                     MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16, BRIDGE_TOKEN="t" * 40,
                     ENABLE_IMESSAGE="true", IMESSAGE_ALLOW_SEND="true", IMESSAGE_SEND_ALLOWLIST="anna@example.org",
                     IMESSAGE_NEVER_SEND="bot@example.org", SEND_REQUIRES_APPROVAL="false").items():
        monkeypatch.setenv(k, v)
    return Settings.from_env()


class Bridge:
    def __init__(self, send_answer=None, fail=None):
        self.calls, self.send_answer, self.fail = [], send_answer or {"status": "sent", "message_id": "m1"}, fail

    def call(self, op, args=None):
        self.calls.append((op, args))
        if op == "imessage_read":
            return read_one(CHATS, args)
        if self.fail:
            raise self.fail
        return self.send_answer


def svc(s, tmp_path, **kw):
    """The service, the fake Mac and the store, with the external source the service queues on since package 4b (m.src)."""
    from icloud_mcp.gate import actions as A
    from icloud_mcp.gate.external import ExternalApprovals
    from icloud_mcp.imessage import register_type
    b = Bridge(**kw)
    m = IMessageService(s, b, None)
    box = Store(str(tmp_path), "external", 3600, 20)
    reg = A.ActionRegistry()
    register_type(reg, m)
    m.src = ExternalApprovals(box, reg)
    return m, b, box


def send(m, text, **kw):
    """What messages_send_request does: queue on the external source (or send at once where approval is off)."""
    return m.send(text, external=m.src, **kw)


def release(m, item_id, sha256=None):
    """The owner's approval of a waiting request, as the dashboard does it; sha256: the version the owner looked at. Returns
    {"status": sent | unclear}; raises what the external source raises (ExternalError, Rechecked, Revised)."""
    from icloud_mcp.gate import actions as A
    q = next((x for x in m.src.box.pending() if x.id == item_id), None)
    if q is None:
        raise IMessageError("That message is no longer waiting.")
    try:
        got = m.src.release(dataclasses.replace(q, sha256=sha256) if sha256 else q)
    except (A.Rechecked, A.Revised):
        raise
    except A.ExternalError as e:                             # it waits again: the words the owner reads
        raise IMessageError(str(e)) from None
    return {"status": {"unconfirmed": "unclear"}.get(got.delivery, got.delivery)}


def test_approval_is_on_by_default_even_when_mail_sends_directly(s, tmp_path):
    assert s.require_approval is False and s.imessage_send_requires_approval is True
    m, b, box = svc(s, tmp_path)
    r = send(m, "Hi Anna", handle="Anna@Example.org", public_url="https://mcp.example.com")
    assert r["status"] == "queued_for_owner_approval" and r["sent"] is False and len(box.pending()) == 1
    assert "imessage_send" not in [op for op, _ in b.calls]                                     # nothing reached the Mac


def test_the_owners_assistant_is_never_a_recipient(s, tmp_path):
    for allow in (("anna@example.org",), ("*",), ("bot@example.org",)):
        m, b, box = svc(dataclasses.replace(s, imessage_send_allowlist=allow, imessage_send_requires_approval=False), tmp_path)
        with pytest.raises(IMessageError, match="IMESSAGE_NEVER_SEND"):
            send(m, "do this", handle="bot@example.org")
        with pytest.raises(IMessageError, match="IMESSAGE_NEVER_SEND"):
            send(m, "do this", chat_id="bot@example.org")
        assert not [op for op, _ in b.calls if op == "imessage_send"] and not box.pending()


def test_the_allowlist_decides_who_may_receive(s, tmp_path):
    m, b, box = svc(dataclasses.replace(s, imessage_send_allowlist=()), tmp_path)
    with pytest.raises(IMessageError, match="empty, so nobody"):
        send(m, "hi", handle="anna@example.org")
    m, b, box = svc(s, tmp_path)
    with pytest.raises(IMessageError, match=r"Not on IMESSAGE_SEND_ALLOWLIST: \+31600000002"):
        send(m, "hi all", chat_id="chat900")                                        # a group needs everyone listed
    m, b, box = svc(dataclasses.replace(s, imessage_send_allowlist=("*",), imessage_send_requires_approval=False), tmp_path)
    assert send(m, "hi all", chat_id="chat900")["status"] == "sent"
    assert b.calls[-1] == ("imessage_send", {"chat_id": "chat900", "text": "hi all"})


def test_hidden_chats_cannot_be_sent_to(s, tmp_path):
    m, b, box = svc(dataclasses.replace(s, imessage_hidden_chats=("anna@example.org",)), tmp_path)
    with pytest.raises(IMessageError, match="No conversation"):
        send(m, "hi", handle="anna@example.org")


def test_local_mode_never_sends_without_the_owner(s, tmp_path):
    m, b, _ = svc(dataclasses.replace(s, local_mode=True), tmp_path)
    r = m.send("Hi Anna", handle="anna@example.org")
    assert r["status"] == "not_sent_needs_owner" and r["text"] == "Hi Anna" and not [op for op, _ in b.calls if op == "imessage_send"]


def test_release_rechecks_the_lists_and_only_requeues_what_the_mac_never_got(s, tmp_path):
    m, b, box = svc(s, tmp_path)
    q = send(m, "Hi Anna", handle="anna@example.org")["outbox_id"]
    m.s = dataclasses.replace(s, imessage_send_allowlist=())                                    # the owner emptied the list meanwhile
    with pytest.raises(Rechecked, match="lists no longer allow") as got:      # a code, never the service's own words (4b)
        release(m, q)
    assert got.value.reason == "not_allowed" and box.state_of(q) == ("discarded", "not_allowed")
    assert not box.pending() and not [op for op, _ in b.calls if op == "imessage_send"]
    m.s = s
    q = send(m, "Hi again", handle="anna@example.org")["outbox_id"]
    m.bridge = Bridge(fail=BridgeError("The Mac did not pick up the request within 60s"))
    with pytest.raises(IMessageError, match="still waiting"):
        release(m, q)
    assert [x.id for x in box.pending()] == [q]                                                 # offline: kept for a retry
    m.bridge = Bridge(fail=BridgeError("The Mac picked up the request but did not finish within 60s"))
    assert release(m, q)["status"] == "unclear"
    assert not box.pending() and box.state_of(q)[0] == "unclear"                               # it may have gone out: never twice
    m.bridge = Bridge()
    q = send(m, "Third", handle="anna@example.org")["outbox_id"]
    assert release(m, q)["status"] == "sent" and m.bridge.calls[-1] == ("imessage_send", {"handle": "anna@example.org", "text": "Third"})


def test_the_send_tool_exists_only_when_sending_is_allowed_and_writable(s):
    def names(settings):
        async def go():
            mcp, _ = create_server(settings)
            return {t.name for t in await mcp.list_tools()}
        return asyncio.run(go())
    assert "messages_send_request" in names(s)
    assert "messages_send_request" not in names(dataclasses.replace(s, imessage_allow_send=False))
    assert "messages_send_request" not in names(dataclasses.replace(s, read_only=True))


async def test_the_outbox_page_shows_and_sends_a_queued_imessage(s, monkeypatch):
    s = dataclasses.replace(s, dashboard=False)                     # the /outbox pages exist only without the dashboard
    mcp, provider = create_server(s)
    sent = []
    mcp._icloud_bridge.call = lambda op, a=None: read_one(CHATS, a) if op == "imessage_read" else (sent.append((op, a)) or {"status": "sent"})
    r = json.loads((await mcp.call_tool("messages_send_request", {"text": "Lunch <b>tomorrow</b>?", "handle": "anna@example.org"})).content[0].text)
    assert r["status"] == "queued_for_owner_approval" and not sent
    app = mcp.streamable_http_app(host="0.0.0.0")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://mcp.example.com") as c:
            page = (await c.post("/outbox", data={"password": "x" * 16})).text
            assert "iMessage to anna@example.org" in page and "Lunch &lt;b&gt;tomorrow&lt;/b&gt;?" in page
            m = re.search(r'name="id" value="([^"]+)"><input type="hidden" name="kind" value="external"><input type="hidden" name="action" '
                          r'value="approve"><input type="hidden" name="exp" value="(\d+)"><input type="hidden" name="tok" value="([0-9a-f]+)"', page)
            forged = await c.post("/outbox/act", data={"id": m[1], "kind": "mail", "action": "approve", "exp": m[2], "tok": m[3]})
            assert forged.status_code == 400 and not sent                                         # a token for one queue fits no other
            done = await c.post("/outbox/act", data={"id": m[1], "kind": "external", "action": "approve", "exp": m[2], "tok": m[3]})
            assert "Sent" in done.text and sent == [("imessage_send", {"handle": "anna@example.org", "text": "Lunch <b>tomorrow</b>?"})]


def test_never_send_and_hidden_match_the_data_however_it_is_written(s, tmp_path):
    odd = {"chats": [{"chat_id": "Bot@Example.ORG", "participants": ["Bot@Example.ORG"], "name": ""},
                     {"chat_id": "chat901", "participants": ["anna@example.org", "+31 6 1234 5678"], "name": "", "group": True}]}
    for never in (("bot@example.org",), ("0612345678",)):
        m, b, box = svc(dataclasses.replace(s, imessage_never_send=never, imessage_send_allowlist=("*",)), tmp_path)
        b.call = lambda op, a=None, b=b: b.calls.append((op, a)) or read_one(odd, a)
        chat = "Bot@Example.ORG" if "@" in never[0] else "chat901"
        with pytest.raises(IMessageError, match="IMESSAGE_NEVER_SEND"):
            send(m, "x", chat_id=chat)
    m, _, _ = svc(dataclasses.replace(s, imessage_hidden_chats=("0612345678",)), tmp_path)
    assert not m._shown("+31612345678") and not m._shown("+31 6 12345678") and m._shown("+31612345679")


def test_release_checks_who_is_in_the_group_now(s, tmp_path):
    m, b, box = svc(dataclasses.replace(s, imessage_send_allowlist=("anna@example.org", "+31600000002")), tmp_path)
    q = send(m, "Hi all", chat_id="chat900")["outbox_id"]
    CHATS["chats"][1]["participants"].append("bot@example.org")                 # the assistant was added to the group meanwhile
    try:
        with pytest.raises(Rechecked, match="lists no longer allow"):
            release(m, q)
    finally:
        CHATS["chats"][1]["participants"].remove("bot@example.org")
    assert box.state_of(q) == ("discarded", "not_allowed")
    assert not box.pending() and not [op for op, _ in b.calls if op == "imessage_send"]


def test_the_target_is_read_as_one_chat_and_a_missing_chat_sends_nothing(s, tmp_path):
    m, b, box = svc(dataclasses.replace(s, imessage_send_allowlist=("anna@example.org", "+31600000002"),
                                        imessage_hidden_chats=("+31699999999",)), tmp_path)
    q = send(m, "Hi all", chat_id="chat900")["outbox_id"]
    assert b.calls[0] == ("imessage_read", {"chat_id": "chat900", "limit": 1, "exclude": "+31699999999"})   # hidden chats stay hidden
    assert json.loads(box.pending()[0].raw)["params"]["to"]["participants"] == [{"handle": "+31600000002"}, {"handle": "anna@example.org"}]
    with pytest.raises(BridgeError, match="no conversation"):
        send(m, "Hi", chat_id="chat404")                                       # unknown to the Mac: nothing queued
    assert len(box.pending()) == 1
    CHATS["chats"][1]["chat_id"] = "chat901"                                              # the chat is gone by the time of approval
    try:
        with pytest.raises(Rechecked, match="target changed"):
            release(m, q)
    finally:
        CHATS["chats"][1]["chat_id"] = "chat900"
    assert box.state_of(q) == ("discarded", "target_changed")
    assert not box.pending() and not [op for op, _ in b.calls if op == "imessage_send"]  # dropped, not put back


def test_the_target_gets_the_same_participants_as_the_chat_list(s, tmp_path):
    """imessage_read's chat record and imessage_chats' entry come from the same chat_groups() on the Mac: _check_chat and
    _permit see the same chat_id, name, group and participants either way, at queue time and at release."""
    m, b, box = svc(dataclasses.replace(s, imessage_send_allowlist=("*",)), tmp_path)
    for c in CHATS["chats"]:
        t = m._target(c["chat_id"], None)
        assert (t["chat_id"], t["name"], t["group"], t["participants"]) == (c["chat_id"], c["name"], c.get("group", False),
                                                                           sorted(c["participants"]))


def test_an_imessage_waits_whatever_rules_exist_and_pushes_once(s, tmp_path):
    """protects=criterion 9 for messages_send_request on iMessage: it answers through gate.submit and, without standing_identity,
    always waits with one push; fails_when=a rule releases it or the push is lost; no_claim=none beyond protects."""
    from icloud_mcp.gate.standing import Standing
    m, b, box = svc(s, tmp_path)
    m.src.standing = Standing(box)
    m.src.standing.add("imessage.send", "anna@example.org", 30)
    pushes = []
    box.on_new = pushes.append
    got = send(m, "See you at noon.", handle="anna@example.org")
    assert got["status"] == "queued_for_owner_approval" and [q.id for q in pushes] == [got["outbox_id"]]
    assert [op for op, _ in b.calls if op == "imessage_send"] == []
