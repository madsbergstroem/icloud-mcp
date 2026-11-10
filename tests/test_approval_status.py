"""approval_status: what became of something an agent queued for the owner's approval. The answer comes from the outboxes (still
waiting) and the activity log (how it ended), is only {id, kind, status, edited, at}, and never holds text, subjects or names."""
import dataclasses
import json
import time

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from icloud_mcp import approval_status as mod
from icloud_mcp.approval_status import ApprovalLedger, Released
from icloud_mcp.audit import AuditLog
from icloud_mcp.outbox import Outbox

from test_dashboard import BASE, api, client, queue, signed_in, web  # noqa: F401  (web is a fixture)

KEYS = {"id", "kind", "status", "edited", "at"}


@pytest.fixture
def parts(tmp_path):
    audit = AuditLog(str(tmp_path))
    mail, msg = Outbox(str(tmp_path), 3600, 20, filename="outbox.json"), Outbox(str(tmp_path), 3600, 20, filename="imessage-outbox.json")
    return audit, mail, msg, ApprovalLedger({"mail": mail, "imessage": msg, "calendar": None}, audit)


def end(audit, action, item_id, kind="mail", **details):
    audit.record(action, actor="owner", via="password", summary="x", kind=kind, outbox_id=item_id, **details)


# ------------------------------------------------------------------------------------------------ one request by id
def test_an_answer_is_five_fields_and_a_queued_request_is_waiting(parts):
    audit, mail, _, ledger = parts
    q = mail.add(b"raw one", ["bob@example.org"])
    got = ledger.status(q.id)
    assert set(got) == KEYS
    assert got == {"id": q.id, "kind": "mail", "status": "waiting", "edited": False, "at": mod._iso(q.created_at)}
    assert got["at"].endswith("Z")


def test_each_way_a_request_can_end_has_its_status(parts):
    audit, mail, msg, ledger = parts
    ids = {n: mail.add(f"raw {n}".encode(), ["bob@example.org"]) for n in ("sent", "discarded", "failed", "unconfirmed", "undelivered")}
    for q in ids.values():
        mail.claim(q.id)                                                 # released: it has left the queue
    end(audit, "approval.sent", ids["sent"].id)
    end(audit, "approval.discarded", ids["discarded"].id)
    end(audit, "approval.failed", ids["failed"].id, ok=False)
    end(audit, "approval.sent", ids["unconfirmed"].id, kind="imessage", delivery="unconfirmed")   # handed to Messages, not confirmed
    end(audit, "approval.sent", ids["undelivered"].id, kind="imessage", delivery="failed")        # Messages said it was not delivered
    got = {n: ledger.status(q.id) for n, q in ids.items()}
    assert {n: r["status"] for n, r in got.items()} == {"sent": "sent", "discarded": "discarded", "failed": "unclear",
                                                          "unconfirmed": "unclear", "undelivered": "unclear"}
    assert got["unconfirmed"]["kind"] == "imessage" and got["sent"]["kind"] == "mail"
    assert all(set(r) == KEYS and r["edited"] is False and r["at"] for r in got.values())


def test_an_unknown_or_old_id_is_unknown_and_says_nothing_else(parts):
    _, _, _, ledger = parts
    assert ledger.status("never-queued") == {"id": "never-queued", "kind": None, "status": "unknown", "edited": False, "at": None}


def test_a_release_that_failed_and_came_back_is_waiting_again_then_ends_normally(parts):
    audit, mail, _, ledger = parts
    q = mail.add(b"raw", ["bob@example.org"])
    claimed = mail.claim(q.id)
    end(audit, "approval.failed", q.id, ok=False)
    assert ledger.status(q.id)["status"] == "unclear"                    # gone and not sent: the owner has to look
    mail.restore(claimed)                                                # what mail and calendar do when the send itself failed
    assert ledger.status(q.id)["status"] == "waiting"
    mail.claim(q.id)
    end(audit, "approval.sent", q.id)
    assert ledger.status(q.id)["status"] == "sent"                       # the newest entry decides


def test_the_log_being_off_is_said(tmp_path):
    audit = AuditLog(str(tmp_path), enabled=False)
    mail = Outbox(str(tmp_path), 3600, 20)
    q = mail.add(b"raw", ["bob@example.org"])
    ledger = ApprovalLedger({"mail": mail}, audit)
    got = ledger.status(q.id)
    assert got["status"] == "waiting" and "AUDIT_LOG=false" in got["note"]
    mail.claim(q.id)
    assert ledger.status(q.id)["status"] == "unknown"


