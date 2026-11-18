"""Package 6a (docs/SENDGATE.md): every way an approved mail can end is a state of the approval store, and nothing reaches DATA
twice for one approval. Runs the real smtplib client against the made-up SMTP server of smtp_fakes, which can refuse or drop the
connection at each step and counts how often a message reached DATA. The mailbox is a small fake; every address is made up."""
import contextlib
import dataclasses
import email
import smtplib
import socket
import subprocess
import sys
import time
from email import policy
from pathlib import Path

import pytest

import icloud_mcp.mail as mail_mod
import smtp_fakes
from test_dashboard import web  # noqa: F401  -- a fixture
from icloud_mcp.config import Settings
from icloud_mcp.gate.actions import Rechecked
from icloud_mcp.gate.store import Store
from icloud_mcp.mail import MailError, MailService, mail_type_of

ME = "me@icloud.com"
REAL_CONNECT = socket.create_connection


class FakeIMAP:
    def __init__(self):
        self.appended, self.flags, self.moved = [], [], []
        self.broken: set[str] = set()                # what fails: "append", "flag", "move"

    def append(self, folder, raw, flags=(), msg_time=None):
        if "append" in self.broken:
            raise OSError("the mailbox did not answer")
        self.appended.append((folder, raw))

    def add_flags(self, uids, flags):
        if "flag" in self.broken:
            raise MailError("The folder changed since it was read (UIDVALIDITY): the uid is out of date.")
        self.flags.append((list(uids), list(flags)))


@pytest.fixture
def env(tmp_path, monkeypatch):
    server = smtp_fakes.Server()
    for k, v in dict(ICLOUD_USERNAME=ME, ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path), ALLOW_SEND="true",
                     MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16, SMTP_HOST="127.0.0.1",
                     SMTP_PORT=str(server.port), SMTP_SECURITY="none").items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(mail_mod, "READ_TIMEOUT", 0.5)              # a server that hangs is given up on quickly
    server.hang_seconds = 2.0
    fake = FakeIMAP()
    state = {"imap_down": False}

    @contextlib.contextmanager
    def imap(self, fresh=False):
        if state["imap_down"]:
            raise MailError("IMAP login failed: the mailbox could not be reached.")
        yield fake

    def select(c, folder, *, readonly=True, expect=None):
        return expect

    def move(c, uids, dst):
        if "move" in fake.broken:
            raise MailError("The move was refused.")
        fake.moved.append((list(uids), dst))

    monkeypatch.setattr(MailService, "imap", imap)
    monkeypatch.setattr(MailService, "resolve_folder", lambda self, c, name: {"sent": "Sent Messages", "drafts": "Drafts",
                                                                              "trash": "Deleted Messages"}.get(name.lower(), name))
    monkeypatch.setattr(MailService, "_select", staticmethod(select))
    monkeypatch.setattr(MailService, "_move_messages", staticmethod(move))
    s = Settings.from_env()
    mail = MailService(s, outbox=Store(str(tmp_path), "mail", 3600, 20, keep_days=s.audit_keep_days, tombstones=True,
                                                type_of=mail_type_of, report_type=False))
    yield mail, server, fake, state
    mail._smtp_drop(quit=False)
    server.close()


def queue(mail, **kw):
    args = dict(to=["jana@example.org"], subject="Lunch on Friday", body="Hi Jana, see you at noon. Sam")
    args.update(kw)
    r = mail.send(**args)
    assert r["status"] == "queued_for_owner_approval"
    return r["outbox_id"]


def state(mail, item_id):
    return mail.outbox.state_of(item_id)


def attempts(mail, item_id):
    return mail.outbox._db.execute("SELECT attempts FROM request WHERE id = ?", (item_id,)).fetchone()[0]


