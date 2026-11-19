"""Package 6b, criterion 4 (docs/SENDGATE.md): what an agent reads about mail stays the same when mail moves onto the external
source. One scenario calls the four mail tools that queue (a plain mail, one that waits for a file, a reply, a forward, a saved
draft and the same draft again), uploads the missing file, asks approval_status, takes one back with approval_withdraw and asks
again after the owner decided. Every answer is recorded as JSON with ids, tokens, hashes, Message-IDs and times replaced by labels,
and compared with tests/fixtures/mail_answers_golden.json, which was made from package 6a (commit 05727aa). Run with
UPDATE_GOLDEN=1 to write it again, and only when an answer is meant to change.

approval_status gains action_type for mail with 6b (D6-8, the owner's answer); the comparison leaves that one key out and the
test checks it on its own."""
import email
import json
import os
import re
from email import policy
from pathlib import Path

from icloud_mcp.mail import MailService

from test_dashboard import api, client, signed_in, web  # noqa: F401  (web is a fixture)

GOLDEN = Path(__file__).parent / "fixtures" / "mail_answers_golden.json"
ORIGINAL = (b"From: Jana Example <jana@example.org>\r\nTo: me@icloud.com\r\nSubject: Invoice 2291\r\n"
            b"Message-ID: <inv2291@example.org>\r\nDate: Mon, 16 Nov 2026 09:00:00 +0100\r\n\r\nPlease confirm the invoice.\r\n")
DRAFT = (b"From: me@icloud.com\r\nTo: tom@example.net\r\nSubject: Holiday rota\r\nMessage-ID: <rota1@icloud.com>\r\n"
         b"Date: Tue, 17 Nov 2026 10:00:00 +0100\r\n\r\nHere is the rota for December.\r\n")
TYPES = {"send": "mail.send", "reply": "mail.reply", "forward": "mail.forward", "draft": "mail.send"}


def _scrub(value, ids):
    if isinstance(value, dict):
        return {k: ("<volatile>" if k in ("at", "expires_in_seconds") else _scrub(v, ids)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, ids) for v in value]
    if isinstance(value, str):
        for item_id, label in ids.items():
            value = value.replace(item_id, f"<id:{label}>")
        value = re.sub(r"/files/[A-Za-z0-9_.-]+", "/files/<token>", value)
        value = re.sub(r"<[^<>@\s]+@[^<>@\s]+>", "<message-id>", value) if value.startswith("<") and value.endswith(">") else value
        return re.sub(r"\b[0-9a-f]{64}\b", "<sha>", value)
    return value


async def _tool(mcp, name, **args):
    return json.loads((await mcp.call_tool(name, args)).content[0].text)


async def _scenario(web, monkeypatch) -> tuple[dict, dict]:
    app, mcp, _, sent = web()
    monkeypatch.setattr(MailService, "_fetch_readable", lambda self, c, folder, uid, readonly=True, uidvalidity=None: (ORIGINAL, (), None, 7, None))
    monkeypatch.setattr(MailService, "_fetch_raw", lambda self, c, folder, uid, readonly=True, uidvalidity=None: (ORIGINAL, (), None, 7))
    monkeypatch.setattr(MailService, "_load_draft", lambda self, c, folder, uid, uv: (
        "Drafts", email.message_from_bytes(DRAFT, policy=policy.default), 7))
    ids: dict[str, str] = {}
    rec: dict = {}
    types: dict = {}

    def keep(label, answer):
        if isinstance(answer, dict) and answer.get("outbox_id"):
            ids.setdefault(answer["outbox_id"], label)
        rec[label] = answer
        return answer

    send = keep("send", await _tool(mcp, "mail_send_message", to=["jana@example.org"], subject="Lunch on Friday",
                                     body="Hi Jana, noon at the usual place? Sam"))
    waits = keep("send_waiting_for_file", await _tool(mcp, "mail_send_message", to=["jana@example.org"], subject="Quote",
                                                      body="The quote is attached.",
                                                      attachments=[{"filename": "Quote.pdf", "content_base64": None}]))
    reply = keep("reply", await _tool(mcp, "mail_reply_to_message", folder="INBOX", uid=7, body="Confirmed, we pay on Friday."))
    forward = keep("forward", await _tool(mcp, "mail_forward_message", folder="INBOX", uid=7, to=["tom@example.net"], note="FYI"))
    draft = keep("draft", await _tool(mcp, "mail_send_draft", uid=4, uidvalidity=7))
    keep("draft_again", await _tool(mcp, "mail_send_draft", uid=4, uidvalidity=7))
    for label, got in (("send", send), ("reply", reply), ("forward", forward), ("draft", draft)):
        types[label] = got["outbox_id"]
    token = waits["files_to_add"][0]["upload_url"].rsplit("/", 1)[1]
    async with client(app) as c:
        up = await c.put(f"/files/{token}?filename=Quote.pdf", content=b"%PDF-1.4 made up", headers={"content-type": "application/pdf"})
        rec["upload"] = {"status": up.status_code, "body": up.json()}
        again = await c.put(f"/files/{token}?filename=Quote.pdf", content=b"%PDF-1.4 made up", headers={"content-type": "application/pdf"})
        rec["upload_again"] = {"status": again.status_code, "body": again.json()}
        statuses = {label: await _tool(mcp, "approval_status", id=item) for label, item in types.items()}
        rec["withdraw"] = await _tool(mcp, "approval_withdraw", id=forward["outbox_id"], reason="duplicate")
        rec["withdraw_again"] = await _tool(mcp, "approval_withdraw", id=forward["outbox_id"])
        h = api(await signed_in(c))
        for item in (send["outbox_id"], draft["outbox_id"]):
            key = next(w["key"] for w in (await c.get("/dashboard/api/today")).json()["waiting"] if w["key"].endswith(item))
            sha = (await c.get(f"/dashboard/api/items/{key}")).json()["sha"]
            assert (await c.post(f"/dashboard/api/items/{key}/approve", json={"sha": sha}, headers=h)).status_code == 200
    statuses["send_sent"] = await _tool(mcp, "approval_status", id=send["outbox_id"])
    statuses["forward_withdrawn"] = await _tool(mcp, "approval_status", id=forward["outbox_id"])
    keep("draft_after_sent", await _tool(mcp, "mail_send_draft", uid=4, uidvalidity=7))
    rec["withdraw_sent"] = await _tool(mcp, "approval_withdraw", id=send["outbox_id"])
    assert len(sent) == 2
    want_type = {"send": "mail.send", "reply": "mail.reply", "forward": "mail.forward", "draft": "mail.send",
                 "send_sent": "mail.send", "forward_withdrawn": "mail.forward"}
    action_types = {label: s.pop("action_type", None) for label, s in statuses.items()}
    rec["approval_status"] = statuses
    return _scrub(rec, ids), {label: (action_types[label], want_type[label]) for label in statuses}


async def test_every_answer_an_agent_reads_about_mail_stays_the_same(web, monkeypatch):
    """protects=package 6b criterion 4: the four mail tools that queue (also for a missing file, a repeated draft and a draft whose
    mail was sent), the upload, approval_status (kind mail) and approval_withdraw answer as in package 6a, key by key and value by
    value; fails_when=a field is added, dropped, renamed or changes its value or its kind, or a mail's link names another kind;
    no_claim=what the owner sees (the card tests), and action_type in approval_status, which D6-8 adds (checked below)."""
    got, action_types = await _scenario(web, monkeypatch)
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    assert got == json.loads(GOLDEN.read_text())
    for label, (have, want) in action_types.items():       # D6-8: kind stays mail; the type comes along with 6b
        assert have == want, label