# ------------------------------------------------------------------------------------------------ expiry
def lapse(box, q):
    """Let an entry run out of time; returns when it did."""
    ran_out = time.time() - 30
    box._items[q.id] = dataclasses.replace(q, expires_at=ran_out)
    return ran_out


def test_an_entry_that_times_out_leaves_one_log_line_and_reads_expired(parts):
    audit, mail, _, ledger = parts
    q = mail.add(b"raw", ["bob@example.org"])
    ran_out = lapse(mail, q)
    got = ledger.status(q.id)                                            # reading the queue is what notices the timeout
    assert got["status"] == "expired" and got["kind"] == "mail"
    assert got["at"] == mod._iso(ran_out)                                # the time it ran out, not when it was noticed
    lines = [e for e in audit.entries() if e["action"] == "approval.expired"]
    assert len(lines) == 1 and lines[0]["actor"] == "server" and lines[0]["details"]["outbox_id"] == q.id
    assert "bob@example.org" not in json.dumps(lines)
    mail.pending(), mail.pending()
    assert len([e for e in audit.entries() if e["action"] == "approval.expired"]) == 1      # once


def test_an_entry_that_timed_out_while_the_server_was_down_is_reported_once(tmp_path):
    audit = AuditLog(str(tmp_path))
    box = Outbox(str(tmp_path), 3600, 20)
    q = box.add(b"raw", ["bob@example.org"])
    data = json.loads(box.path.read_text())
    data["items"][0]["expires_at"] = time.time() - 60
    box.path.write_text(json.dumps(data))
    restarted = Outbox(str(tmp_path), 3600, 20)                          # loads, finds it expired, drops it, and nobody listens yet
    assert not audit.entries()
    ledger = ApprovalLedger({"mail": restarted}, audit)                  # the ledger starts listening and the entry is written now
    assert [e["details"]["outbox_id"] for e in audit.entries()] == [q.id]
    assert ledger.status(q.id)["status"] == "expired"
    again = Outbox(str(tmp_path), 3600, 20)                              # another restart: the drop was saved, so nothing repeats
    ApprovalLedger({"mail": again}, audit)
    assert len(audit.entries()) == 1


def test_a_failing_log_never_breaks_the_queue(tmp_path):
    box = Outbox(str(tmp_path), 3600, 20)
    q = box.add(b"raw", ["bob@example.org"])
    box.on_expiry(lambda _q: 1 / 0)
    lapse(box, q)
    assert box.pending() == []


# ------------------------------------------------------------------------------------------------ since a cursor
def test_now_starts_a_cursor_and_later_outcomes_come_after_it(parts):
    audit, mail, msg, ledger = parts
    audit.record("tool.mail", actor="Claude", summary="Moved messages")
    first = ledger.since("now")
    assert first["items"] == [] and first["more"] is False and first["cursor"] == audit.entries()[0]["h"]
    a, b = mail.add(b"a", ["bob@example.org"]), msg.add(b"b", ["anna@example.org"])
    audit.record("tool.mail", actor="Claude", summary="Something unrelated")
    mail.claim(a.id), msg.claim(b.id)
    end(audit, "approval.sent", a.id)
    end(audit, "approval.discarded", b.id, kind="imessage")
    got = ledger.since(first["cursor"])
    assert [(i["id"], i["kind"], i["status"]) for i in got["items"]] == [(a.id, "mail", "sent"), (b.id, "imessage", "discarded")]
    assert all(set(i) == KEYS for i in got["items"]) and got["more"] is False
    assert ledger.since(got["cursor"])["items"] == []                    # nothing new since the cursor it handed back


def test_the_start_of_the_log_and_an_unknown_cursor(parts):
    audit, mail, _, ledger = parts
    q = mail.add(b"a", ["bob@example.org"])
    mail.claim(q.id)
    end(audit, "approval.discarded", q.id)
    assert [i["id"] for i in ledger.since("0" * 64)["items"]] == [q.id]  # genesis: everything the log holds
    lost = ledger.since("f" * 64)
    assert lost["items"] == [] and lost["cursor_unknown"] is True and lost["cursor"] == audit.entries()[0]["h"]
    assert ledger.since("")["cursor_unknown"] is True


