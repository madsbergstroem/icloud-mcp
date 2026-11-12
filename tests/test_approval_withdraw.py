"""approval_withdraw: an agent takes back its own request while it still waits. It claims the entry the way a discard does, so a
withdrawal and an Approve cannot both win; it answers {id, withdrawn, status} and never anything that looks like success for a
request that was already decided; and its log line (approval.withdrawn, by the assistant) holds the title and the reason, never
the text."""
import base64
import contextlib
import dataclasses
import hashlib
import json
import re
import secrets
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import Implementation

from icloud_mcp import approval_withdraw as mod
from icloud_mcp.approval_withdraw import REASONS, Withdrawals
from icloud_mcp.approval_status import ApprovalLedger
from icloud_mcp.audit import AuditLog
from icloud_mcp.outbox import Outbox
from icloud_mcp.server import _client_label

from test_dashboard import BASE, PASSWORD, api, client, queue, signed_in, web  # noqa: F401  (web is a fixture)

KEYS = {"id", "withdrawn", "status"}
IMESSAGE = dict(ENABLE_IMESSAGE="true", IMESSAGE_ALLOW_SEND="true", IMESSAGE_SEND_ALLOWLIST="anna@example.org", BRIDGE_TOKEN="t" * 40)


def queue_imessage(mcp, text="Running late, start without me"):
    raw = json.dumps({"chat_id": "anna@example.org", "handle": "anna@example.org", "text": text,
                      "to": {"name": "", "group": False, "participants": [{"handle": "anna@example.org", "name": "Anna Rivera"}]}}).encode()
    return mcp._icloud_outboxes["imessage"].add(raw, ["anna@example.org"]).id


def queue_calendar(mcp, summary="Autumn fair", description="Bring ideas"):
    return mcp._icloud_outboxes["calendar"].add(
        b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", ["jana@example.org"],
        {"op": "create", "calendar_url": "https://caldav.example.com/cal/", "calendar": "Club", "uid": "u1", "summary": summary,
         "start": "2026-10-22T18:00:00+02:00", "end": "2026-10-22T19:30:00+02:00", "location": "", "description": description,
         "organizer": None, "href": None, "etag": None}).id


async def withdraw(mcp, item_id, reason=None):
    args = {"id": item_id, **({"reason": reason} if reason else {})}
    return json.loads((await mcp.call_tool("approval_withdraw", args)).content[0].text)


async def status(mcp, item_id):
    return json.loads((await mcp.call_tool("approval_status", {"id": item_id})).content[0].text)


def log(mcp, *prefix):
    return mcp._icloud_dashboard.audit.entries(100, prefix)


def waiting(mcp, kind):
    return [q.id for q in mcp._icloud_outboxes[kind].pending()]


# ------------------------------------------------------------------------------------------------ taking back, every kind
async def test_a_waiting_request_of_every_kind_can_be_taken_back(web):
    app, mcp, _, sent = web(ALLOW_CALENDAR_INVITES="approve", **IMESSAGE)
    ids = {"mail": queue(mcp, subject="Invoice 2291", body="Thanks"), "imessage": queue_imessage(mcp), "calendar": queue_calendar(mcp)}
    for kind, item_id in ids.items():
        got = await withdraw(mcp, item_id, "mistake")
        assert got == {"id": item_id, "withdrawn": True, "status": "withdrawn"}
        assert waiting(mcp, kind) == []
        seen = await status(mcp, item_id)
        assert (seen["status"], seen["kind"], seen["edited"]) == ("withdrawn", kind, False) and seen["at"].endswith("Z")
    assert sent == []                                                          # taking back sends nothing


