"""Editing the text of a waiting request on the dashboard: PUT /dashboard/api/items/{key}/text for mail, iMessage and calendar,
the original kept for "Bring it back", stale versions refused, locked cases, and no text in the activity log."""
import email
import json
from email import policy
from email.message import EmailMessage

from icalendar import Calendar, Event

from icloud_mcp.outbox import Outbox
from test_dashboard import SAME, api, client, queue, signed_in, web  # noqa: F401  (web is a fixture)

IMESSAGE_ENV = dict(ENABLE_IMESSAGE="true", IMESSAGE_ALLOW_SEND="true", IMESSAGE_SEND_ALLOWLIST="anna@example.org",
                    BRIDGE_TOKEN="t" * 40)


def url(key):
    return f"/dashboard/api/items/{key}/text"


def text_of(raw: bytes) -> str:
    return email.message_from_bytes(raw, policy=policy.default).get_body(preferencelist=("plain",)).get_content()


def audit_text(mcp) -> str:
    return (mcp._icloud_dashboard.audit.path).read_text()


def event(raw: bytes):
    return Calendar.from_ical(raw).walk("VEVENT")[0]


def queue_imessage(mcp, text="Running late, start without me"):
    src = mcp._icloud_dashboard.sources["imessage"]
    raw = json.dumps({"chat_id": "chat900", "handle": None, "text": text,
                      "to": {"name": "Hiking crew", "group": True, "participants": [{"handle": "anna@example.org", "name": "Anna"}]}},
                     ensure_ascii=False).encode()
    return src, src.box.add(raw, ["chat900"])


def ics(summary="Autumn fair", description="Bring ideas", events=1) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//test//")
    cal.add("version", "2.0")
    for i in range(events):
        ev = Event()
        ev.add("uid", f"u{i}@example.org")
        ev.add("summary", summary)
        ev.add("dtstart", __import__("datetime").datetime(2026, 10, 22, 18, 0))
        if description:
            ev.add("description", description)
        cal.add_component(ev)
    return cal.to_ical()


def queue_calendar(mcp, op="create", events=1, description="Bring ideas"):
    src = mcp._icloud_dashboard.sources["calendar"]
    meta = {"op": op, "calendar_url": "https://caldav.example.com/cal/", "calendar": "Club", "uid": "u0@example.org",
            "summary": "Autumn fair", "start": "2026-10-22T18:00:00+02:00", "end": None, "location": "",
            "description": description, "organizer": None, "href": None, "etag": None}
    return src, src.cal.outbox.add(ics(description=description, events=events), ["jana@example.org"], meta)


# -- mail ----------------------------------------------------------------------------------------------------------------------
async def test_the_owner_edits_a_mail_and_what_is_sent_is_the_edited_text(web):
    app, mcp, _, sent = web()
    qid = queue(mcp, subject="Lunch", body="See you at noon")
    src = mcp._icloud_dashboard.sources["mail"]
    before = src.pending()[0]
    async with client(app) as c:
        token = await signed_in(c)
        it = (await c.get(f"/dashboard/api/items/mail:{qid}")).json()
        assert it["edit"]["allowed"] is True and it["edit"]["text"].strip() == "See you at noon" and it["edit"]["edited"] is False
        r = await c.put(url(f"mail:{qid}"), json={"sha": it["sha"], "text": "See you at one, ÜBER-café"}, headers=api(token))
        assert r.status_code == 200 and r.json()["changed"] is True and r.json()["sha"] != it["sha"]
        after = src.pending()[0]
        assert text_of(after.raw).strip() == "See you at one, ÜBER-café"
        assert (after.id, after.created_at, after.expires_at, after.recipients) == (before.id, before.created_at, before.expires_at, before.recipients)
        old_msg = email.message_from_bytes(before.raw, policy=policy.default)
        new_msg = email.message_from_bytes(after.raw, policy=policy.default)
        assert [(k, v) for k, v in new_msg.items() if not k.lower().startswith("content-")] == \
               [(k, v) for k, v in old_msg.items() if not k.lower().startswith("content-")]      # From, To, Subject, Message-ID stay
        again = (await c.get(f"/dashboard/api/items/mail:{qid}")).json()
        assert again["sha"] == r.json()["sha"] and again["text"].strip() == "See you at one, ÜBER-café"
        assert again["edit"]["edited"] is True and again["edit"]["original"].strip() == "See you at noon"
        stale = await c.post(f"/dashboard/api/items/mail:{qid}/approve", json={"sha": it["sha"]}, headers=api(token))
        assert stale.status_code == 409 and sent == []                                       # the old version can no longer go out
        ok = await c.post(f"/dashboard/api/items/mail:{qid}/approve", json={"sha": again["sha"]}, headers=api(token))
        assert ok.status_code == 200
    assert [text_of(m.as_bytes()).strip() for m, _ in sent] == ["See you at one, ÜBER-café"] and sent[0][1] == ["bob@example.org"]