def test_a_request_seen_twice_is_listed_once_with_its_state_now(parts):
    audit, mail, _, ledger = parts
    start = ledger.since("now")["cursor"]
    q = mail.add(b"a", ["bob@example.org"])
    claimed = mail.claim(q.id)
    end(audit, "approval.failed", q.id, ok=False)
    mail.restore(claimed)
    got = ledger.since(start)
    assert [(i["id"], i["status"]) for i in got["items"]] == [(q.id, "waiting")]


def test_pages_are_cut_at_a_cursor_that_loses_nothing(parts, monkeypatch):
    audit, mail, _, ledger = parts
    monkeypatch.setattr(mod, "_PAGE", 2)
    start = ledger.since("now")["cursor"]
    made = [mail.add(f"m{n}".encode(), ["bob@example.org"]) for n in range(5)]
    for q in made:
        mail.claim(q.id)
        end(audit, "approval.sent", q.id)
        audit.record("tool.mail", actor="Claude", summary="Noise")
    seen, cursor = [], start
    for _ in range(5):
        page = ledger.since(cursor)
        seen += [i["id"] for i in page["items"]]
        cursor = page["cursor"]
        if not page["more"]:
            break
    assert seen == [q.id for q in made]


def test_an_entry_that_names_several_ids_covers_each_of_them(parts):
    audit, mail, _, ledger = parts
    ids = [mail.add(f"m{n}".encode(), ["bob@example.org"]).id for n in range(3)]
    for i in ids:
        mail.claim(i)
    audit.record("approval.discarded", actor="owner", summary="Discarded 3", kind="mail", outbox_ids=ids)
    assert [ledger.status(i)["status"] for i in ids] == ["discarded"] * 3


# ------------------------------------------------------------------------------------------------ edited
def edit(audit, box, q, original="what the agent asked"):
    """What the dashboard does when the owner changes the text: swap the entry (it now keeps the agent's text) and log it."""
    new = box.replace(q.id, q.sha256, b"the owner's words", edited_from=original)
    end(audit, "approval.edited", q.id)
    return new


def bring_back(audit, box, q):
    """What the dashboard does when the owner brings the agent's text back."""
    new = box.replace(q.id, q.sha256, b"the agent's words", edited_from=None)
    end(audit, "approval.edit_undone", q.id)
    return new


def test_a_waiting_request_is_edited_when_its_entry_keeps_the_agents_text(parts):
    audit, mail, _, ledger = parts
    plain, changed = mail.add(b"raw one", ["bob@example.org"]), mail.add(b"raw two", ["bob@example.org"])
    edit(audit, mail, changed)
    assert [ledger.status(q.id)["edited"] for q in (plain, changed)] == [False, True]
    assert ledger.status(changed.id)["status"] == "waiting" and ledger.status(changed.id)["at"] == mod._iso(changed.created_at)
    bring_back(audit, mail, mail.pending()[1])
    assert ledger.status(changed.id)["edited"] is False                  # the owner brought the agent's text back


def test_a_waiting_request_follows_its_entry_and_not_the_log(parts):
    audit, mail, _, ledger = parts
    q = mail.add(b"raw", ["bob@example.org"])
    mail.replace(q.id, q.sha256, b"edited raw", edited_from="asked")     # the entry says edited, the log does not yet
    assert ledger.status(q.id)["edited"] is True
    other = mail.add(b"raw two", ["bob@example.org"])
    end(audit, "approval.edited", other.id)                              # the log says edited, the entry does not
    assert ledger.status(other.id)["edited"] is False


@pytest.mark.parametrize("action, details, status", [
    ("approval.sent", {}, "sent"),
    ("approval.discarded", {}, "discarded"),
    ("approval.failed", {"ok": False}, "unclear"),
    ("approval.sent", {"delivery": "unconfirmed", "kind": "imessage"}, "unclear"),
])
def test_an_edit_is_remembered_however_the_request_ends(parts, action, details, status):
    audit, mail, _, ledger = parts
    edited, untouched = mail.add(b"raw one", ["bob@example.org"]), mail.add(b"raw two", ["bob@example.org"])
    edit(audit, mail, edited)
    for q in (edited, untouched):
        mail.claim(q.id)
        end(audit, action, q.id, **details)
    got = ledger.status(edited.id), ledger.status(untouched.id)
    assert [(r["status"], r["edited"]) for r in got] == [(status, True), (status, False)]
    assert all(set(r) == KEYS for r in got)