async def test_the_reason_is_a_fixed_choice_and_no_longer_needed_is_the_default(web):
    app, mcp, *_ = web()
    assert set(REASONS) == {"mistake", "duplicate", "superseded", "no_longer_needed"}
    for n, reason in enumerate((*REASONS, None)):
        got = await withdraw(mcp, queue(mcp, subject=f"Subject {n}", body=f"Body {n}"), reason)
        assert got["withdrawn"] is True
    assert [e["details"]["reason"] for e in reversed(log(mcp, "approval.withdrawn"))] == [*REASONS, "no_longer_needed"]
    keep = queue(mcp, subject="Stays", body="Stays")
    for bad in ("because", "", "MISTAKE", "mistake, duplicate", 3):
        with pytest.raises(ToolError):                                          # the schema refuses anything outside the four
            await mcp.call_tool("approval_withdraw", {"id": keep, "reason": bad})
    assert waiting(mcp, "mail") == [keep]                                       # and nothing was taken back


async def test_the_tool_refuses_an_id_that_cannot_be_one(web):
    _, mcp, *_ = web()
    for args in ({"id": ""}, {"id": "  "}, {"id": "x" * 65}, {}):
        with pytest.raises(ToolError):
            await mcp.call_tool("approval_withdraw", args)


# ------------------------------------------------------------------------------------------------ too late
async def test_a_request_the_owner_approved_first_is_not_taken_back(web):
    app, mcp, _, sent = web()
    q = queue(mcp, subject="Quote", body="Details")
    async with client(app) as c:
        h = api(await signed_in(c))
        sha = (await c.get(f"/dashboard/api/items/mail:{q}")).json()["sha"]
        assert (await c.post(f"/dashboard/api/items/mail:{q}/approve", json={"sha": sha}, headers=h)).status_code == 200
    got = await withdraw(mcp, q)
    assert got == {"id": q, "withdrawn": False, "status": "sent"} and len(sent) == 1
    assert log(mcp, "approval.withdrawn") == []


async def test_taken_back_first_the_owners_approve_finds_nothing_and_sends_nothing(web):
    app, mcp, _, sent = web()
    q = queue(mcp, subject="Quote", body="Details")
    async with client(app) as c:
        h = api(await signed_in(c))
        sha = (await c.get(f"/dashboard/api/items/mail:{q}")).json()["sha"]
        assert (await withdraw(mcp, q))["withdrawn"] is True
        r = await c.post(f"/dashboard/api/items/mail:{q}/approve", json={"sha": sha}, headers=h)
        assert r.status_code == 409 and "no longer waiting" in r.json()["error"]
    assert sent == []


async def test_when_the_claim_is_lost_to_an_approve_nothing_looks_like_success(web):
    """The look says waiting; between the look and the claim the owner's Approve claims the entry: the tool's claim finds nothing."""
    app, mcp, _, sent = web()
    q = queue(mcp)
    box = mcp._icloud_outboxes["mail"]
    real = box.claim

    def owner_first(item_id):
        real(item_id)                                  # the owner's release got there first ...
        return real(item_id)                           # ... so the tool's own claim comes back empty
    box.claim = owner_first
    got = await withdraw(mcp, q)
    assert got["withdrawn"] is False and got["status"] != "withdrawn" and set(got) == KEYS
    assert log(mcp, "approval.withdrawn") == [] and waiting(mcp, "mail") == []
    mcp._icloud_dashboard.audit.record("approval.sent", actor="owner", via="password", summary="x", kind="mail", outbox_id=q)
    assert (await withdraw(mcp, q)) == {"id": q, "withdrawn": False, "status": "sent"}      # once the release is logged, it reads sent


async def test_an_unknown_an_expired_and_a_discarded_request_each_answer_with_their_status(web):
    app, mcp, *_ = web()
    assert await withdraw(mcp, "never-queued") == {"id": "never-queued", "withdrawn": False, "status": "unknown"}
    gone = queue(mcp, subject="Old", body="Old")
    box = mcp._icloud_outboxes["mail"]
    box._items[gone] = dataclasses.replace(box._items[gone], expires_at=time.time() - 30)
    assert await withdraw(mcp, gone) == {"id": gone, "withdrawn": False, "status": "expired"}
    no = queue(mcp, subject="No", body="No")
    async with client(app) as c:
        h = api(await signed_in(c))
        sha = (await c.get(f"/dashboard/api/items/mail:{no}")).json()["sha"]
        assert (await c.post(f"/dashboard/api/items/mail:{no}/discard", json={"sha": sha}, headers=h)).status_code == 200
    assert await withdraw(mcp, no) == {"id": no, "withdrawn": False, "status": "discarded"}
    assert (await withdraw(mcp, no)) == (await withdraw(mcp, no))              # asking again changes nothing
    assert log(mcp, "approval.withdrawn") == []