# -- the server takes it -------------------------------------------------------------------------------------------------------
def test_an_approved_mail_is_sent_once_filed_in_sent_and_ends_sent(env):
    """protects=the "SMTP accepts it for every recipient" row; fails_when=it reaches DATA twice, another state, no Sent copy, or the
    note stops naming the recipients (D6-6); no_claim=delivery to the recipients (a bounce comes later as mail)."""
    mail, server, fake, _ = env
    item = queue(mail)
    r = mail.approve(item)
    assert r["status"] == "sent" and r["note"] == "Sent to jana@example.org."
    assert state(mail, item) == ("sent", None) and server.data_reached == 1 and len(server.messages) == 1
    assert [f for f, _ in fake.appended] == ["Sent Messages"]
    assert mail.outbox._db.execute("SELECT action_type FROM request WHERE id = ?", (item,)).fetchone()[0] == "mail.send"
    with pytest.raises(MailError, match="no longer waiting"):
        mail.approve(item)
    assert server.data_reached == 1


def test_the_wire_carries_no_bcc_and_the_copy_in_sent_keeps_it(env):
    """protects=the review's Bcc requirement: the stepwise send builds the bytes it sends without Bcc, and the stored message and
    the copy for Sent keep it; fails_when=the DATA bytes hold a Bcc header or the hidden address, the hidden address is not an
    envelope recipient, or the Sent copy loses its Bcc; no_claim=none beyond protects."""
    mail, server, fake, _ = env
    item = queue(mail, to=["jana@example.org"], bcc=["sam@example.net"])
    assert mail.approve(item)["status"] == "sent"
    wire = email.message_from_bytes(server.messages[0], policy=policy.default)
    assert wire["Bcc"] is None and b"sam@example.net" not in server.messages[0]
    assert sorted(server.envelopes[0][1]) == ["jana@example.org", "sam@example.net"]
    assert email.message_from_bytes(fake.appended[0][1], policy=policy.default)["Bcc"] == "sam@example.net"


def test_some_recipients_refused_ends_sent_and_the_note_names_them(env):
    """protects=M4 and D6-6: a recipient refused at RCPT while others were taken is named in the note, not reported as sent to;
    fails_when=the note says "Sent to" the refused address, leaves it out, or the mail waits again; no_claim=the log line (the
    dashboard test of the wiring)."""
    mail, server, _, _ = env
    server.refuse = {"tom@example.net"}
    item = queue(mail, to=["jana@example.org", "tom@example.net"])
    r = mail.approve(item)
    assert r["status"] == "sent" and r["note"] == "Sent to jana@example.org. The mail server refused 1: tom@example.net."
    assert list(r["refused"]) == ["tom@example.net"] and state(mail, item) == ("sent", None) and server.data_reached == 1


@pytest.mark.parametrize("broken, says", [("append", "could not be copied to the Sent folder"),
                                          ("flag", "The original was not marked"),
                                          ("move", "could not be moved to Trash")])
def test_sent_but_filing_afterwards_failed_ends_sent_and_says_what_is_missing(env, monkeypatch, broken, says):
    """protects=the last row: what fails after the server took the mail never puts it back; fails_when=it waits again (a second
    approval would send it twice) or the note hides what is missing; no_claim=none."""
    mail, server, fake, _ = env
    fake.broken = {broken}
    original = b"From: Jana <jana@example.org>\r\nTo: me@icloud.com\r\nSubject: Lunch?\r\nMessage-ID: <q1@example.org>\r\n\r\nFriday?\r\n"
    monkeypatch.setattr(MailService, "_fetch_readable", lambda self, c, folder, uid, readonly=True, uidvalidity=None: (original, (), None, 7, None))
    if broken == "flag":
        item = mail.reply("INBOX", 7, "Yes, noon works.")["outbox_id"]
    elif broken == "move":
        draft = email.message_from_bytes(b"From: me@icloud.com\r\nTo: jana@example.org\r\nSubject: Lunch\r\n\r\nNoon?\r\n", policy=policy.default)
        monkeypatch.setattr(MailService, "_load_draft", lambda self, c, folder, uid, uv: ("Drafts", draft, 7))
        item = mail.send_draft(4)["outbox_id"]
    else:
        item = queue(mail)
    r = mail.approve(item)
    assert r["status"] == "sent" and says in r["note"] and state(mail, item) == ("sent", None) and server.data_reached == 1