def test_an_edited_request_that_expires_reads_expired_and_edited(parts):
    audit, mail, _, ledger = parts
    q = edit(audit, mail, mail.add(b"raw", ["bob@example.org"]))
    ran_out = lapse(mail, q)
    got = ledger.status(q.id)
    assert (got["status"], got["edited"], got["at"]) == ("expired", True, mod._iso(ran_out))


def test_an_edit_that_was_brought_back_does_not_count(parts):
    audit, mail, _, ledger = parts
    q = mail.add(b"raw", ["bob@example.org"])
    q = bring_back(audit, mail, edit(audit, mail, q))
    mail.claim(q.id)
    end(audit, "approval.sent", q.id)
    assert ledger.status(q.id)["edited"] is False                        # the newest of the two entries is the one that undid it


def test_an_edit_after_one_was_brought_back_counts_again(parts):
    audit, mail, _, ledger = parts
    q = mail.add(b"raw", ["bob@example.org"])
    q = edit(audit, mail, bring_back(audit, mail, edit(audit, mail, q)))
    mail.claim(q.id)
    end(audit, "approval.sent", q.id)
    assert ledger.status(q.id)["edited"] is True


def test_edit_entries_of_other_requests_do_not_leak_into_an_answer(parts):
    audit, mail, _, ledger = parts
    one, two = mail.add(b"raw one", ["bob@example.org"]), mail.add(b"raw two", ["bob@example.org"])
    edit(audit, mail, one)
    bring_back(audit, mail, mail.pending()[0])
    two = edit(audit, mail, two)
    for q in (one, two):
        mail.claim(q.id)
        end(audit, "approval.sent", q.id)
    assert (ledger.status(one.id)["edited"], ledger.status(two.id)["edited"]) == (False, True)


def test_an_unknown_id_is_not_edited_even_when_the_log_mentions_it(parts):
    audit, _, _, ledger = parts
    end(audit, "approval.edited", "gone")                                # an edit entry alone ends nothing
    assert ledger.status("gone") == {"id": "gone", "kind": None, "status": "unknown", "edited": False, "at": None}


def test_the_list_since_a_cursor_shows_edited_for_each_item(parts):
    audit, mail, msg, ledger = parts
    start = ledger.since("now")["cursor"]
    a, b, c, d = (mail.add(f"m{n}".encode(), ["bob@example.org"]) for n in range(4))
    e = msg.add(b"waiting", ["anna@example.org"])
    edit(audit, mail, a)
    edit(audit, mail, b)
    bring_back(audit, mail, mail.pending()[1])                           # b: edited, then brought back
    edit(audit, mail, d)
    for q, action in ((a, "approval.sent"), (b, "approval.sent"), (c, "approval.sent"), (d, "approval.discarded")):
        mail.claim(q.id)
        end(audit, action, q.id)
    end(audit, "approval.failed", e.id, kind="imessage", ok=False)       # e: failed once and is back, edited meanwhile
    edit(audit, msg, e)
    got = ledger.since(start)
    assert [(i["id"], i["status"], i["edited"]) for i in got["items"]] == [
        (a.id, "sent", True), (b.id, "sent", False), (c.id, "sent", False), (d.id, "discarded", True), (e.id, "waiting", True)]
    assert all(set(i) == KEYS for i in got["items"]) and got["more"] is False


def test_edit_entries_alone_change_neither_the_list_nor_its_cursor(parts):
    audit, mail, _, ledger = parts
    start = ledger.since("now")["cursor"]
    q = mail.add(b"raw", ["bob@example.org"])
    edit(audit, mail, q)
    bring_back(audit, mail, mail.pending()[0])
    got = ledger.since(start)
    assert got["items"] == [] and got["more"] is False and got["cursor"] == audit.entries()[0]["h"]


# ------------------------------------------------------------------------------------------------ the tool
async def call(mcp, args):
    return json.loads((await mcp.call_tool("approval_status", args)).content[0].text)


