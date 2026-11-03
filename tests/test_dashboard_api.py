"""The dashboard's JSON API for the Ionic app: sign-in, what waits, acting on it, attachments, activity, security, settings."""
import email
import time
from email.message import EmailMessage

from test_dashboard import BASE, PASSWORD, SAME, client, queue, signed_in, web  # noqa: F401  (web is a fixture)


async def api_session(c):
    r = await c.get("/dashboard/api/session")
    assert r.status_code == 200
    return r.json()


async def signed(c):
    await signed_in(c)
    s = await api_session(c)
    assert s["signed_in"]
    return {**SAME, "x-csrf": s["csrf"]}


async def test_everything_needs_a_session_and_writes_need_the_token(web):
    app, mcp, *_ = web()
    async with client(app) as c:
        assert (await api_session(c))["signed_in"] is False
        for path in ("/dashboard/api/today", "/dashboard/api/activity", "/dashboard/api/security", "/dashboard/api/server",
                     "/dashboard/api/settings"):
            assert (await c.get(path)).status_code == 401, path
        h = await signed(c)
        key = "mail:" + queue(mcp)
        r = await c.post(f"/dashboard/api/items/{key}/discard", json={"sha": "x"}, headers=SAME)
        assert r.status_code == 403
        r = await c.post(f"/dashboard/api/items/{key}/discard", json={"sha": "x"}, headers={**h, "origin": "https://evil.example"})
        assert r.status_code == 403


async def test_password_sign_in_through_the_api(web):
    app, *_ = web()
    async with client(app) as c:
        r = await c.post("/dashboard/api/signin", json={"password": "wrong"}, headers=SAME)
        assert r.status_code == 401 and r.json()["error"] == "Incorrect password."
        r = await c.post("/dashboard/api/signin", json={"password": PASSWORD}, headers=SAME)
        assert r.status_code == 200
        s = await api_session(c)
        assert s["signed_in"] and s["via"] == "the owner password" and s["recent"]


async def test_today_lists_what_waits_and_an_item_opens_with_its_message(web):
    app, mcp, *_ = web()
    async with client(app) as c:
        h = await signed(c)
        qid = queue(mcp, subject="Meeting on Thursday", body="Does 10 work?")
        today = (await c.get("/dashboard/api/today")).json()
        assert [w["title"] for w in today["waiting"]] == ["Meeting on Thursday"]
        assert today["waiting"][0]["to"] == "bob@example.org" and today["waiting"][0]["key"] == f"mail:{qid}"
        it = (await c.get(f"/dashboard/api/items/mail:{qid}")).json()
        assert it["text"].strip() == "Does 10 work?" and it["approve_label"] == "Send" and it["files"] == [] and it["missing"] == []
        r = await c.post(f"/dashboard/api/items/mail:{qid}/approve", json={"sha": it["sha"]}, headers=h)
        assert r.status_code == 200 and "bob@example.org" in r.json()["note"]
        assert (await c.get(f"/dashboard/api/items/mail:{qid}")).status_code == 404
        assert (await c.get("/dashboard/api/today")).json()["waiting"] == []


async def test_a_changed_or_unknown_item_is_refused_and_discard_sends_nothing(web):
    app, mcp, _p, sent = web()
    async with client(app) as c:
        h = await signed(c)
        qid = queue(mcp)
        r = await c.post(f"/dashboard/api/items/mail:{qid}/approve", json={"sha": "0" * 64}, headers=h)
        assert r.status_code == 409 and not sent
        sha = (await c.get(f"/dashboard/api/items/mail:{qid}")).json()["sha"]
        r = await c.post(f"/dashboard/api/items/mail:{qid}/discard", json={"sha": sha}, headers=h)
        assert r.status_code == 200 and r.json()["note"].startswith("Discarded") and not sent