async def test_a_second_withdrawal_says_it_was_already_taken_back(web):
    _, mcp, *_ = web()
    q = queue(mcp)
    assert (await withdraw(mcp, q))["withdrawn"] is True
    assert await withdraw(mcp, q) == {"id": q, "withdrawn": False, "status": "withdrawn"}
    assert len(log(mcp, "approval.withdrawn")) == 1


# ------------------------------------------------------------------------------------------------ the label
async def as_client(mcp, name, tool, args):
    async with Client(mcp, client_info=Implementation(name=name, version="1.0"), mode="legacy") as c:
        result = await c.call_tool(tool, args)
    return result


async def test_a_request_carries_the_name_of_the_client_that_queued_it_and_only_that_client_takes_it_back(web):
    app, mcp, *_ = web()
    mail = {"to": ["bob@example.org"], "subject": "Lunch", "body": "See you at one"}
    queued = json.loads((await as_client(mcp, "agent-a", "mail_send_message", mail)).content[0].text)
    assert queued["status"] == "queued_for_owner_approval"
    (q,) = mcp._icloud_outboxes["mail"].pending()
    assert q.requested_by == "agent-a"
    async with client(app) as c:
        await signed_in(c)
        assert [w["by"] for w in (await c.get("/dashboard/api/today")).json()["waiting"]] == ["agent-a"]     # Home shows who asked
    other = await as_client(mcp, "agent-b", "approval_withdraw", {"id": q.id})
    assert other.is_error and "Another client queued this request" in other.content[0].text
    nameless = await as_client(mcp, "", "approval_withdraw", {"id": q.id})
    assert nameless.is_error and waiting(mcp, "mail") == [q.id]                 # a label that does not match is refused, however it fails to
    again = await as_client(mcp, "agent-a", "approval_withdraw", {"id": q.id, "reason": "duplicate"})
    assert not again.is_error and json.loads(again.content[0].text) == {"id": q.id, "withdrawn": True, "status": "withdrawn"}
    (entry,) = log(mcp, "approval.withdrawn")
    assert entry["via"] == "agent-a" and entry["actor"] == "assistant"


@contextlib.asynccontextmanager
async def over_http(app, *names):
    """The server as it is served (stateless: no handshake outlives its first request), reached the way apps reach it: each name
    registers, the owner lets it in, and its calls carry its bearer token. Yields {name: call}, call(tool, arguments) -> (is_error, text)."""
    redirect = "http://localhost:6274/callback"
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE, follow_redirects=False) as c:
            async def connect(name):
                cid = (await c.post("/register", json={"client_name": name, "redirect_uris": [redirect], "token_endpoint_auth_method": "none",
                                                       "grant_types": ["authorization_code"], "response_types": ["code"]})).json()["client_id"]
                verifier = secrets.token_urlsafe(48)
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                r = await c.get("/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": redirect, "state": "s",
                                                      "code_challenge": challenge, "code_challenge_method": "S256", "scope": "icloud"})
                login = r.headers["location"]
                await c.get(login)
                r = await c.post("/login", data={"p": parse_qs(urlsplit(login).query)["p"][0], "password": PASSWORD, "action": "approve"})
                code = parse_qs(urlsplit(r.headers["location"]).query)["code"][0]
                tok = (await c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect, "client_id": cid,
                                                    "code_verifier": verifier})).json()["access_token"]
                headers = {"Authorization": f"Bearer {tok}", "Accept": "application/json, text/event-stream"}

                async def call(tool, arguments):
                    r = await c.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                                   "params": {"name": tool, "arguments": arguments}})
                    assert r.status_code == 200, r.text
                    result = r.json()["result"]
                    return result.get("isError", False), result["content"][0]["text"]
                return call
            yield {name: await connect(name) for name in names}


