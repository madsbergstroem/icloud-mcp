"""Files a waiting mail asks for: the agent gives only a name, the mail waits, and the owner (attach page) or an upload
(PUT /files/<token>) adds the file. Nothing goes out while a file is missing, also with owner approval switched off."""
import email
import json
from email import policy

import pytest

from icloud_mcp.mail import PENDING_HEADER, MailError
from test_dashboard import BASE, SAME, client, csrf, signed_in, web  # noqa: F401  -- web is a fixture

PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"


def mail_of(mcp):
    return mcp._icloud_dashboard.sources["mail"].mail


def ask(mcp, **kw):
    return mail_of(mcp).send(to=["anna@example.org"], subject="Quote for the spring order", body="Hi Anna, here is the quote.",
                             attachments=[{"filename": "Quote-2026.pdf", "content_base64": None}], **kw)


def token_of(result):
    return result["files_to_add"][0]["upload_url"].rsplit("/", 1)[1]


async def test_a_mail_without_its_file_waits_and_cannot_be_sent(web, tmp_path):
    app, mcp, _, sent = web()
    r = ask(mcp)
    assert r["status"] == "queued_waiting_for_files" and r["sent"] is False
    assert r["owner_link"] == f"{BASE}/dashboard/attach/{r['outbox_id']}"
    assert r["files_to_add"][0]["filename"] == "Quote-2026.pdf" and r["files_to_add"][0]["upload_url"].startswith(f"{BASE}/files/")
    stored = (tmp_path / "outbox.json").read_text()
    assert token_of(r) not in stored                                   # only the token's hash is kept on the server
    with pytest.raises(MailError, match="still waits for Quote-2026.pdf"):
        mail_of(mcp).release(r["outbox_id"])
    async with client(app) as c:
        token = await signed_in(c)
        q = mail_of(mcp).outbox.pending()[0]
        resp = await c.post("/dashboard/approvals/act", headers={**SAME, "accept": "application/json"},
                            data={"csrf": token, "kind": "mail", "id": q.id, "sha": q.sha256, "action": "approve"})
        assert resp.status_code == 409 and "Add the file first" in resp.json()["error"]
        page = (await c.get(f"/dashboard/approvals?item=mail:{q.id}")).text
        assert "Waiting for: Quote-2026.pdf" in page and f'href="/dashboard/attach/{q.id}"' in page
    assert sent == [] and len(mail_of(mcp).outbox.pending()) == 1      # still waiting, nothing went out


def test_it_waits_even_when_owner_approval_is_off(web):
    _, mcp, _, sent = web(SEND_REQUIRES_APPROVAL="false")
    assert ask(mcp)["status"] == "queued_waiting_for_files" and sent == []


def test_a_draft_cannot_wait_for_a_file(web):
    _, mcp, _, _ = web()
    with pytest.raises(MailError, match="draft cannot wait"):
        ask(mcp, draft=True)


def test_without_the_dashboard_a_file_cannot_wait(tmp_path, monkeypatch):
    from icloud_mcp.config import Settings
    from icloud_mcp.mail import MailService
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", MCP_PUBLIC_URL=BASE,
                     DATA_DIR=str(tmp_path), ALLOW_SEND="true", DASHBOARD="false").items():
        monkeypatch.setenv(k, v)
    with pytest.raises(MailError, match="needs the dashboard"):
        MailService(Settings.from_env()).send(to=["anna@example.org"], subject="x", body="y",
                                              attachments=[{"filename": "Quote-2026.pdf", "content_base64": None}])