async def test_the_tool_follows_a_mail_from_queue_to_approval_and_never_shows_its_content(web):
    app, mcp, _p, sent = web()
    q1 = queue(mcp, subject="Secret plan for Thursday", body="Meet at the old mill")
    q2 = queue(mcp, subject="Second one", body="Another body")
    assert await call(mcp, {"id": q1}) | {"at": 0} == {"id": q1, "kind": "mail", "status": "waiting", "edited": False, "at": 0}
    start = (await call(mcp, {"since": "now"}))["cursor"]
    async with client(app) as c:
        h = api(await signed_in(c))
        shas = {w["key"]: (await c.get(f"/dashboard/api/items/{w['key']}")).json()["sha"] for w in (await c.get("/dashboard/api/today")).json()["waiting"]}
        r = await c.post(f"/dashboard/api/items/mail:{q1}/approve", json={"sha": shas[f"mail:{q1}"]}, headers=h)
        assert r.status_code == 200 and sent
        r = await c.post(f"/dashboard/api/items/mail:{q2}/discard", json={"sha": shas[f"mail:{q2}"]}, headers=h)
        assert r.status_code == 200
    assert (await call(mcp, {"id": q1}))["status"] == "sent" and (await call(mcp, {"id": q2}))["status"] == "discarded"
    listed = await call(mcp, {"since": start})
    assert [(i["id"], i["status"]) for i in listed["items"]] == [(q1, "sent"), (q2, "discarded")] and listed["more"] is False
    text = json.dumps([await call(mcp, {"id": q1}), listed])
    for secret in ("Secret plan", "old mill", "Second one", "Another body", "bob@example.org", "Lunch"):
        assert secret not in text
    log = mcp._icloud_dashboard.audit.entries(10, ("approval.",))
    assert {e["action"]: e["details"]["outbox_id"] for e in log} == {"approval.discarded": q2, "approval.sent": q1}


async def test_the_tool_reports_an_edit_made_on_the_dashboard_and_never_the_text(web):
    app, mcp, _p, sent = web()
    q = queue(mcp, subject="Plans for Friday", body="Asked-for-words")
    mail = mcp._icloud_dashboard.sources["mail"]
    cursor = (await call(mcp, {"since": "now"}))["cursor"]
    seen = []

    async def ask():
        seen.append(await call(mcp, {"id": q}))
        return seen[-1]["edited"]
    assert await ask() is False
    async with client(app) as c:
        h = api(await signed_in(c))
        sha = mail.pending()[0].sha256
        put = lambda body: c.put(f"/dashboard/api/items/mail:{q}/text", json=body, headers=h)  # noqa: E731
        sha = (await put({"sha": sha, "text": "Owner-written-words"})).json()["sha"]
        assert await ask() is True and seen[-1]["status"] == "waiting"
        sha = (await put({"sha": sha, "restore": True})).json()["sha"]
        assert await ask() is False
        sha = (await put({"sha": sha, "text": "Owner-rewrote-it"})).json()["sha"]
        assert await ask() is True
        r = await c.post(f"/dashboard/api/items/mail:{q}/approve", json={"sha": sha}, headers=h)
        assert r.status_code == 200 and sent
    assert await ask() is True and seen[-1]["status"] == "sent"
    listed = await call(mcp, {"since": cursor})
    assert [(i["id"], i["status"], i["edited"]) for i in listed["items"]] == [(q, "sent", True)]
    text = json.dumps([*seen, listed])
    for secret in ("Plans for Friday", "Asked-for-words", "Owner-written-words", "Owner-rewrote-it", "bob@example.org"):
        assert secret not in text
    assert all(set(r) == KEYS for r in seen) and set(listed["items"][0]) == KEYS
    assert "edited_from" not in text and "original" not in text
    assert [e["action"] for e in mcp._icloud_dashboard.audit.entries(20, ("approval.",))].count("approval.edited") == 2