async def test_over_http_the_label_is_the_name_the_app_registered_under_and_only_that_app_takes_it_back(web):
    """MCP_STATELESS is on by default, so a call after the handshake knows no clientInfo: the registered name is what is left."""
    app, mcp, *_ = web()
    async with over_http(app, "Agent A", "Agent B") as apps:
        a, b = apps["Agent A"], apps["Agent B"]
        bad, text = await a("mail_send_message", {"to": ["bob@example.org"], "subject": "Lunch", "body": "See you at one"})
        assert not bad and json.loads(text)["status"] == "queued_for_owner_approval"
        (q,) = mcp._icloud_outboxes["mail"].pending()
        assert q.requested_by == "Agent A"
        bad, text = await b("approval_withdraw", {"id": q.id})
        assert bad and "Another client queued this request" in text and waiting(mcp, "mail") == [q.id]
        bad, text = await a("approval_withdraw", {"id": q.id, "reason": "mistake"})
        assert not bad and json.loads(text) == {"id": q.id, "withdrawn": True, "status": "withdrawn"}
    (entry,) = log(mcp, "approval.withdrawn")
    assert entry["actor"] == "assistant" and entry["via"] == "Agent A" and entry["details"]["reason"] == "mistake"


async def test_every_queueing_tool_labels_its_request(web):
    app, mcp, *_ = web(ALLOW_CALENDAR_INVITES="approve", **IMESSAGE)
    mcp._icloud_bridge.call = lambda op, a=None: ({"chat": {"chat_id": "anna@example.org", "participants": ["anna@example.org"], "name": ""},
                                                  "messages": [], "complete": True} if op == "imessage_read" else {})
    got = await as_client(mcp, "agent-a", "messages_send_request", {"text": "Hello", "handle": "anna@example.org"})
    assert json.loads(got.content[0].text)["status"] == "queued_for_owner_approval", got.content[0].text
    (q,) = mcp._icloud_outboxes["imessage"].pending()
    assert q.requested_by == "agent-a"


async def test_a_request_with_no_label_can_be_taken_back_by_any_client(web):
    """Nothing says who queued it (a client that gave no name, or an entry from before labels): there is nothing to compare."""
    app, mcp, *_ = web()
    q = queue(mcp)                                                              # queued with no client behind the call
    assert mcp._icloud_outboxes["mail"].pending()[0].requested_by is None
    got = await as_client(mcp, "agent-b", "approval_withdraw", {"id": q})
    assert not got.is_error and json.loads(got.content[0].text)["withdrawn"] is True


async def test_a_client_without_a_name_leaves_the_label_empty(web):
    app, mcp, *_ = web()
    mail = {"to": ["bob@example.org"], "subject": "Lunch", "body": "See you at one"}
    await mcp.call_tool("mail_send_message", mail)                              # no handshake behind the call at all
    assert mcp._icloud_outboxes["mail"].pending()[0].requested_by is None


def test_the_label_is_a_short_clean_name_and_empty_when_there_is_none():
    def ctx(name):
        return SimpleNamespace(session=SimpleNamespace(client_params=SimpleNamespace(client_info=SimpleNamespace(name=name))))
    assert _client_label(ctx("Claude Code")) == "Claude Code"
    assert _client_label(ctx("  two\n lines\t ")) == "two lines"
    assert _client_label(ctx("x" * 100)) == "x" * 40
    assert _client_label(ctx("")) == "" and _client_label(ctx(None)) == ""
    assert _client_label(SimpleNamespace(session=SimpleNamespace(client_params=None))) == ""      # a client that did not introduce itself
    assert _client_label(None) == "" and _client_label(object()) == ""


def test_without_a_handshake_the_label_is_the_registered_name(monkeypatch):
    monkeypatch.setattr("icloud_mcp.server._registered_name", lambda: "Claude")
    no_handshake = SimpleNamespace(session=SimpleNamespace(client_params=None))
    assert _client_label(no_handshake) == "Claude" and _client_label(None) == "Claude"
    named = SimpleNamespace(session=SimpleNamespace(client_params=SimpleNamespace(client_info=SimpleNamespace(name="agent-a"))))
    assert _client_label(named) == "agent-a"                                    # what the client calls itself wins