async def test_an_agent_upload_fills_the_file_and_the_owner_sends_it(web):
    app, mcp, _, sent = web()
    r = ask(mcp)
    async with client(app) as c:
        put = await c.put(f"/files/{token_of(r)}", content=PDF, headers={"content-type": "application/pdf"})
        assert put.status_code == 200 and put.json()["ok"] and put.json()["still_waiting"] == []
        again = await c.put(f"/files/{token_of(r)}", content=PDF)
        assert again.status_code == 404                                # one file, one upload
        assert (await c.put("/files/not-a-token", content=PDF)).status_code == 404
        token = await signed_in(c)
        q = mail_of(mcp).outbox.pending()[0]
        resp = await c.post("/dashboard/approvals/act", headers={**SAME, "accept": "application/json"},
                            data={"csrf": token, "kind": "mail", "id": q.id, "sha": q.sha256, "action": "approve"})
        assert resp.status_code == 200 and resp.json()["ok"], resp.text
    msg, rcpts = sent[0]
    out = email.message_from_bytes(msg.as_bytes(policy=policy.SMTP), policy=policy.default)
    atts = [p for p in out.iter_attachments()]
    assert rcpts == ["anna@example.org"] and [a.get_filename() for a in atts] == ["Quote-2026.pdf"]
    assert atts[0].get_content() == PDF and atts[0].get_content_type() == "application/pdf"
    assert PENDING_HEADER.lower() not in out.as_string().lower()      # the placeholder's header never leaves


async def test_the_owner_adds_the_file_on_the_attach_page_and_sends_it(web):
    app, mcp, _, sent = web()
    r = ask(mcp)
    path = f"/dashboard/attach/{r['outbox_id']}"
    async with client(app) as c:
        out = await c.get(path)
        assert out.status_code == 303 and out.headers["location"].startswith("/dashboard/signin?next=/dashboard/attach/")
        await signed_in(c)
        page = (await c.get(path)).text
        assert "Drop Quote-2026.pdf here" in page and "Add the file first" in page and 'src="/dashboard/attach.js?v=' in page
        assert "Hi Anna, here is the quote." in page and "anna@example.org" in page
        up = await c.post(f"{path}/file", headers={**SAME, "accept": "application/json"},
                          data={"csrf": csrf(page), "index": "0"}, files={"file": ("Offer_final.pdf", PDF, "application/pdf")})
        got = up.json()
        assert up.status_code == 200 and got["ok"] and got["waiting"] == [] and got["other_name"] and got["asked"] == "Quote-2026.pdf"
        assert "Approve and send" in (await c.get(path)).text and "Add the file first" not in (await c.get(path)).text
        act = await c.post("/dashboard/approvals/act", headers={**SAME, "accept": "application/json"},
                           data={"csrf": csrf(page), "kind": "mail", "id": r["outbox_id"], "sha": got["sha"], "action": "approve"})
        assert act.json()["ok"], act.text
        assert (await c.get(path)).status_code == 404                  # sent: the page says it is no longer waiting
    att = next(sent[0][0].iter_attachments())
    assert att.get_filename() == "Offer_final.pdf" and att.get_content() == PDF


async def test_an_old_hash_or_a_foreign_origin_changes_nothing(web):
    app, mcp, _, sent = web()
    r = ask(mcp)
    old = mail_of(mcp).outbox.pending()[0].sha256
    async with client(app) as c:
        await c.put(f"/files/{token_of(r)}", content=PDF)
        token = await signed_in(c)
        stale = await c.post("/dashboard/approvals/act", headers={**SAME, "accept": "application/json"},
                             data={"csrf": token, "kind": "mail", "id": r["outbox_id"], "sha": old, "action": "approve"})
        assert stale.status_code == 409 and sent == []                 # approved what the owner saw, not what is there now
        foreign = await c.post(f"/dashboard/attach/{r['outbox_id']}/file", headers={"origin": "https://evil.example.net"},
                               data={"csrf": token, "index": "0"}, files={"file": ("x.pdf", PDF, "application/pdf")})
        assert foreign.status_code == 403


async def test_uploads_keep_to_the_size_limit(web):
    app, mcp, _, _ = web(MAX_ATTACHMENT_BYTES="1000")
    r = ask(mcp)
    async with client(app) as c:
        big = await c.put(f"/files/{token_of(r)}", content=b"x" * 1001)
        assert big.status_code == 413 and "MAX_ATTACHMENT_BYTES" in big.json()["error"]
        empty = await c.put(f"/files/{token_of(r)}", content=b"")
        assert empty.status_code == 400 and "empty" in empty.json()["error"]
        ok = await c.put(f"/files/{token_of(r)}?filename=../../Quote.pdf", content=PDF)
        assert ok.status_code == 200 and json.loads(ok.text)["filename"] == "Quote.pdf"   # no folders in a file name


