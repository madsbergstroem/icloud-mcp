"""Service plugins (plugins/__init__.py): two made-up plugins come in through the entry point group `icloud_mcp.plugins`, with no
change to the core. "Chatty" offers messages: its chats join messages_list_chats under its own prefix, and a message to one of its
rooms waits for the owner's approval, is checked against its list and its policy at queue and at approval, and is carried out once.
"Mailbox" declares mail with a stub. Both show up under Connected services and on Connect a service. Every name is made up."""
import json

import pytest

from icloud_mcp import actions as A
from icloud_mcp import messages as M
from icloud_mcp import plugins as P
from test_dashboard import api, client, signed_in, web  # noqa: F401  (web is a fixture)
from test_external_approvals import url

CHATTY_TYPE = A.ActionType(
    id="chatty.message.send", title="Chatty message", badge="Chatty", approve_label="Send",
    params_schema={"type": "object", "properties": {"chat_id": {"type": "string"}, "text": {"type": "string"}, "room": {"type": "string"}},
                   "required": ["chat_id", "text", "room"], "additionalProperties": False},
    rows=(("Room", "room"),), text_field="text", recipients=lambda p: [p["chat_id"]])
CARRIED_OUT: list[dict] = []


class ChattyExecutor:
    def execute(self, request):
        CARRIED_OUT.append(dict(request.params))
        return A.Result("sent", "Sent. Chatty accepted it.")


class ChattySender:
    name, takes_handle = "chatty", False

    def __init__(self, external):
        self.external = external

    def send_request(self, req):
        q = self.external.queue_external(CHATTY_TYPE.id, {"chat_id": req.chat_id, "text": req.text, "room": "Garden club"}, None)
        return {"status": "queued_for_owner_approval", "sent": False, "outbox_id": q.id}


class ChattyBackend:
    name = "chatty"

    def accounts(self):
        return [M.Account("chatty-1", "Chatty", "chatty")]

    def list_chats(self, flt):
        return [M.Chat("chatty:room-1", "Garden club", "group", "chatty-1", last_at="2026-11-12T08:00:00", last_text="Seedlings on Saturday?")]

    def read_chat(self, chat_id, limit, cursor):
        raise M.MessagesError("not in this test")

    def search(self, query, chat_id, since, before, limit):
        return M.Matches(())

    def filters(self):
        return M.Filters()


def _chatty_group():
    from icloud_mcp.settings import Group, Spec, _changed
    return Group("chatty", "Chatty", "Rooms on Chatty your assistant may write to, after your approval.", [
        Spec("CHATTY_SEND_ALLOWLIST", "Rooms that may be written to", "list", loosens=_changed, section="Sending")])


def chatty_plugin():
    return P.Plugin(
        id="chatty", name="Chatty", status="Set up by whoever runs this server", limits="Chatty cannot read old rooms.",
        description="Rooms on Chatty, a made-up chat service.",
        capabilities=P.Capabilities(messages=P.MessagesCapability(
            prefix="chatty", action_type=CHATTY_TYPE, executor=lambda s: ChattyExecutor(), sender=lambda s, ext: ChattySender(ext),
            backend=lambda s: ChattyBackend(),
            allowed=lambda s: [x for x in s.plugin_value("CHATTY_SEND_ALLOWLIST").split(",") if x.strip()],
            policy=lambda s: (lambda p: "Not permitted: quiet hours." if "[quiet]" in p["text"] else None),
            sending_on=lambda s: True)),
        settings_keys=frozenset({"CHATTY_SEND_ALLOWLIST"}), settings_group=_chatty_group,
        enabled=lambda s: True, secrets=lambda s: ("chatty-secret-token-123",),
        health=lambda s: {"rooms": 1},
        accounts=lambda s: (lambda: P.Listing("Rooms", [{"title": "Garden club", "sub": "group of 4"}])))


class StubMail:
    def list_folders(self):
        return []

    def search_messages(self, folder, query, limit):
        return []

    def get_message(self, folder, uid):
        return {}

    def send_raw(self, raw, recipients):
        return {}


def mailbox_plugin():
    return P.Plugin(id="mailbox", name="Mailbox", status="Set up by whoever runs this server", limits="Mail only, no calendar.",
                    description="A made-up mail service.", capabilities=P.Capabilities(mail=lambda s: StubMail()), enabled=lambda s: True)