def test_the_label_survives_a_restart_an_edit_and_old_files_without_it(tmp_path):
    box = Outbox(str(tmp_path), 3600, 20)
    q = box.add(b"raw", ["bob@example.org"], requested_by="agent-a")
    assert Outbox(str(tmp_path), 3600, 20).pending()[0].requested_by == "agent-a"
    edited = box.replace(q.id, q.sha256, b"new raw", edited_from="raw")
    assert edited.requested_by == "agent-a" and Outbox(str(tmp_path), 3600, 20).pending()[0].requested_by == "agent-a"
    assert "requested_by" not in box.add(b"other", ["x@example.org"]).to_json()          # nothing stored when there is no label
    legacy = q.to_json()
    legacy.pop("requested_by")
    assert type(q).from_json(legacy).requested_by is None


# ------------------------------------------------------------------------------------------------ the log
async def test_the_log_line_is_by_the_assistant_and_holds_no_text(web):
    app, mcp, *_ = web(ALLOW_CALENDAR_INVITES="approve", **IMESSAGE)
    secrets = {"mail": ("Hidden mail body", "Invoice 2291"), "imessage": ("Hidden iMessage text", None), "calendar": ("Hidden event notes", "Autumn fair")}
    ids = {"mail": queue(mcp, subject="Invoice 2291", body=secrets["mail"][0]), "imessage": queue_imessage(mcp, secrets["imessage"][0]),
           "calendar": queue_calendar(mcp, description=secrets["calendar"][0])}
    for kind, item_id in ids.items():
        await withdraw(mcp, item_id, "duplicate")
    entries = list(reversed(log(mcp, "approval.withdrawn")))
    assert [(e["actor"], e["details"]["kind"], e["details"]["outbox_id"], e["details"]["reason"]) for e in entries] == [
        ("assistant", kind, ids[kind], "duplicate") for kind in ids]
    assert [e["summary"] for e in entries] == ["Withdrew email “Invoice 2291”", "Withdrew imessage “iMessage to Anna Rivera (anna@example.org)”",
                                               "Withdrew invitation “Autumn fair”"]
    # a mail and an invitation keep their first recipient, as approval.sent does, so Home can say whom it was for
    assert [(e["ok"], set(e["details"])) for e in entries] == [(True, {"kind", "outbox_id", "reason", "recipients"}),
                                                               (True, {"kind", "outbox_id", "reason"}),
                                                               (True, {"kind", "outbox_id", "reason", "recipients"})]
    assert entries[0]["details"]["recipients"] == ["bob@example.org"]
    raw_log = mcp._icloud_dashboard.audit.path.read_text()
    for hidden in ("Hidden mail body", "Hidden iMessage text", "Hidden event notes"):
        assert hidden not in raw_log
    assert "tool.approval" not in raw_log                                        # one line per withdrawal: the tool writes its own
    assert mcp._icloud_dashboard.audit.verify()["ok"] is True


# ------------------------------------------------------------------------------------------------ approval_status
async def test_approval_status_reads_a_withdrawal_as_a_result_by_id_and_in_the_list(web):
    app, mcp, *_ = web()
    cursor = json.loads((await mcp.call_tool("approval_status", {"since": "now"})).content[0].text)["cursor"]
    a, b, c_ = queue(mcp, subject="A", body="a"), queue(mcp, subject="B", body="b"), queue(mcp, subject="C", body="c")
    await withdraw(mcp, b, "superseded")
    listed = json.loads((await mcp.call_tool("approval_status", {"since": cursor})).content[0].text)
    assert [(i["id"], i["status"], i["edited"]) for i in listed["items"]] == [(b, "withdrawn", False)] and listed["more"] is False
    assert set(listed["items"][0]) == {"id", "kind", "status", "edited", "at"}
    assert (await status(mcp, a))["status"] == "waiting" and (await status(mcp, c_))["status"] == "waiting"
    text = json.dumps([listed, await status(mcp, b)])
    for hidden in ("bob@example.org", '"B"', "superseded"):
        assert hidden not in text                                                # no text, no recipient, no reason in the answer