class _Ctx:
    """What the tool sees of the request: the protocol, the client's capabilities and an echoed request_state."""
    def __init__(self, url: bool, version: str = "2026-07-28", state: str | None = None):
        from mcp.types import ClientCapabilities, ElicitationCapability, FormElicitationCapability, UrlElicitationCapability
        self.protocol_version, self.request_state = version, state
        self.client_capabilities = ClientCapabilities(elicitation=ElicitationCapability(
            url=UrlElicitationCapability() if url else None, form=None if url else FormElicitationCapability()))


ARGS = {"to": ["anna@example.org"], "subject": "Quote for the spring order", "body": "Hi Anna, here is the quote.",
        "attachments": [{"filename": "Quote-2026.pdf"}]}


async def test_a_client_that_opens_links_shows_the_owner_the_attach_page_and_the_mail_is_queued_once(web):
    from mcp.types import InputRequiredResult
    _, mcp, _, _ = web()
    tool = mcp._tool_manager.get_tool("mail_send_message")
    first = await tool.run(ARGS, context=_Ctx(url=True))
    assert isinstance(first, InputRequiredResult)
    ask = first.input_requests["add_file"].params
    assert ask.mode == "url" and ask.url.startswith(f"{BASE}/dashboard/attach/") and "Quote-2026.pdf" in ask.message
    assert len(mail_of(mcp).outbox.pending()) == 1
    again = await tool.run(ARGS, context=_Ctx(url=True, state=first.request_state))    # the client's retry after the link
    text = again if isinstance(again, str) else again.content[0].text
    assert json.loads(text)["status"] == "queued_waiting_for_files" and "upload_url" in text
    assert len(mail_of(mcp).outbox.pending()) == 1                                    # asked once, queued once


@pytest.mark.parametrize("ctx", [_Ctx(url=False), _Ctx(url=True, version="2025-11-25")], ids=["form-only", "older-protocol"])
async def test_other_clients_get_the_link_in_the_result(web, ctx):
    _, mcp, _, _ = web()
    out = await mcp._tool_manager.get_tool("mail_send_message").run(ARGS, context=ctx)
    text = out if isinstance(out, str) else out.content[0].text
    assert json.loads(text)["owner_link"].startswith(f"{BASE}/dashboard/attach/")


async def test_the_activity_log_says_the_mail_waits_and_was_not_sent(web, tmp_path):
    _, mcp, _, _ = web()
    await mcp._tool_manager.get_tool("mail_send_message").run(ARGS, context=_Ctx(url=False))
    line = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines() if '"mail_send_message"' in x or "send message" in x][-1]
    assert "waiting for your approval" in line["summary"] and not line["summary"].startswith("Sent")


async def test_the_owner_replaces_an_added_file_before_sending(web):
    app, mcp, _, sent = web()
    r = ask(mcp)
    path = f"/dashboard/attach/{r['outbox_id']}"
    async with client(app) as c:
        await c.put(f"/files/{token_of(r)}", content=b"%PDF-1.4 the wrong one\n", headers={"content-type": "application/pdf"})
        await signed_in(c)
        page = (await c.get(path)).text
        assert "Replace" in page and "Remove" in page and "Approve and send" in page
        up = await c.post(f"{path}/file", headers={**SAME, "accept": "application/json"},
                          data={"csrf": csrf(page), "index": "0"}, files={"file": ("Offer_final.pdf", PDF, "application/pdf")})
        got = up.json()
        assert up.status_code == 200 and got["ok"] and got["index"] == 0 and got["asked"] == "Quote-2026.pdf" and got["other_name"]
        page = (await c.get(path)).text
        assert "Offer_final.pdf" in page and "asked for Quote-2026.pdf" in page and "Other name" in page
        act = await c.post("/dashboard/approvals/act", headers={**SAME, "accept": "application/json"},
                           data={"csrf": csrf(page), "kind": "mail", "id": r["outbox_id"], "sha": got["sha"], "action": "approve"})
        assert act.json()["ok"], act.text
    out = email.message_from_bytes(sent[0][0].as_bytes(policy=policy.SMTP), policy=policy.default)
    atts = list(out.iter_attachments())
    assert [a.get_filename() for a in atts] == ["Offer_final.pdf"] and atts[0].get_content() == PDF   # one file, the new one
    assert "x-icloud-mcp" not in out.as_string().lower()                # the server's own headers never leave