async def test_an_edit_keeps_the_attachments_of_a_mail(web):
    app, mcp, *_ = web()
    mail = mcp._icloud_dashboard.sources["mail"].mail
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "me@icloud.com", "bob@example.org", "Files"
    msg.set_content("See attached.")
    msg.add_attachment(b"%PDF-1.4 example", maintype="application", subtype="pdf", filename="Receipts.pdf")
    q = mail.outbox.add(msg.as_bytes(), ["bob@example.org"])
    async with client(app) as c:
        token = await signed_in(c)
        r = await c.put(url(f"mail:{q.id}"), json={"sha": q.sha256, "text": "Receipts are attached."}, headers=api(token))
        assert r.status_code == 200
        it = (await c.get(f"/dashboard/api/items/mail:{q.id}")).json()
        assert [f["filename"] for f in it["files"]] == ["Receipts.pdf"] and it["text"].strip() == "Receipts are attached."


async def test_a_mail_with_an_html_part_cannot_be_edited_and_says_why(web):
    app, mcp, *_ = web()
    mail = mcp._icloud_dashboard.sources["mail"].mail
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "me@icloud.com", "bob@example.org", "Newsletter"
    msg.set_content("Plain version")
    msg.add_alternative("<p>HTML version</p>", subtype="html")
    q = mail.outbox.add(msg.as_bytes(), ["bob@example.org"])
    async with client(app) as c:
        token = await signed_in(c)
        it = (await c.get(f"/dashboard/api/items/mail:{q.id}")).json()
        assert it["edit"]["allowed"] is False and "HTML" in it["edit"]["reason"] and it["edit"]["text"] == ""
        r = await c.put(url(f"mail:{q.id}"), json={"sha": q.sha256, "text": "Changed"}, headers=api(token))
        assert r.status_code == 422 and "HTML" in r.json()["error"]
        assert mail.outbox.pending()[0].raw == q.raw                                          # untouched


# -- the checks every kind shares ----------------------------------------------------------------------------------------------
async def test_a_wrong_or_stale_sha_and_a_missing_session_change_nothing(web):
    app, mcp, *_ = web()
    qid = queue(mcp, body="Original words")
    src = mcp._icloud_dashboard.sources["mail"]
    raw = src.pending()[0].raw
    async with client(app) as c:
        assert (await c.put(url(f"mail:{qid}"), json={"sha": "x", "text": "No"}, headers=SAME)).status_code == 401
        token = await signed_in(c)
        assert (await c.put(url(f"mail:{qid}"), json={"sha": "x", "text": "No"}, headers=SAME)).status_code == 403          # no CSRF token
        evil = {**api(token), "origin": "https://evil.example"}
        assert (await c.put(url(f"mail:{qid}"), json={"sha": "x", "text": "No"}, headers=evil)).status_code == 403
        r = await c.put(url(f"mail:{qid}"), json={"sha": "0" * 64, "text": "No"}, headers=api(token))
        assert r.status_code == 409 and src.pending()[0].raw == raw
        sha = src.pending()[0].sha256
        assert (await c.put(url(f"mail:{qid}"), json={"sha": sha, "text": "First"}, headers=api(token))).status_code == 200
        r = await c.put(url(f"mail:{qid}"), json={"sha": sha, "text": "Second, from an old page"}, headers=api(token))
        assert r.status_code == 409 and text_of(src.pending()[0].raw).strip() == "First"       # two pages cannot both win
        assert (await c.put(url("mail:nothing"), json={"sha": sha, "text": "x"}, headers=api(token))).status_code == 409


