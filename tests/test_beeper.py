"""Beeper messages through the approval hub (beeper.py). A gateway of our own, made for the test, stands in for LiteLLM and Beeper: a
real MCP server in this process with the two tools, a bearer key, and ways to fail. Chats, titles and texts are made up; nothing here
reaches anything outside the process."""
import json
import logging
import time

import httpx2
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from icloud_mcp import actions as A
from icloud_mcp import beeper as B
from icloud_mcp.config import Settings
from test_dashboard import api, client, signed_in, web  # noqa: F401  (web is a fixture)
from test_external_approvals import audit_text, tool, url

KEY = "exec-key-0123456789"
URL = "http://gateway.test/mcp"
SINGLE, GROUP = "!single:beeper.test", "!group:beeper.test"
WORDS = "See you at the north gate at nine"


class FakeGateway:
    """A gateway with Beeper behind it. `mode` makes it fail: 'refuse' (401 for everything), 'forbid' (403 for everything),
    'forbid_call' (403 only for a tool call: the session starts, the call is turned away), 'down' (no connection), 'lose' (the send
    happens, the answer is lost: a 502)."""

    def __init__(self):
        self.mode = None
        self.sent: list[dict] = []
        self.chat_calls: list[dict] = []
        self.seen_keys: list[str] = []
        self.chats = {
            SINGLE: {"id": SINGLE, "title": "Example Person", "type": "single", "network": "WhatsApp", "accountID": "wa",
                     "participants": {"items": [], "hasMore": False, "total": 2}},
            GROUP: {"id": GROUP, "title": "Example Club", "type": "group", "network": "Signal", "accountID": "sg",
                    "participants": {"items": [], "hasMore": False, "total": 12}},
        }
        self.send_error = False
        self.slow = 0.0

    def new_app(self):
        """A fresh MCP server: the client of beeper.py runs every call in an event loop of its own, and a server's session manager
        cannot move between loops."""
        server = MCPServer("fake-gateway")

        @server.tool(name="beeper-get_chat")
        def get_chat(chatID: str, maxParticipantCount: int = -1) -> dict:
            self.chat_calls.append({"chatID": chatID, "maxParticipantCount": maxParticipantCount})
            if chatID not in self.chats:
                raise ToolError("Chat not found")
            return self.chats[chatID]

        @server.tool(name="beeper_send-send_message")
        def send_message(chatID: str, text: str = "", replyToMessageID: str = "") -> str:
            self.sent.append({"chatID": chatID, "text": text, "replyToMessageID": replyToMessageID})
            if self.slow:
                time.sleep(self.slow)                             # the send is done; the answer is late
            if self.send_error:
                raise ToolError("Could not send")
            return json.dumps({"chatID": chatID, "pendingMessageID": "pending-1", "deeplink": "beeper://chat/example"})

        return server.streamable_http_app(stateless_http=True, json_response=True,
                                          transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    async def serve(self, scope, receive, send):
        app = self.new_app()
        async with app.router.lifespan_context(app):
            await app(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.serve(scope, receive, send)
        key = dict(scope["headers"]).get(b"authorization", b"").decode()
        self.seen_keys.append(key)
        body, more = b"", True
        while more:
            m = await receive()
            body, more = body + m.get("body", b""), m.get("more_body", False)
        is_call = b'"tools/call"' in body
        status = 401 if self.mode == "refuse" or key != f"Bearer {KEY}" else 403 if self.mode == "forbid" or (self.mode == "forbid_call" and is_call) else None
        if status:
            await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
            return await send({"type": "http.response.body", "body": b'{"error":"no"}'})
        replay = [{"type": "http.request", "body": body, "more_body": False}]

        async def again():
            return replay.pop(0) if replay else {"type": "http.disconnect"}

        async def drop(m):
            pass

        lose = self.mode == "lose" and b"beeper_send-send_message" in body
        await self.serve(scope, again, drop if lose else send)
        if lose:                                              # the send was done; what comes back is lost
            await send({"type": "http.response.start", "status": 502, "headers": []})
            await send({"type": "http.response.body", "body": b""})

    def make_http(self, headers, timeout):
        class Down(httpx2.AsyncBaseTransport):
            async def handle_async_request(self, request):
                raise httpx2.ConnectError("no route")
        transport = Down() if self.mode == "down" else httpx2.ASGITransport(app=self)
        return httpx2.AsyncClient(transport=transport, base_url="http://gateway.test", headers=headers, timeout=timeout)


@pytest.fixture
def gateway(monkeypatch):
    g = FakeGateway()
    monkeypatch.setattr(B, "connect", lambda s: B.Beeper(B.Gateway(s.beeper_gateway_url, s.beeper_exec_key, 5, make_http=g.make_http),
                                                         s.beeper_chat_tool, s.beeper_send_tool))
    return g


@pytest.fixture
def hub(monkeypatch):
    reg = A.ActionRegistry()
    monkeypatch.setattr(A, "REGISTRY", reg)
    return reg


def launch(web, hub, *, allow=f"{SINGLE},{GROUP}", **env):
    app, mcp, _p, _sent = web(BEEPER_GATEWAY_URL=URL, BEEPER_EXEC_KEY=KEY, BEEPER_SEND_ALLOWLIST=allow, **env)
    return app, mcp, mcp._icloud_dashboard.sources["external"]


async def ask(mcp, chat_id=SINGLE, text=WORDS, **kw):
    return json.loads((await mcp.call_tool("beeper_send_request", {"chat_id": chat_id, "text": text, **kw})).content[0].text)


# -- switched on only when set up ------------------------------------------------------------------------------------------------
async def test_without_the_gateway_settings_there_is_no_tool_and_no_source(web, hub):
    app, mcp, *_ = web()
    assert "beeper_send_request" not in [t.name for t in mcp._tool_manager.list_tools()]
    assert "external" not in mcp._icloud_dashboard.sources


@pytest.mark.parametrize("missing", ["BEEPER_GATEWAY_URL", "BEEPER_EXEC_KEY"])
async def test_the_gateway_address_and_the_key_are_both_needed(web, hub, missing):
    env = {"BEEPER_GATEWAY_URL": URL, "BEEPER_EXEC_KEY": KEY, "BEEPER_SEND_ALLOWLIST": "*"}
    env.pop(missing)
    app, mcp, *_ = web(**env)
    assert "beeper_send_request" not in [t.name for t in mcp._tool_manager.list_tools()]


async def test_with_the_settings_the_tool_and_the_source_appear_and_the_shared_registry_stays_empty(web, hub, gateway):
    app, mcp, src = launch(web, hub)
    assert "beeper_send_request" in [t.name for t in mcp._tool_manager.list_tools()]
    assert src.registry.get(B.ACTION_ID) is not None and hub.types() == []       # the server's own copy took the type
    assert src.box.path.name == "external-outbox.json"


# -- asking ----------------------------------------------------------------------------------------------------------------------------
async def test_a_request_waits_with_the_chat_as_beeper_describes_it_and_sends_nothing(web, hub, gateway):
    app, mcp, src = launch(web, hub)
    got = await ask(mcp)
    assert got["status"] == "queued_for_owner_approval" and got["sent"] is False and got["chat"] == {
        "title": "Example Person", "network": "WhatsApp", "group": False, "people": 2}
    assert got["approve_at"].endswith(f"/dashboard#/item/external:{got['outbox_id']}") and got["expires_in_seconds"] > 0
    assert gateway.sent == []
    assert gateway.chat_calls == [{"chatID": SINGLE, "maxParticipantCount": 0}]     # details only, no names of people
    assert set(gateway.seen_keys) == {f"Bearer {KEY}"}
    (q,) = src.pending()
    assert q.recipients == [SINGLE]
    d = src.describe(q)
    assert d["title"] == "Beeper message" and d["text"] == WORDS and d["warnings"] == []
    assert ("Chat", "Example Person") in d["rows"] and ("Network", "WhatsApp") in d["rows"] and ("People", "2") in d["rows"]


async def test_a_group_shows_its_size_as_a_warning(web, hub, gateway):
    app, mcp, src = launch(web, hub)
    await ask(mcp, GROUP)
    assert src.describe(src.pending()[0])["warnings"] == ["A group chat with 12 people: all of them read this."]


async def test_what_the_agent_says_about_the_chat_is_not_taken(web, hub, gateway):
    app, mcp, src = launch(web, hub)
    await mcp.call_tool("beeper_send_request", {"chat_id": SINGLE, "text": WORDS, "chat_title": "The Bank", "network": "Mail"})   # not parameters of the tool
    (q,) = src.pending()
    shown = dict(src.describe(q)["rows"])
    assert shown["Chat"] == "Example Person" and shown["Network"] == "WhatsApp"                     # what Beeper said


async def test_a_chat_outside_the_owners_list_is_refused_and_empty_means_nobody(web, hub, gateway):
    app, mcp, src = launch(web, hub, allow=SINGLE)
    with pytest.raises(Exception, match="Not permitted"):
        await ask(mcp, GROUP)
    assert src.pending() == []
    app, mcp, src = launch(web, hub, allow="")
    with pytest.raises(Exception, match="Not permitted"):
        await ask(mcp, SINGLE)


async def test_a_star_lets_any_chat_through(web, hub, gateway):
    app, mcp, src = launch(web, hub, allow="*")
    assert (await ask(mcp, GROUP))["status"] == "queued_for_owner_approval"


async def test_a_chat_beeper_does_not_know_queues_nothing(web, hub, gateway):
    app, mcp, src = launch(web, hub, allow="*")
    with pytest.raises(Exception, match="does not know that chat_id"):
        await ask(mcp, "!nobody:beeper.test")
    assert src.pending() == []


@pytest.mark.parametrize("mode", ["refuse", "forbid", "down"])
async def test_when_the_gateway_cannot_answer_nothing_is_queued(web, hub, gateway, mode):
    app, mcp, src = launch(web, hub)
    gateway.mode = mode
    with pytest.raises(Exception, match="Could not read that chat from Beeper"):
        await ask(mcp)
    assert src.pending() == [] and gateway.sent == []


async def test_empty_or_oversized_text_and_a_blank_chat_are_refused_before_anything_is_asked(web, hub, gateway):
    app, mcp, src = launch(web, hub)
    for kw in ({"text": "  "}, {"text": "x" * (B.MAX_TEXT + 1)}, {"chat_id": " "}):
        with pytest.raises(Exception):
            await ask(mcp, **kw)
    assert gateway.chat_calls == [] and src.pending() == []


async def test_the_same_request_id_returns_the_first_request(web, hub, gateway):
    app, mcp, src = launch(web, hub)
    first = await ask(mcp, request_id="k-1")
    again = await ask(mcp, request_id="k-1")
    assert again["outbox_id"] == first["outbox_id"] and again["already_queued"] is True and "already_queued" not in first
    assert len(src.pending()) == 1
    with pytest.raises(Exception, match="already used for a different request"):
        await ask(mcp, GROUP, request_id="k-1")


async def test_a_reply_is_carried_with_its_message_id(web, hub, gateway):
    app, mcp, src = launch(web, hub)
    await ask(mcp, reply_to="$message-7")
    d = src.describe(src.pending()[0])
    assert ("Replying to", "$message-7") in d["rows"]


# -- approving -------------------------------------------------------------------------------------------------------------------------
async def test_approving_sends_once_with_the_text_the_owner_saw_and_logs_no_content(web, hub, gateway):
    app, mcp, src = launch(web, hub)
    got = await ask(mcp, reply_to="$message-7")
    (q,) = src.pending()
    async with client(app) as c:
        token = await signed_in(c)
        sha = (await c.put(url(got["outbox_id"]) + "/text", json={"sha": q.sha256, "text": "Edited: nine is fine"}, headers=api(token))).json()["sha"]
        r = await c.post(url(got["outbox_id"]) + "/approve", json={"sha": sha}, headers=api(token))
        assert r.status_code == 200 and r.json()["note"] == "Sent. Beeper accepted it."
        assert (await c.post(url(got["outbox_id"]) + "/approve", json={"sha": sha}, headers=api(token))).status_code == 409
    assert gateway.sent == [{"chatID": SINGLE, "text": "Edited: nine is fine", "replyToMessageID": "$message-7"}]
    assert (await tool(mcp, "approval_status", id=got["outbox_id"]))["status"] == "sent"
    log = audit_text(mcp)
    for secret in (WORDS, "Edited: nine", "Example Person", SINGLE, KEY, "north gate"):
        assert secret not in log
    (sent,) = [json.loads(x) for x in log.splitlines() if '"approval.sent"' in x]
    assert sent["summary"] == "Approved beeper “Beeper message”" and sent["details"]["delivery"] == "sent"
    assert sent["details"]["action_type"] == B.ACTION_ID


async def test_the_list_is_checked_again_when_approving(web, hub, gateway):
    app, mcp, src = launch(web, hub)
    got = await ask(mcp)
    (q,) = src.pending()
    object.__setattr__(mcp._icloud_dashboard.s, "beeper_send_allowlist", ())
    async with client(app) as c:
        token = await signed_in(c)
        r = await c.post(url(got["outbox_id"]) + "/approve", json={"sha": q.sha256}, headers=api(token))
        assert r.status_code >= 400
    assert gateway.sent == [] and len(src.pending()) == 1


@pytest.mark.parametrize("change", [{"title": "Somebody Else"}, {"network": "Telegram"}, {"type": "group"}])
async def test_a_chat_that_changed_since_the_owner_looked_is_not_written_to(web, hub, gateway, change):
    app, mcp, src = launch(web, hub)
    got = await ask(mcp)
    gateway.chats[SINGLE] = {**gateway.chats[SINGLE], **change}
    (q,) = src.pending()
    async with client(app) as c:
        token = await signed_in(c)
        r = await c.post(url(got["outbox_id"]) + "/approve", json={"sha": q.sha256}, headers=api(token))
        assert r.status_code == 502 and "not the one you looked at" in r.json()["error"]
    assert gateway.sent == [] and len(src.pending()) == 1                      # still waiting: the owner discards it and asks again


async def test_more_people_in_the_chat_than_the_owner_saw_stops_it_too(web, hub, gateway):
    app, mcp, src = launch(web, hub)
    got = await ask(mcp, GROUP)
    gateway.chats[GROUP]["participants"]["total"] = 40
    (q,) = src.pending()
    async with client(app) as c:
        token = await signed_in(c)
        r = await c.post(url(got["outbox_id"]) + "/approve", json={"sha": q.sha256}, headers=api(token))
        assert r.status_code == 502
    assert gateway.sent == []


@pytest.mark.parametrize("mode", ["refuse", "forbid", "forbid_call", "down"])
async def test_a_gateway_that_never_took_the_send_leaves_the_request_waiting(web, hub, gateway, mode):
    app, mcp, src = launch(web, hub)
    got = await ask(mcp)
    (q,) = src.pending()
    gateway.mode = mode
    async with client(app) as c:
        token = await signed_in(c)
        r = await c.post(url(got["outbox_id"]) + "/approve", json={"sha": q.sha256}, headers=api(token))
        assert r.status_code == 502 and r.json()["error"].startswith("Not sent (still waiting): The gateway ")
        assert ("refused the key" in r.json()["error"]) == (mode != "down")
        gateway.mode = None
        r = await c.post(url(got["outbox_id"]) + "/approve", json={"sha": q.sha256}, headers=api(token))
        assert r.status_code == 200
    assert len(gateway.sent) == 1                                                # one send in the end, not two


async def test_an_answer_that_is_lost_after_the_send_is_unclear_and_never_repeated(web, hub, gateway, caplog):
    app, mcp, src = launch(web, hub)
    got = await ask(mcp)
    (q,) = src.pending()
    real = B.Gateway.call

    def lose(self, name, arguments):
        if name == "beeper_send-send_message":
            gateway.mode = "lose"
        return real(self, name, arguments)
    B.Gateway.call = lose
    try:
        async with client(app) as c:
            token = await signed_in(c)
            with caplog.at_level(logging.INFO):
                r = await c.post(url(got["outbox_id"]) + "/approve", json={"sha": q.sha256}, headers=api(token))
            assert r.status_code == 200 and r.json()["state"] == "unclear"
            assert (await c.post(url(got["outbox_id"]) + "/approve", json={"sha": q.sha256}, headers=api(token))).status_code == 409
    finally:
        B.Gateway.call = real
    assert len(gateway.sent) == 1 and src.pending() == []
    assert (await tool(mcp, "approval_status", id=got["outbox_id"]))["status"] == "unclear"
    assert WORDS not in caplog.text and KEY not in caplog.text and WORDS not in audit_text(mcp)


async def test_an_error_from_the_send_tool_is_unclear_not_a_retry(web, hub, gateway):
    app, mcp, src = launch(web, hub)
    got = await ask(mcp)
    (q,) = src.pending()
    gateway.send_error = True
    async with client(app) as c:
        token = await signed_in(c)
        r = await c.post(url(got["outbox_id"]) + "/approve", json={"sha": q.sha256}, headers=api(token))
        assert r.status_code == 200 and r.json()["state"] == "unclear"
    assert len(gateway.sent) == 1 and src.pending() == []


# -- the rest of the hub works on it as on the other kinds -----------------------------------------------------------------------------
async def test_the_agent_can_take_a_waiting_request_back(web, hub, gateway):
    app, mcp, src = launch(web, hub)
    got = await ask(mcp)
    out = json.loads((await mcp.call_tool("approval_withdraw", {"id": got["outbox_id"], "reason": "mistake"})).content[0].text)
    assert out["withdrawn"] is True and src.pending() == [] and gateway.sent == []


async def test_the_exec_key_is_masked_in_errors_and_not_on_the_settings_page(web, hub, gateway):
    app, mcp, src = launch(web, hub)
    from icloud_mcp import server as S
    assert KEY in S._error_secrets
    async with client(app) as c:
        await signed_in(c)
        page = (await c.get("/dashboard/api/settings")).text
    assert KEY not in page and "BEEPER_SEND_ALLOWLIST" in page         # the key's value is nowhere; only the list can be changed here


def test_an_answer_that_is_too_late_is_unknown_not_a_refusal(gateway):
    gateway.slow = 2.0
    g = B.Gateway(URL, KEY, 0.5, make_http=gateway.make_http)
    with pytest.raises(B.GatewayError) as e:
        g.call("beeper_send-send_message", {"chatID": SINGLE, "text": "t"})
    assert (e.value.kind, e.value.before_send) == ("unknown", False) and len(gateway.sent) == 1


def test_the_gateway_errors_say_nothing_the_gateway_said(gateway):
    gateway.mode = "refuse"
    with pytest.raises(B.GatewayError) as e:
        B.Gateway(URL, KEY, 5, make_http=gateway.make_http).call("beeper-get_chat", {"chatID": SINGLE})
    assert str(e.value) == "the gateway refused the key" and (e.value.kind, e.value.before_send) == ("auth", True)
    assert KEY not in str(e.value)


def test_the_executor_answers_failed_for_a_chat_it_cannot_read_and_never_raises(monkeypatch):
    class Silent:
        def chat(self, chat_id):
            raise B.GatewayError("unreachable", True)

        def send(self, *a):
            raise AssertionError("nothing may be sent")
    r = B.BeeperExecutor(Silent()).execute(A.ActionRequest("x", B.ACTION_ID, {"chat_id": SINGLE, "text": "t", "chat_title": "T", "network": "N", "is_group": False}))
    assert r.status == "failed" and "could not be reached" in r.line