async def test_removing_an_added_file_makes_the_mail_wait_again(web):
    app, mcp, _, sent = web()
    r = ask(mcp)
    path = f"/dashboard/attach/{r['outbox_id']}"
    async with client(app) as c:
        await c.put(f"/files/{token_of(r)}", content=PDF)
        await signed_in(c)
        page = (await c.get(path)).text
        rm = await c.post(f"{path}/remove", headers={**SAME, "accept": "application/json"}, data={"csrf": csrf(page), "index": "0"})
        got = rm.json()
        assert rm.status_code == 200 and got["ok"] and got["waiting"] == ["Quote-2026.pdf"] and got["asked"] == "Quote-2026.pdf"
        page = (await c.get(path)).text
        assert "Drop Quote-2026.pdf here" in page and "Add the file first" in page
        act = await c.post("/dashboard/approvals/act", headers={**SAME, "accept": "application/json"},
                           data={"csrf": csrf(page), "kind": "mail", "id": r["outbox_id"], "sha": got["sha"], "action": "approve"})
        assert act.status_code == 409 and sent == []                   # waiting for the file again, nothing went out
        assert (await c.put(f"/files/{token_of(r)}", content=PDF)).status_code == 404   # the agent's link stays used up
        again = await c.post(f"{path}/remove", headers={**SAME, "accept": "application/json"}, data={"csrf": csrf(page), "index": "0"})
        assert again.status_code == 409                                # a placeholder is not a file to remove
        up = await c.post(f"{path}/file", headers={**SAME, "accept": "application/json"},
                          data={"csrf": csrf(page), "index": "0"}, files={"file": ("Quote-2026.pdf", PDF, "application/pdf")})
        assert up.json()["ok"] and up.json()["waiting"] == []          # the owner adds it again


async def test_the_agents_own_attachment_cannot_be_removed_or_replaced(web):
    import base64
    app, mcp, _, _ = web()
    r = mail_of(mcp).send(to=["anna@example.org"], subject="Quote", body="Hi Anna.",
                          attachments=[{"filename": "Terms.pdf", "content_base64": base64.b64encode(PDF).decode()},
                                       {"filename": "Quote-2026.pdf", "content_base64": None}])
    path = f"/dashboard/attach/{r['outbox_id']}"
    async with client(app) as c:
        await signed_in(c)
        page = (await c.get(path)).text
        assert "Terms.pdf" in page and "Remove" not in page
        rm = await c.post(f"{path}/remove", headers={**SAME, "accept": "application/json"}, data={"csrf": csrf(page), "index": "0"})
        up = await c.post(f"{path}/file", headers={**SAME, "accept": "application/json"},
                          data={"csrf": csrf(page), "index": "0"}, files={"file": ("x.pdf", PDF, "application/pdf")})
        assert rm.status_code == 409 and up.status_code == 409
        foreign = await c.post(f"{path}/remove", headers={"origin": "https://evil.example.net"}, data={"csrf": csrf(page), "index": "1"})
        assert foreign.status_code == 403


async def test_a_token_prefix_names_the_server_in_the_upload_path(web):
    """One proxy in front of several servers sends each upload to the right one by its path, without reading the body."""
    app, mcp, _, _ = web(FILE_TOKEN_PREFIX="a")
    r = ask(mcp)
    url = r["files_to_add"][0]["upload_url"]
    assert url.startswith(f"{BASE}/files/a.") and len(token_of(r)) > 40
    async with client(app) as c:
        assert (await c.put(f"/files/{token_of(r).removeprefix('a.')}", content=PDF)).status_code == 404   # the prefix is part of the key
        ok = await c.put(f"/files/{token_of(r)}", content=PDF)
        assert ok.status_code == 200 and ok.json()["still_waiting"] == []