async def test_attachments_are_listed_and_only_safe_kinds_are_previewed(web):
    app, mcp, *_ = web()
    mail = mcp._icloud_dashboard.sources["mail"].mail
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "me@icloud.com", "bob@example.org", "Files"
    msg.set_content("See attached.")
    msg.add_attachment(b"%PDF-1.4 example", maintype="application", subtype="pdf", filename="Receipts.pdf")
    msg.add_attachment(b"MZ\x90\x00", maintype="application", subtype="octet-stream", filename="tool.exe")
    q = mail.outbox.add(msg.as_bytes(), ["bob@example.org"])
    async with client(app) as c:
        await signed(c)
        it = (await c.get(f"/dashboard/api/items/mail:{q.id}")).json()
        files = {f["filename"]: f for f in it["files"]}
        assert files["Receipts.pdf"]["preview"] is True and files["tool.exe"]["preview"] is False
        r = await c.get(f"/dashboard/api/items/mail:{q.id}/files/{files['Receipts.pdf']['index']}")
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
        assert "sandbox" in r.headers["content-security-policy"] and r.headers["x-content-type-options"] == "nosniff"
        r = await c.get(f"/dashboard/api/items/mail:{q.id}/files/{files['tool.exe']['index']}")
        assert r.status_code == 415


async def test_activity_is_grouped_by_day_in_plain_words(web):
    app, *_ = web()
    async with client(app) as c:
        await signed(c)
        a = (await c.get("/dashboard/api/activity")).json()
        assert a["days"][0]["day"] == "Today"
        first = a["days"][0]["entries"][0]
        assert first["who"] == "You" and first["what"] == "signed in with the owner password" and "<" not in first["what"]
        assert a["chain"]["ok"] is True
        assert (await c.get("/dashboard/api/activity?filter=approvals")).json()["days"] == []


async def test_protected_steps_ask_to_confirm_after_five_minutes(web, monkeypatch):
    app, *_ = web()
    async with client(app) as c:
        h = await signed(c)
        real = time.time
        monkeypatch.setattr(time, "time", lambda: real() + 400)
        r = await c.post("/dashboard/api/security/apps/sign-out-all", headers=h)
        assert r.status_code == 403 and r.json()["confirm"] is True
        r = await c.post("/dashboard/api/signin", json={"password": PASSWORD}, headers=h)
        assert r.status_code == 200 and r.json()["confirmed"]
        h = {**SAME, "x-csrf": (await api_session(c))["csrf"]}
        r = await c.post("/dashboard/api/security/apps/sign-out-all", headers=h)
        assert r.status_code == 200 and r.json()["note"].startswith("Signed out 0 apps")


async def test_pause_and_resume_through_the_api(web):
    app, *_ = web()
    async with client(app) as c:
        h = await signed(c)
        assert (await c.post("/dashboard/api/server/pause", json={"paused": True}, headers=h)).json()["paused"] is True
        assert (await c.get("/dashboard/api/state")).json()["paused"] is True
        assert (await c.post("/dashboard/api/server/pause", json={"paused": False}, headers=h)).json()["paused"] is False
        srv = (await c.get("/dashboard/api/server")).json()
        assert srv["tools"] > 0 and srv["bridge"] == {"set_up": False}


async def test_settings_read_and_save_and_a_looser_change_needs_a_fresh_sign_in(web, monkeypatch):
    app, *_ = web()
    async with client(app) as c:
        h = await signed(c)
        st = (await c.get("/dashboard/api/settings")).json()
        cal = next(g for g in st["groups"] if g["slug"] == "calendar")
        tz = next(f for f in cal["fields"] if f["key"] == "DEFAULT_TIMEZONE")
        assert tz["value"] == "Europe/Berlin" and tz["source"] == "environment"
        r = await c.post("/dashboard/api/settings/calendar", json={"values": {"DEFAULT_TIMEZONE": "Mars/Base"}}, headers=h)
        assert r.status_code == 400 and "DEFAULT_TIMEZONE" in r.json()["fields"]
        r = await c.post("/dashboard/api/settings/calendar", json={"values": {"DEFAULT_TIMEZONE": "Europe/Vienna"}}, headers=h)
        assert r.status_code == 200 and r.json()["restart_needed"] is True
        assert (await c.get("/dashboard/api/settings")).json()["restart_needed"] is True
        real = time.time
        monkeypatch.setattr(time, "time", lambda: real() + 400)
        r = await c.post("/dashboard/api/settings/mail", json={"values": {"ALLOW_PERMANENT_DELETE": True}}, headers=h)
        assert r.status_code == 403 and r.json()["confirm"] is True