async def test_edited_reads_as_before_on_a_request_that_was_withdrawn(web):
    app, mcp, *_ = web()
    one, two = queue(mcp, subject="One", body="Asked-for-words"), queue(mcp, subject="Two", body="Original words")
    mail = mcp._icloud_dashboard.sources["mail"]
    async with client(app) as c:
        h = api(await signed_in(c))
        put = lambda q, sha, body: c.put(f"/dashboard/api/items/mail:{q}/text", json={"sha": sha, **body}, headers=h)  # noqa: E731
        sha1 = next(q.sha256 for q in mail.pending() if q.id == one)
        sha2 = next(q.sha256 for q in mail.pending() if q.id == two)
        sha1 = (await put(one, sha1, {"text": "Owner-written-words"})).json()["sha"]
        sha2 = (await put(two, sha2, {"text": "Owner-written-words"})).json()["sha"]
        await put(two, sha2, {"restore": True})                                  # two: edited, then brought back
    assert (await status(mcp, one))["edited"] is True and (await status(mcp, two))["edited"] is False
    await withdraw(mcp, one)
    await withdraw(mcp, two)
    got_one, got_two = await status(mcp, one), await status(mcp, two)
    assert (got_one["status"], got_one["edited"]) == ("withdrawn", True)
    assert (got_two["status"], got_two["edited"]) == ("withdrawn", False)
    assert "Owner-written-words" not in json.dumps([got_one, got_two])


# ------------------------------------------------------------------------------------------------ the dashboard
async def test_activity_shows_a_withdrawal_in_plain_words_with_its_reason_and_a_tag(web):
    app, mcp, *_ = web()
    q = queue(mcp, subject="Re: Invoice 2291", body="Hello")
    await withdraw(mcp, q, "mistake")
    async with client(app) as c:
        await signed_in(c)
        for which in ("all", "approvals"):                                        # the Approvals filter counts it (approval.*)
            days = (await c.get(f"/dashboard/api/activity?filter={which}")).json()["days"]
            rows = [e for d in days for e in d["entries"] if e["tag"] == "Withdrawn"]
            assert len(rows) == 1, which
            row = rows[0]
            assert row["who"] == "Your assistant" and row["what"] == "withdrew email “Re: Invoice 2291”" and row["ok"] is True
            assert row["note"] == "Withdrawn by your assistant: queued by mistake. Nothing was sent."
        assert not [e for d in (await c.get("/dashboard/api/activity?filter=signins")).json()["days"] for e in d["entries"] if e["tag"] == "Withdrawn"]
    # an app that happens to be called "assistant" is still itself in its own lines
    mcp._icloud_dashboard.audit.record("tool.mail", actor="assistant", summary="Moved messages (1)", tool="mail_move_messages")
    async with client(app) as c:
        await signed_in(c)
        names = [e["who"] for d in (await c.get("/dashboard/api/activity")).json()["days"] for e in d["entries"] if "moved messages" in e["what"]]
    assert names == ["assistant"]