# -- nothing reaches the server ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("change", ["allowlist", "max_recipients", "allow_send"])
def test_a_mail_the_lists_no_longer_allow_ends_not_allowed_and_nothing_reaches_smtp(env, change):
    """protects=the first row: the owner's lists are checked again at approval with mail's own rules; fails_when=it reaches the
    server, waits again, or ends another way; no_claim=none."""
    mail, server, _, _ = env
    item = queue(mail, to=["jana@example.org", "tom@example.net"])
    mail.s = dataclasses.replace(mail.s, **{"allowlist": {"send_allowlist": ("jana@example.org",)},
                                            "max_recipients": {"max_recipients": 1},
                                            "allow_send": {"allow_send": False}}[change])
    with pytest.raises(Rechecked) as e:
        mail.approve(item)
    assert e.value.reason == "not_allowed" and state(mail, item) == ("discarded", "not_allowed")
    assert server.connections == 0 and server.data_reached == 0


@pytest.mark.parametrize("lists, guest", [((), "anyone@example.net"), (("@example.org",), "kim@example.org")])
def test_the_lists_at_approval_are_mails_own(env, lists, guest):
    """protects=an empty SEND_ALLOWLIST allows everyone and a domain entry the domain, at approval as when asked; fails_when=the
    gate's exact matching is used; no_claim=none."""
    mail, server, _, _ = env
    mail.s = dataclasses.replace(mail.s, send_allowlist=lists)
    item = queue(mail, to=[guest])
    assert mail.approve(item)["status"] == "sent" and server.data_reached == 1


def test_a_missing_file_claims_nothing(env):
    """protects=the "file still missing" row: nothing is claimed, so the request is not counted as carried out; fails_when=it is
    claimed, sent, or ends; no_claim=the page's own block (test_file_tickets.py)."""
    mail, server, _, _ = env
    mail.s = dataclasses.replace(mail.s, dashboard=True)
    r = mail.send(to=["jana@example.org"], subject="Quote", body="Attached.", attachments=[{"filename": "Quote.pdf", "content_base64": None}])
    with pytest.raises(MailError, match="still waits for Quote.pdf"):
        mail.approve(r["outbox_id"])
    assert state(mail, r["outbox_id"]) == ("waiting", None) and attempts(mail, r["outbox_id"]) == 0 and server.connections == 0


def test_a_file_added_between_the_look_and_the_click_sends_nothing(env):
    """protects=M3 and criterion 3: the approval names the version the owner saw; a file added after it is not sent unseen, and
    the mail waits with the new hash; fails_when=anything reaches the server, or the mail ends; no_claim=none."""
    mail, server, _, _ = env
    mail.s = dataclasses.replace(mail.s, dashboard=True)
    r = mail.send(to=["jana@example.org"], subject="Quote", body="Attached.",
                  attachments=[{"filename": "Notes.txt", "content_base64": "aGVsbG8="}, {"filename": "Quote.pdf", "content_base64": None}])
    item = r["outbox_id"]
    looked = mail.outbox.pending()[0].sha256                         # the owner's look: a file is still missing
    new, _ = mail.add_file(b"%PDF-1.4 made up", filename="Quote.pdf", item_id=item, index=1)
    with pytest.raises(MailError, match="changed just now"):
        mail.approve(item, looked)
    assert state(mail, item) == ("waiting", None) and mail.outbox.pending()[0].sha256 == new.sha256 != looked
    assert server.connections == 0 and server.data_reached == 0
    assert mail.approve(item, new.sha256)["status"] == "sent" and server.data_reached == 1


def test_a_mailbox_that_cannot_be_opened_keeps_it_waiting(env):
    """protects=the IMAP row: the mailbox is opened before SMTP, and when it cannot be, nothing is sent and the mail waits again;
    fails_when=it reaches the server or ends; no_claim=none."""
    mail, server, _, imap = env
    item = queue(mail)
    imap["imap_down"] = True
    with pytest.raises(MailError, match="mailbox could not be opened"):
        mail.approve(item)
    assert state(mail, item) == ("waiting", None) and server.connections == 0
    imap["imap_down"] = False
    assert mail.approve(item)["status"] == "sent" and server.data_reached == 1