async def test_bring_it_back_restores_the_text_the_agent_asked_for(web):
    app, mcp, *_ = web()
    qid = queue(mcp, body="Asked for by the agent")
    src = mcp._icloud_dashboard.sources["mail"]
    async with client(app) as c:
        token = await signed_in(c)
        sha = src.pending()[0].sha256
        r = await c.put(url(f"mail:{qid}"), json={"sha": sha, "restore": True}, headers=api(token))
        assert r.status_code == 422 and "nothing to bring back" in r.json()["error"]
        sha = (await c.put(url(f"mail:{qid}"), json={"sha": sha, "text": "My version"}, headers=api(token))).json()["sha"]
        sha = (await c.put(url(f"mail:{qid}"), json={"sha": sha, "text": "My second version"}, headers=api(token))).json()["sha"]
        assert src.pending()[0].edited_from.strip() == "Asked for by the agent"               # always the agent's text, not the first edit
        r = await c.put(url(f"mail:{qid}"), json={"sha": sha, "restore": True}, headers=api(token))
        assert r.status_code == 200 and r.json()["changed"] is True
        it = (await c.get(f"/dashboard/api/items/mail:{qid}")).json()
        assert it["text"].strip() == "Asked for by the agent" and it["edit"]["edited"] is False and it["edit"]["original"] is None
        # typing the original back by hand is the same as bringing it back
        sha = (await c.put(url(f"mail:{qid}"), json={"sha": it["sha"], "text": "Something else"}, headers=api(token))).json()["sha"]
        r = await c.put(url(f"mail:{qid}"), json={"sha": sha, "text": "Asked for by the agent"}, headers=api(token))
        assert r.status_code == 200 and src.pending()[0].edited_from is None
        # nothing to change: no new version, no log line
        r = await c.put(url(f"mail:{qid}"), json={"sha": r.json()["sha"], "text": "Asked for by the agent"}, headers=api(token))
        assert r.json()["changed"] is False


async def test_the_log_records_that_a_text_was_edited_and_never_the_text(web):
    app, mcp, *_ = web()
    qid = queue(mcp, subject="Plans", body="Original-secret-words")
    src = mcp._icloud_dashboard.sources["mail"]
    async with client(app) as c:
        token = await signed_in(c)
        sha = src.pending()[0].sha256
        sha = (await c.put(url(f"mail:{qid}"), json={"sha": sha, "text": "Edited-secret-words"}, headers=api(token))).json()["sha"]
        await c.put(url(f"mail:{qid}"), json={"sha": sha, "restore": True}, headers=api(token))
        a = (await c.get("/dashboard/api/activity?filter=approvals")).json()
        entries = a["days"][0]["entries"]
        assert [(e["what"], e["tag"]) for e in entries] == [("brought back the original text of email “Plans”", "Edited"),
                                                           ("edited email “Plans” before sending", "Edited")]
        assert entries[1]["note"] == "The text changed. The log keeps no text."
    log = audit_text(mcp)
    assert "Original-secret-words" not in log and "Edited-secret-words" not in log
    edits = [json.loads(line) for line in log.splitlines() if '"approval.edit' in line]
    assert [e["action"] for e in edits] == ["approval.edited", "approval.edit_undone"]
    assert all(e["details"]["kind"] == "mail" and e["details"]["outbox_id"] == qid and e["actor"] == "owner" for e in edits)
    assert mcp._icloud_dashboard.audit.verify()["ok"] is True


async def test_typing_the_original_back_by_hand_is_logged_as_bringing_it_back(web):
    app, mcp, *_ = web()
    qid = queue(mcp, subject="Plans", body="Original words")
    src = mcp._icloud_dashboard.sources["mail"]
    async with client(app) as c:
        token = await signed_in(c)
        sha = (await c.put(url(f"mail:{qid}"), json={"sha": src.pending()[0].sha256, "text": "Changed words"}, headers=api(token))).json()["sha"]
        assert src.pending()[0].edited_from is not None
        r = await c.put(url(f"mail:{qid}"), json={"sha": sha, "text": "Original words"}, headers=api(token))
        assert r.status_code == 200 and r.json()["changed"] is True
        entries = (await c.get("/dashboard/api/activity?filter=approvals")).json()["days"][0]["entries"]
        assert [e["what"] for e in entries] == ["brought back the original text of email “Plans”", "edited email “Plans” before sending"]
    assert src.pending()[0].edited_from is None
    edits = [json.loads(line)["action"] for line in audit_text(mcp).splitlines() if '"approval.edit' in line]
    assert edits == ["approval.edited", "approval.edit_undone"]                          # the same entry "Bring it back" writes