async def test_home_lists_what_was_taken_back_in_the_last_day_and_lets_go_after_that(web, monkeypatch):
    app, mcp, *_ = web()
    audit = mcp._icloud_dashboard.audit
    async with client(app) as c:
        await signed_in(c)
        assert (await c.get("/dashboard/api/today")).json()["withdrawn"] == []
        keep = queue(mcp, subject="Stays waiting", body="x")
        for n in range(2):
            await withdraw(mcp, queue(mcp, subject=f"Taken back {n}", body="Secret words"), reason="duplicate" if n else None)
        long_ago = time.time() - 25 * 3600
        with monkeypatch.context() as m:                                           # a withdrawal from 25 hours ago, written then
            m.setattr(time, "time", lambda: long_ago)
            audit.record("approval.withdrawn", actor="assistant", summary="Withdrew email “Old”", kind="mail", outbox_id="old", reason="mistake")
        today = (await c.get("/dashboard/api/today")).json()
        assert len(today["withdrawn"]) == 2 and all(0 <= time.time() - w["t"] < 60 for w in today["withdrawn"])
        # what Activity shows for each, newest first: title, kind, first recipient, the reason; never the text
        assert [(w["title"], w["what"], w["to"], w["why"]) for w in today["withdrawn"]] == [
            ("Taken back 1", "Email", "bob@example.org", "A duplicate."), ("Taken back 0", "Email", "bob@example.org", "No longer needed.")]
        assert "Secret words" not in json.dumps(today["withdrawn"])
        assert [w["title"] for w in today["waiting"]] == ["Stays waiting"] and today["waiting"][0]["key"] == f"mail:{keep}"   # waiting entries are untouched
        assert [e["actor"] for e in audit.entries(5, ("approval.withdrawn",))].count("assistant") == 3      # the log keeps all three
        later = time.time() + 24 * 3600 + 120                                       # only the API's clock moves: the sign-in stays valid
        monkeypatch.setattr("icloud_mcp.dashboard_api.time", SimpleNamespace(time=lambda: later, strftime=time.strftime))
        assert (await c.get("/dashboard/api/today")).json()["withdrawn"] == []        # a day on: nothing to say any more


async def test_the_home_screen_lists_them_and_lets_go_of_each_by_itself():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "src" / "icloud_mcp" / "static" / "app" / "today.js").read_text()
    assert "Taken back by your assistant" in js and "Nothing was sent." in js and "withdrawn by your assistant" not in js
    assert "setTimeout(() => this.render()" in js and "clearTimeout(this.letGoTimer)" in js
    assert "icm:activity-focus" in js                                             # a row opens the entry in Activity


# ------------------------------------------------------------------------------------------------ the tool itself
async def test_the_description_names_the_limits_and_the_tool_is_marked_as_a_write(web):
    _, mcp, *_ = web()
    tool = next(t for t in await mcp.list_tools() if t.name == "approval_withdraw")
    text = tool.description
    for phrase in ("only the owner can", "withdrawn false", "not a security boundary", "ask the owner before you queue", "Never queue it again",
                   "no_longer_needed", "mail, imessage, calendar", "never the text"):
        assert phrase in text, phrase
    assert tool.annotations.read_only_hint is False and tool.annotations.destructive_hint is True and tool.annotations.open_world_hint is False
    assert tool.input_schema["properties"]["reason"]["enum"] == list(REASONS) and tool.input_schema["required"] == ["id"]
    assert "ctx" not in tool.input_schema["properties"]                          # the client's name is not something a caller passes in
    status_text = next(t for t in await mcp.list_tools() if t.name == "approval_status").description
    assert "- withdrawn:" in status_text and "unknown status" not in status_text and "treat any status you do not know as not sent" in status_text


async def test_it_stays_with_presets_but_not_with_a_pause_and_is_absent_where_nothing_queues(web, tmp_path):
    from icloud_mcp.server import ALWAYS_KEPT, ESSENTIAL_TOOLS, KEPT_WITH_AREAS
    assert "approval_withdraw" in ESSENTIAL_TOOLS and "approval_withdraw" in KEPT_WITH_AREAS and "approval_withdraw" not in ALWAYS_KEPT
    _, mcp, *_ = web()
    q = queue(mcp)
    (tmp_path / "paused").write_text("")
    with pytest.raises(ToolError, match="Paused by the owner"):                  # unlike the status question, taking back is an action
        await mcp.call_tool("approval_withdraw", {"id": q})
    assert waiting(mcp, "mail") == [q]
    (tmp_path / "paused").unlink()
    assert (await withdraw(mcp, q))["withdrawn"] is True


async def test_a_server_that_cannot_queue_has_no_such_tool(web):
    _, mcp, *_ = web(READ_ONLY="true")
    assert "approval_withdraw" not in {t.name for t in await mcp.list_tools()}
    assert "approval_withdraw" not in (mcp.instructions or "")