# -- the closed list: SMTP fails before the message is handed over -----------------------------------------------------------------
def _refused_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.parametrize("how", ["refused", "name", "connect_timeout", "auth", "no_starttls", "mail_553", "mail_drop",
                                 "rcpt_all_refused", "rcpt_drop", "data_451", "after_554"])
def test_a_failure_before_the_message_is_handed_over_keeps_it_waiting(env, monkeypatch, how):
    """protects=the closed list of "never sent" SMTP errors (the step table) and D6-2; fails_when=it ends unclear or sent, or the
    message reaches DATA more than once for the approval; no_claim=the exception classes a real network raises beyond these."""
    mail, server, _, _ = env
    item = queue(mail, to=["jana@example.org", "tom@example.net"])
    if how == "refused":
        mail.s = dataclasses.replace(mail.s, smtp_port=_refused_port())
    elif how in ("name", "connect_timeout"):
        err = socket.gaierror(8, "nodename nor servname provided") if how == "name" else TimeoutError("timed out")
        monkeypatch.setattr(smtplib.socket, "create_connection", lambda *a, **kw: (_ for _ in ()).throw(err))
    elif how == "auth":
        server.fault = ("auth", 535)
    elif how == "no_starttls":
        mail.s = dataclasses.replace(mail.s, smtp_security="starttls")
    elif how == "rcpt_all_refused":
        server.refuse = {"jana@example.org", "tom@example.net"}
    else:
        step, what = how.split("_")
        server.fault = ({"after": "after"}.get(step, step), int(what) if what.isdigit() else what)
    with pytest.raises(MailError, match="The mail server"):
        mail.approve(item)
    assert state(mail, item) == ("waiting", None)
    assert server.data_reached == (1 if how in ("data_451", "after_554") else 0)
    reached = server.data_reached
    monkeypatch.setattr(smtplib.socket, "create_connection", REAL_CONNECT)       # the network is back
    server.fault, server.refuse = None, set()
    mail.s = dataclasses.replace(mail.s, smtp_port=server.port, smtp_security="none")
    assert mail.approve(item)["status"] == "sent" and server.data_reached == reached + 1


# -- it may have gone out ----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fault", [("data", "drop"), ("after", "drop"), ("after", "hang")])
def test_a_send_cut_off_while_the_message_is_handed_over_ends_unclear_and_is_never_repeated(env, fault):
    """protects=M1: a timeout or dropped connection once DATA began may have delivered the mail, so it ends unclear and never
    waits again; fails_when=it waits again (a second approval would send it twice) or ends sent; no_claim=none."""
    mail, server, fake, _ = env
    item = queue(mail)
    server.fault = fault
    r = mail.approve(item)
    assert r["status"] == "unclear" and "may or may not have gone out" in r["note"]
    assert state(mail, item) == ("unclear", None) and mail.outbox.pending() == [] and server.data_reached == 1
    assert fake.appended == []                                     # no copy in Sent for a mail nobody confirmed
    server.fault = None
    with pytest.raises(MailError, match="no longer waiting"):
        mail.approve(item)
    assert server.data_reached == 1


def test_a_draft_whose_mail_ended_unclear_is_not_queued_again_also_after_the_retention(env, monkeypatch):
    """protects=M5, D6-3 and criterion 6: mail_send_draft for a draft whose mail ended unclear queues nothing and answers with that
    mail's status, also once keep_days dropped the row (the tombstone); fails_when=a second mail waits, or the answer hides how it
    ended; no_claim=the draft in Drafts (it stays there; only the owner can send it from Mail)."""
    mail, server, _, _ = env
    draft = email.message_from_bytes(b"From: me@icloud.com\r\nTo: jana@example.org\r\nSubject: Lunch\r\nMessage-ID: <d1@icloud.com>\r\n\r\nNoon?\r\n",
                                     policy=policy.default)
    monkeypatch.setattr(MailService, "_load_draft", lambda self, c, folder, uid, uv: ("Drafts", draft, 7))
    item = mail.send_draft(4)["outbox_id"]
    server.fault = ("after", "drop")
    assert mail.approve(item)["status"] == "unclear"
    again = mail.send_draft(4)
    assert again == {"status": "unclear", "sent": False, "outbox_id": item, "already_decided": True, "notice": again["notice"]}
    assert mail.outbox.pending() == []
    mail.outbox._db.execute("UPDATE request SET decided_at = ? WHERE id = ?", (time.time() - (mail.outbox.keep_days + 1) * 86400, item))
    mail.outbox._purged_at = 0
    mail.outbox.pending()
    assert state(mail, item) is None                               # the row is gone with the activity retention ...
    assert mail.send_draft(4)["already_decided"] is True and mail.outbox.pending() == []     # ... the key is not
    assert server.data_reached == 1