# -- iMessage ----------------------------------------------------------------------------------------------------------------------
async def test_an_imessage_edit_changes_only_the_text_and_the_limit_is_the_one_for_queueing(web):
    app, mcp, *_ = web(**IMESSAGE_ENV)
    src, q = queue_imessage(mcp)
    async with client(app) as c:
        token = await signed_in(c)
        it = (await c.get(f"/dashboard/api/items/imessage:{q.id}")).json()
        assert it["edit"]["allowed"] is True and it["edit"]["text"] == "Running late, start without me" and it["edit"]["limit"] == 10000
        for bad, why in (("   ", "empty"), ("x" * 10001, "10,000"), (None, "missing")):
            r = await c.put(url(f"imessage:{q.id}"), json={"sha": it["sha"], "text": bad}, headers=api(token))
            assert r.status_code == 422 and why in r.json()["error"] and src.pending()[0].sha256 == it["sha"]
        r = await c.put(url(f"imessage:{q.id}"), json={"sha": it["sha"], "text": "  Running fifteen minutes late.  "}, headers=api(token))
        assert r.status_code == 200
        new = src.pending()[0]
        d = json.loads(new.raw)
        assert d["text"] == "Running fifteen minutes late." and d["chat_id"] == "chat900" and d["to"]["group"] is True
        assert (new.recipients, new.expires_at, new.created_at) == (q.recipients, q.expires_at, q.created_at)
        assert new.edited_from == "Running late, start without me"
        r = await c.put(url(f"imessage:{q.id}"), json={"sha": new.sha256, "text": "x" * 10000}, headers=api(token))
        assert r.status_code == 200                                                          # exactly the limit is fine


async def test_an_edited_imessage_is_sent_as_edited_and_an_unconfirmed_send_says_so(web, monkeypatch):
    app, mcp, *_ = web(**IMESSAGE_ENV)
    src, q = queue_imessage(mcp)
    calls = []
    status = {"v": "sent"}
    monkeypatch.setattr(type(src.imessage), "release", lambda self, box, item_id: (
        calls.append(json.loads(box.claim(item_id).raw)["text"]) or {"status": status["v"], "to": "Hiking crew", "note": ""}))
    async with client(app) as c:
        token = await signed_in(c)
        sha = (await c.put(url(f"imessage:{q.id}"), json={"sha": q.sha256, "text": "Edited text"}, headers=api(token))).json()["sha"]
        status["v"] = "unconfirmed"
        r = await c.post(f"/dashboard/api/items/imessage:{q.id}/approve", json={"sha": sha}, headers=api(token))
        assert r.status_code == 200 and r.json()["state"] == "unclear" and calls == ["Edited text"]
        a = (await c.get("/dashboard/api/activity?filter=approvals")).json()["days"][0]["entries"]
        assert [(e["tag"], e["note"]) for e in a if e["tag"] == "Unclear"] == [("Unclear", "Messages did not confirm it. Nothing is sent again.")]
    sent = [json.loads(line) for line in audit_text(mcp).splitlines() if '"approval.sent"' in line]
    assert sent[0]["details"]["delivery"] == "unconfirmed" and sent[0]["details"]["outbox_id"] == q.id


async def test_a_sent_imessage_has_no_state_flag(web, monkeypatch):
    app, mcp, *_ = web(**IMESSAGE_ENV)
    src, q = queue_imessage(mcp)
    monkeypatch.setattr(type(src.imessage), "release", lambda self, box, item_id: (
        box.claim(item_id) and {"status": "sent", "to": "Hiking crew", "note": ""}))
    async with client(app) as c:
        token = await signed_in(c)
        r = await c.post(f"/dashboard/api/items/imessage:{q.id}/approve", json={"sha": q.sha256}, headers=api(token))
        assert r.status_code == 200 and "state" not in r.json() and r.json()["note"].startswith("Sent: iMessage to Hiking crew")


# -- calendar ----------------------------------------------------------------------------------------------------------------------
async def test_a_calendar_edit_changes_the_description_in_the_stored_event(web):
    app, mcp, *_ = web(ALLOW_CALENDAR_INVITES="approve")
    src, q = queue_calendar(mcp)
    async with client(app) as c:
        token = await signed_in(c)
        it = (await c.get(f"/dashboard/api/items/calendar:{q.id}")).json()
        assert it["edit"]["allowed"] is True and it["edit"]["text"] == "Bring ideas"
        r = await c.put(url(f"calendar:{q.id}"), json={"sha": it["sha"], "text": "Bring ideas and chairs"}, headers=api(token))
        assert r.status_code == 200
        new = src.pending()[0]
        ev = event(new.raw)
        assert str(ev["description"]) == "Bring ideas and chairs" and str(ev["summary"]) == "Autumn fair" and str(ev["uid"]) == "u0@example.org"
        assert new.followup["description"] == "Bring ideas and chairs" and new.followup["op"] == "create"
        assert new.recipients == ["jana@example.org"] and new.expires_at == q.expires_at
        again = (await c.get(f"/dashboard/api/items/calendar:{q.id}")).json()
        assert again["text"] == "Bring ideas and chairs" and again["edit"]["original"] == "Bring ideas"
        r = await c.put(url(f"calendar:{q.id}"), json={"sha": new.sha256, "restore": True}, headers=api(token))
        assert r.status_code == 200 and str(event(src.pending()[0].raw)["description"]) == "Bring ideas"
        r = await c.put(url(f"calendar:{q.id}"), json={"sha": src.pending()[0].sha256, "text": ""}, headers=api(token))
        assert r.status_code == 200 and "description" not in event(src.pending()[0].raw)   # may be cleared