async def test_the_original_typed_back_by_hand_reads_as_not_edited_waiting_and_sent(web):
    app, mcp, _p, sent = web()
    q = queue(mcp, body="Original words")
    mail = mcp._icloud_dashboard.sources["mail"]
    async with client(app) as c:
        h = api(await signed_in(c))
        put = lambda sha, text: c.put(f"/dashboard/api/items/mail:{q}/text", json={"sha": sha, "text": text}, headers=h)  # noqa: E731
        sha = (await put(mail.pending()[0].sha256, "Changed words")).json()["sha"]
        assert (await call(mcp, {"id": q}))["edited"] is True
        sha = (await put(sha, "Original words")).json()["sha"]                  # no "Bring it back": the same words typed in again
        assert (await call(mcp, {"id": q})) | {"at": 0} == {"id": q, "kind": "mail", "status": "waiting", "edited": False, "at": 0}
        assert (await c.post(f"/dashboard/api/items/mail:{q}/approve", json={"sha": sha}, headers=h)).status_code == 200 and sent
    got = await call(mcp, {"id": q})
    assert (got["status"], got["edited"]) == ("sent", False)                    # the waiting answer and the sent one agree


async def test_the_tool_refuses_neither_and_both(web):
    _, mcp, *_ = web()
    for args in ({}, {"id": "a", "since": "now"}, {"id": "x" * 65}, {"id": "  "}):
        with pytest.raises(ToolError):
            await mcp.call_tool("approval_status", args)


async def test_a_request_that_expires_reads_expired_through_the_tool(web):
    _, mcp, *_ = web()
    q = queue(mcp)
    box = mcp._icloud_outboxes["mail"]
    lapse(box, box._items[q])
    got = await call(mcp, {"id": q})
    assert got["status"] == "expired" and got["kind"] == "mail"


async def test_an_iMessage_handed_to_messages_without_confirmation_is_unclear(web, monkeypatch):
    app, mcp, *_ = web(ENABLE_IMESSAGE="true", IMESSAGE_ALLOW_SEND="true", IMESSAGE_SEND_ALLOWLIST="anna@example.org", BRIDGE_TOKEN="t" * 40)
    chats = {"chats": [{"chat_id": "anna@example.org", "participants": ["anna@example.org"], "name": ""}]}
    sends = []

    def bridge(op, a=None):
        if op == "imessage_read":
            return {"chat": chats["chats"][0], "messages": [], "complete": True}
        sends.append(a)
        return {"status": "unconfirmed", "note": "Handed to Messages."}
    mcp._icloud_bridge.call = bridge
    queued = json.loads((await mcp.call_tool("imessage_send_message", {"text": "See you at 6", "handle": "anna@example.org"})).content[0].text)
    assert queued["status"] == "queued_for_owner_approval" and "approval_status" in queued["notice"]
    assert (await call(mcp, {"id": queued["outbox_id"]}))["kind"] == "imessage"
    async with client(app) as c:
        h = api(await signed_in(c))
        key = "imessage:" + queued["outbox_id"]
        sha = (await c.get(f"/dashboard/api/items/{key}")).json()["sha"]
        r = await c.post(f"/dashboard/api/items/{key}/approve", json={"sha": sha}, headers=h)
        assert r.status_code == 200 and len(sends) == 1
    got = await call(mcp, {"id": queued["outbox_id"]})
    assert got["status"] == "unclear" and got["kind"] == "imessage"
    assert "See you at 6" not in json.dumps(got)


def forms_of(page):
    """The /outbox page's forms as {action: [fields]}: every hidden input of each form."""
    import re
    out = []
    for form in re.findall(r'<form method="post" action="/outbox/act".*?</form>', page, re.S):
        out.append(dict(re.findall(r'name="(\w+)" value="([^"]*)"', form)))
    return out