def test_a_mail_cut_off_while_sent_ends_unclear_at_the_next_start(tmp_path):
    """protects=M2 and criterion 5: a row of source mail left executing ends unclear once, also when another source's store opened
    first, and is logged once; fails_when=it stays executing or waits again (it may have gone out); no_claim=a real process kill:
    the first store is a second process that exits without finishing."""
    from icloud_mcp.audit import AuditLog
    from icloud_mcp.gate.status import ApprovalLedger
    src = Path(__file__).resolve().parents[1] / "src"
    code = ("import os, sys; sys.path.insert(0, %r)\n" % str(src)
            + "from icloud_mcp.gate.store import Store\n"
            + "st = Store(%r, 'mail', 3600, 20, tombstones=True)\n" % str(tmp_path)
            + "q = st.add(b'Subject: Lunch\\r\\n\\r\\nNoon?', ['jana@example.org'], action_type='mail.send')\n"
            + "st.claim(q.id, 'executing')\nprint(q.id, flush=True)\nos._exit(0)\n")
    qid = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True).stdout.strip()
    Store(str(tmp_path), "external", 3600, 20)                        # another source's store takes the lock first
    box = Store(str(tmp_path), "mail", 3600, 20, tombstones=True)
    audit = AuditLog(str(tmp_path))
    ApprovalLedger({"mail": box}, audit)
    assert box.state_of(qid) == ("unclear", "interrupted") and box.pending() == []
    lines = [e for e in audit.entries(10) if e["action"] == "approval.sent"]
    assert len(lines) == 1 and lines[0]["details"]["kind"] == "mail" and lines[0]["details"]["delivery"] == "unconfirmed"
    ApprovalLedger({"mail": Store(str(tmp_path), "mail", 3600, 20, tombstones=True)}, audit)
    assert len([e for e in audit.entries(10) if e["action"] == "approval.sent"]) == 1


def test_the_type_of_each_mail_is_written_with_it(env, monkeypatch):
    """protects=the type 6b reads (mail.send, mail.reply, mail.forward), written when the mail is queued; fails_when=a forward or a
    draft is typed as a reply; no_claim=what 6b does with it."""
    mail, _, _, _ = env
    original = b"From: Jana <jana@example.org>\r\nTo: me@icloud.com\r\nSubject: Lunch?\r\nMessage-ID: <q1@example.org>\r\n\r\nFriday?\r\n"
    monkeypatch.setattr(MailService, "_fetch_readable", lambda self, c, folder, uid, readonly=True, uidvalidity=None: (original, (), None, 7, None))
    monkeypatch.setattr(MailService, "_fetch_raw", lambda self, c, folder, uid, readonly=True, uidvalidity=None: (original, (), None, 7))
    ids = {"mail.reply": mail.reply("INBOX", 7, "Yes.")["outbox_id"],
           "mail.forward": mail.forward("INBOX", 7, ["tom@example.net"], note="FYI")["outbox_id"],
           "mail.send": queue(mail)}
    types = dict(mail.outbox._db.execute("SELECT id, action_type FROM request").fetchall())
    assert {t: types[i] for t, i in ids.items()} == {t: t for t in ids}