async def test_calendar_changes_without_a_description_to_edit_are_locked(web):
    app, mcp, *_ = web(ALLOW_CALENDAR_INVITES="approve")
    async with client(app) as c:
        token = await signed_in(c)
        for op, events, why in (("delete", 1, "cancellation"), ("reply", 1, "cancellation"), ("update", 2, "exactly one event")):
            src, q = queue_calendar(mcp, op=op, events=events, description=f"d-{op}")
            it = (await c.get(f"/dashboard/api/items/calendar:{q.id}")).json()
            assert it["edit"]["allowed"] is False and why in it["edit"]["reason"], op
            r = await c.put(url(f"calendar:{q.id}"), json={"sha": q.sha256, "text": "x"}, headers=api(token))
            assert r.status_code == 422 and why in r.json()["error"] and next(x for x in src.pending() if x.id == q.id).raw == q.raw


# -- the queue ------------------------------------------------------------------------------------------------------------------------
def test_the_original_text_survives_a_restart_and_a_file_added_later(tmp_path):
    box = Outbox(str(tmp_path), 3600, 5)
    q = box.add(b"raw one", ["a@example.org"], {"op": "x"})
    new = box.replace(q.id, q.sha256, b"raw two", edited_from="the first text", followup={"op": "y"})
    assert (new.edited_from, new.followup, new.sha256 == q.sha256) == ("the first text", {"op": "y"}, False)
    assert box.replace(q.id, q.sha256, b"stale", edited_from="no") is None                       # only the version the caller read
    later = box.replace(q.id, new.sha256, b"raw three")                                           # a file added afterwards keeps both
    assert (later.edited_from, later.followup) == ("the first text", {"op": "y"})
    reread = Outbox(str(tmp_path), 3600, 5).pending()[0]
    assert (reread.raw, reread.edited_from, reread.followup) == (b"raw three", "the first text", {"op": "y"})
    plain = box.add(b"other", ["b@example.org"])
    assert "edited_from" not in plain.to_json()


# -- approval_status reads through an edit --------------------------------------------------------------------------------------
async def test_approval_status_is_not_disturbed_by_edit_entries_in_the_log(web):
    app, mcp, *_ = web()
    first, second = queue(mcp, subject="One", body="Asked for one"), queue(mcp, subject="Two", body="Asked for two")
    mail = mcp._icloud_dashboard.sources["mail"]

    async def ask(**args):
        return json.loads((await mcp.call_tool("approval_status", args)).content[0].text)

    cursor = (await ask(since="now"))["cursor"]
    async with client(app) as c:
        token = await signed_in(c)
        for qid, word in ((first, "one"), (second, "two")):
            sha = next(q for q in mail.pending() if q.id == qid).sha256
            sha = (await c.put(url(f"mail:{qid}"), json={"sha": sha, "text": f"Edited {word}"}, headers=api(token))).json()["sha"]
            assert (await ask(id=qid))["status"] == "waiting"                              # an edit leaves it waiting
            sha = (await c.put(url(f"mail:{qid}"), json={"sha": sha, "restore": True}, headers=api(token))).json()["sha"]
            sha = (await c.put(url(f"mail:{qid}"), json={"sha": sha, "text": f"Edited again {word}"}, headers=api(token))).json()["sha"]
            assert (await ask(id=qid))["status"] == "waiting"
            await c.post(f"/dashboard/api/items/mail:{qid}/{'approve' if word == 'one' else 'discard'}", json={"sha": sha}, headers=api(token))
    got = await ask(id=first)
    assert (got["status"], got["kind"]) == ("sent", "mail") and got["id"] == first
    assert (await ask(id=second))["status"] == "discarded"
    listed = await ask(since=cursor)
    assert [(i["id"], i["status"]) for i in listed["items"]] == [(first, "sent"), (second, "discarded")]   # each once, the edits are not outcomes
    log = [json.loads(line) for line in audit_text(mcp).splitlines()]
    assert [e["action"] for e in log if e["action"].startswith("approval.edit")].count("approval.edited") == 4
    assert mcp._icloud_dashboard.audit.verify()["ok"] is True