async def test_the_old_outbox_page_writes_the_same_ids(tmp_path, monkeypatch):
    import contextlib

    from icloud_mcp.config import Settings
    from icloud_mcp.mail import MailError, MailService
    from icloud_mcp.server import create_server
    from test_dashboard import PASSWORD
    from test_outbox import FakeIMAP
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", MCP_PUBLIC_URL=BASE, MCP_OWNER_PASSWORD=PASSWORD,
                     DATA_DIR=str(tmp_path), ALLOW_SEND="true", DASHBOARD="false").items():
        monkeypatch.setenv(k, v)
    fake, sent, fail = FakeIMAP(), [], []

    @contextlib.contextmanager
    def fake_imap(self):
        yield fake

    def smtp(self, msg, rcpts):
        if fail:
            raise MailError("The mail server refused it.")
        sent.append((msg, list(rcpts)))
        return {}
    monkeypatch.setattr(MailService, "imap", fake_imap)
    monkeypatch.setattr(MailService, "resolve_folder", lambda self, c, name: {"sent": "Sent Messages", "drafts": "Drafts"}.get(name.lower(), name))
    monkeypatch.setattr(MailService, "_smtp_send", smtp)
    mcp, provider = create_server(Settings.from_env())
    queued = [json.loads((await mcp.call_tool("mail_send_message", {"to": ["bob@example.org"], "subject": f"Hello {n}", "body": "Hi"})).content[0].text)
              for n in range(5)]
    ids = [m["outbox_id"] for m in queued]
    assert all(m["status"] == "queued_for_owner_approval" and "approval_status" in m["notice"] for m in queued)

    async def status(item_id):
        return json.loads((await mcp.call_tool("approval_status", {"id": item_id})).content[0].text)["status"]
    app = mcp.streamable_http_app(host="0.0.0.0")
    async with app.router.lifespan_context(app):
        async with client(app) as c:
            async def act(**form):
                return (await c.post("/outbox/act", data=form)).text

            async def fresh():
                return forms_of((await c.post("/outbox", data={"password": PASSWORD})).text)
            by = lambda forms, item_id, action: next(f for f in forms if f.get("id") == item_id and f["action"] == action)  # noqa: E731
            forms = await fresh()
            assert "Sent" in await act(**by(forms, ids[0], "approve"))
            assert "Discarded" in await act(**by(forms, ids[1], "discard"))
            fail.append(1)
            assert "Not sent (still queued)" in await act(**by(forms, ids[4], "approve"))        # the mail server refused it: it stays
            assert await status(ids[4]) == "waiting"
            forms = await fresh()
            everything = next(f for f in forms if f["action"] == "discard_all")
            assert "Discarded 3" in await act(**everything)
    assert [await status(i) for i in ids] == ["sent", "discarded", "discarded", "discarded", "discarded"]   # the newest entry decides
    from icloud_mcp.audit import AuditLog
    log = {(e["action"], e["details"].get("outbox_id") or tuple(e["details"]["outbox_ids"])) for e in AuditLog(str(tmp_path)).entries(20, ("approval.",))}
    assert log == {("approval.sent", ids[0]), ("approval.discarded", ids[1]), ("approval.failed", ids[4]),
                   ("approval.discarded", tuple(ids[2:]))}                                      # discard-all: one entry naming its ids


# ------------------------------------------------------------------------------------------------ what the surface says
def test_released_reads_as_the_string_it_is_and_carries_how_delivery_ended():
    r = Released("Sent: iMessage to Anna.", "unconfirmed")
    assert r == "Sent: iMessage to Anna." and r.delivery == "unconfirmed" and json.dumps({"note": r}) == '{"note": "Sent: iMessage to Anna."}'
    assert Released("x").delivery is None


async def test_the_description_keeps_kind_open_and_reserves_a_status_for_later(web):
    _, mcp, *_ = web()
    tool = next(t for t in await mcp.list_tools() if t.name == "approval_status")
    text = tool.description
    assert "kind is an open string" in text and "changes_requested" in text and "treat any status you do not know as not sent" in text
    assert "always false" not in text and "edited is true when the owner changed the text you queued" in text
    for status in mod.STATUSES:
        assert f"- {status}:" in text
    assert tool.annotations.read_only_hint is True and tool.annotations.open_world_hint is False
    assert "approval_status" in (mcp.instructions or "")


async def test_the_tool_exists_only_where_something_can_be_queued_and_stays_with_presets_and_a_pause(web, tmp_path):
    from icloud_mcp.server import ALWAYS_KEPT, ESSENTIAL_TOOLS
    assert "approval_status" in ALWAYS_KEPT and "approval_status" in ESSENTIAL_TOOLS
    _, mcp, *_ = web()
    assert "approval_status" in {t.name for t in await mcp.list_tools()}
    (tmp_path / "paused").write_text("")
    assert (await call(mcp, {"id": "anything"}))["status"] == "unknown"      # a read of metadata: it answers while the owner has paused agents
    (tmp_path / "paused").unlink()


async def test_a_server_that_cannot_queue_has_no_such_tool(web):
    _, mcp, *_ = web(READ_ONLY="true")
    assert "approval_status" not in {t.name for t in await mcp.list_tools()}
    assert "approval_status" not in (mcp.instructions or "")