# -- through the server's wiring ---------------------------------------------------------------------------------------------------
def test_the_server_reads_outbox_json_in_once_with_ids_hashes_and_types(tmp_path, monkeypatch):
    """protects=criterion 4 through the real wiring: the first start keeps every waiting mail with its id, hash, key and followup,
    types it in the row, renames the file, and a second start changes nothing; fails_when=a mail is lost, renumbered, rehashed or
    untyped, or the type leaks into what the mail reads back; no_claim=none beyond protects."""
    from icloud_mcp.outbox import Outbox
    from icloud_mcp.server import create_server
    for k, v in dict(ICLOUD_USERNAME=ME, ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path), ALLOW_SEND="true",
                     MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16).items():
        monkeypatch.setenv(k, v)
    old = Outbox(str(tmp_path), 3600, 20)
    a = old.add(b"From: me@icloud.com\r\nTo: jana@example.org\r\nSubject: Lunch\r\n\r\nNoon?\r\n", ["jana@example.org"])
    b = old.add(b"From: me@icloud.com\r\nTo: tom@example.net\r\nSubject: Fwd: Plan\r\n\r\nFYI\r\n", ["tom@example.net"],
                {"folder": "INBOX", "uid": 9, "flag": "$Forwarded", "uidvalidity": 3}, key=None)
    mcp, _ = create_server(Settings.from_env())
    box = mcp._icloud_dashboard.sources["mail"].mail.outbox
    assert sorted((q.id, q.sha256, q.followup, q.action_type) for q in box.pending()) == sorted(
        [(a.id, a.sha256, None, None), (b.id, b.sha256, b.followup, None)])
    assert dict(box._db.execute("SELECT id, action_type FROM request").fetchall()) == {a.id: "mail.send", b.id: "mail.forward"}
    assert (tmp_path / "outbox.json.migrated").exists() and not (tmp_path / "outbox.json").exists()


async def test_the_card_logs_refused_recipients_and_an_unclear_send_reads_unclear(web, monkeypatch):
    """protects=the log line's new refused count (M4) and an unclear ending through the dashboard: the note, the line and
    approval_status say what happened; fails_when=the line lacks the count, an unclear send is logged as sent or waits again;
    no_claim=the app's rendering (QA)."""
    import json as J
    from icloud_mcp.mail import SmtpFailed
    from test_dashboard import api, client, signed_in
    app, mcp, _, _ = web()
    calls = []

    def smtp(self, msg, rcpts):
        calls.append(rcpts)
        if len(calls) == 2:
            raise SmtpFailed("SMTP send failed: timed out.", True) from TimeoutError("timed out")
        return {"tom@example.net": (550, b"No such user here")}
    monkeypatch.setattr(MailService, "_smtp_send", smtp)
    mail = mcp._icloud_dashboard.sources["mail"].mail
    first = mail.send(to=["jana@example.org", "tom@example.net"], subject="Lunch", body="Noon?")["outbox_id"]
    second = mail.send(to=["jana@example.org"], subject="Dinner", body="Seven?")["outbox_id"]
    async with client(app) as c:
        h = api(await signed_in(c))
        for item in (first, second):
            sha = (await c.get(f"/dashboard/api/items/mail:{item}")).json()["sha"]
            got = (await c.post(f"/dashboard/api/items/mail:{item}/approve", json={"sha": sha}, headers=h)).json()
            if item == first:
                assert got["note"] == "Sent to jana@example.org. The mail server refused 1: tom@example.net." and "state" not in got
            else:
                assert got["state"] == "unclear" and "may or may not have gone out" in got["note"]
    lines = {e["details"]["outbox_id"]: e["details"] for e in mcp._icloud_dashboard.audit.entries(20) if e["action"] == "approval.sent"}
    assert lines[first]["refused"] == 1 and lines[first].get("delivery") is None and "action_type" not in lines[first]
    assert lines[second]["delivery"] == "unconfirmed" and "refused" not in lines[second]
    status = J.loads((await mcp.call_tool("approval_status", {"id": second})).content[0].text)
    assert status["status"] == "unclear" and set(status) == {"id", "kind", "status", "edited", "at"}
    assert mail.outbox.state_of(first) == ("sent", None) and mail.outbox.state_of(second) == ("unclear", None)