# ------------------------------------------------------------------------------------------------ the words agents read
def sentences(text):
    return [s for s in re.split(r"(?<=[.!?])\s+", text.replace("\n", " ")) if "password" in s.lower()]


async def test_no_tool_text_names_the_password_as_the_only_way_to_approve(web):
    from icloud_mcp.mail import OWNER_APPROVAL_NOTICE
    _, mcp, *_ = web()
    tools = {t.name: t.description for t in await mcp.list_tools()}
    texts = {"instructions": mcp.instructions, "queued mail notice": OWNER_APPROVAL_NOTICE, "approval_status": tools["approval_status"],
             "approval_withdraw": tools["approval_withdraw"], "mail_send_message": tools["mail_send_message"],
             "messages_send_request": tools.get("messages_send_request", "")}
    for name, text in texts.items():
        for sentence in sentences(text):
            # a sentence about the password only forbids handling it, or calls it the fallback; none says it is the way in
            assert sentence.lstrip("- ").startswith("Never") or "fallback" in sentence, (name, sentence)
        assert not re.search(r"(with|using|types?|enters?) (a|the) (owner )?password", text), name
    assert "passkey" not in OWNER_APPROVAL_NOTICE and "where they sign in themselves" in OWNER_APPROVAL_NOTICE
    assert "only the owner can approve or discard it, on the dashboard" in mcp.instructions and "fallback" in mcp.instructions


async def test_the_rules_say_how_to_take_a_request_back_and_what_to_ask_the_owner_first(web):
    _, mcp, *_ = web()
    text = mcp.instructions
    assert "approval_withdraw (take back a request you queued)" in text
    rule = text[text.index("TAKING A REQUEST BACK"):].split("\n")[0]
    assert "approval_withdraw" in rule and "ask the owner before you queue a corrected version or the same text again" in rule and "Edit text" in rule
    assert "approval_status" in text and "never send it again" in text           # the older rules stay


def test_the_stale_docstrings_no_longer_call_the_approval_page_password_protected():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "src" / "icloud_mcp"
    for name in ("imessage.py", "mail.py", "outbox.py"):
        assert "password-protected" not in (root / name).read_text() and "needs the owner password" not in (root / name).read_text(), name
    assert "approval_withdraw" in mod.__doc__ and "not a security boundary" in mod.__doc__


# ------------------------------------------------------------------------------------------------ the unit
def test_a_withdrawal_by_the_unit_claims_once_and_writes_one_line(tmp_path):
    audit = AuditLog(str(tmp_path))
    box = Outbox(str(tmp_path), 3600, 20, filename="outbox.json")
    ledger = ApprovalLedger({"mail": box, "imessage": None, "calendar": None}, audit)
    q = box.add(b"Subject: Hello\r\n\r\nBody\r\n", ["bob@example.org"], requested_by="agent-a")
    got = Withdrawals(ledger).withdraw(q.id, "mistake", "agent-a")
    assert got == {"id": q.id, "withdrawn": True, "status": "withdrawn"} and box.pending() == []
    (entry,) = audit.entries(5)
    assert entry["action"] == "approval.withdrawn" and entry["actor"] == "assistant" and entry["summary"] == "Withdrew email “Hello”"
    assert Withdrawals(ledger).withdraw(q.id, "mistake", "agent-a")["withdrawn"] is False
    assert len(audit.entries(5)) == 1


def test_a_title_that_cannot_be_read_still_lets_the_request_be_taken_back(tmp_path):
    audit = AuditLog(str(tmp_path))
    box = Outbox(str(tmp_path), 3600, 20, filename="imessage-outbox.json")
    ledger = ApprovalLedger({"mail": None, "imessage": box, "calendar": None}, audit)
    q = box.add(b"not json at all", ["anna@example.org"])
    assert Withdrawals(ledger).withdraw(q.id, "no_longer_needed", "")["withdrawn"] is True
    assert audit.entries(1)[0]["summary"] == "Withdrew a queued imessage request"
    assert mod.describe("somethingelse", q) == ("", "")