@pytest.fixture
def two_plugins(monkeypatch):
    CARRIED_OUT.clear()
    monkeypatch.setattr(P, "entry_points", lambda: [("chatty", "test_plugins:chatty_plugin"), ("mailbox", "test_plugins:mailbox_plugin")])


def test_plugins_come_from_the_entry_points_after_the_built_in_ones(two_plugins):
    assert [p.id for p in P.load_plugins()] == ["beeper", "chatty", "mailbox"]
    assert isinstance(StubMail(), P.MailCapability) and P.load_plugins()[2].capabilities.names() == ["mail"]
    assert "CHATTY_SEND_ALLOWLIST" in P.settings_keys()


def test_a_broken_plugin_is_skipped_and_the_rest_load(monkeypatch):
    monkeypatch.setattr(P, "entry_points", lambda: [("gone", "no_such_module:plugin"), ("mailbox", "test_plugins:mailbox_plugin")])
    assert [p.id for p in P.load_plugins()] == ["beeper", "mailbox"]


async def test_both_show_under_connected_services_and_on_connect(web, two_plugins):
    app, *_ = web(CHATTY_SEND_ALLOWLIST="room-1")
    async with client(app) as c:
        await signed_in(c)
        r = (await c.get("/dashboard/api/accounts")).json()
        st = (await c.get("/dashboard/api/settings")).json()
    got = {p["id"]: p for p in r["plugins"]}
    assert (got["chatty"]["enabled"], got["mailbox"]["enabled"], got["beeper"]["enabled"]) == (True, True, False)
    assert got["chatty"]["listing"]["items"] == [{"title": "Garden club", "sub": "group of 4"}]
    assert got["chatty"]["messages"] == {"prefix": "chatty", "sending": True, "reading": True, "settings": "chatty"}
    assert got["mailbox"]["capabilities"] == ["mail"] and "messages" not in got["mailbox"]
    assert next(g for g in st["groups"] if g["slug"] == "chatty")["fields"][0]["value"] == "room-1"     # its settings, drawn by the core


async def test_its_chats_join_the_chat_tools_under_its_own_prefix(web, two_plugins):
    _app, mcp, *_ = web(CHATTY_SEND_ALLOWLIST="room-1")
    got = json.loads((await mcp.call_tool("messages_list_chats", {})).content[0].text)
    assert [(c["chat_id"], c["title"]) for c in got["chats"]] == [("chatty:room-1", "Garden club")]
    health = json.loads((await mcp.call_tool("icloud_check_health", {})).content[0].text)
    assert health["areas"]["chatty"]["ok"] is True and health["areas"]["chatty"]["rooms"] == 1


async def test_a_message_waits_for_approval_passes_list_and_policy_and_is_carried_out_once(web, two_plugins):
    app, mcp, *_ = web(CHATTY_SEND_ALLOWLIST="room-1")
    ask = lambda chat, text: mcp.call_tool("messages_send_request", {"chat_id": f"chatty:{chat}", "text": text})   # noqa: E731
    with pytest.raises(Exception, match="Not permitted for chatty.message.send: room-2"):
        await ask("room-2", "Hello")                                                       # not on its list
    with pytest.raises(Exception, match="quiet hours"):
        await ask("room-1", "Hello [quiet]")                                               # its policy
    got = json.loads((await ask("room-1", "Seedlings on Saturday at ten")).content[0].text)
    assert got["status"] == "queued_for_owner_approval" and CARRIED_OUT == []            # nothing carried out before the owner approves
    src = mcp._icloud_dashboard.sources["external"]
    (q,) = src.pending()
    async with client(app) as c:
        token = await signed_in(c)
        r = await c.post(url(q.id) + "/approve", json={"sha": q.sha256}, headers=api(token))
        assert r.status_code == 200, r.text
        again = await c.post(url(q.id) + "/approve", json={"sha": q.sha256}, headers=api(token))
    assert again.status_code in (404, 409) and CARRIED_OUT == [{"chat_id": "room-1", "text": "Seedlings on Saturday at ten", "room": "Garden club"}]
    from icloud_mcp import server as S
    assert "chatty-secret-token-123" in S._error_secrets
    log = (mcp._icloud_dashboard.audit.path.read_text())
    assert "Seedlings" not in log                                                         # the log holds metadata only
