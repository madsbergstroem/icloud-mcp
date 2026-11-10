"""Golden check: mail, iMessage and calendar approvals answer exactly as they did before the hub cut.

One scenario runs through the dashboard API and the two agent tools for each of the three kinds (queue, look, edit, bring back,
approve, a send that is not certain, a send that fails, discard, take back, time out) and records every answer, the activity
log and what approval_status says. Ids, hashes and times are replaced by labels, then the whole record is compared with
tests/fixtures/legacy_approvals_golden.json, which was made from the code as it stood before the change. Run with UPDATE_GOLDEN=1 to
write it again, and only when the answers are meant to change.
"""
import dataclasses
import json
import os
import re
import time
from pathlib import Path

from icloud_mcp.mail import MailService

from test_dashboard import SAME, api, client, queue, signed_in, web  # noqa: F401  (web is a fixture)
from test_text_edit import IMESSAGE_ENV, queue_calendar, queue_imessage

GOLDEN = Path(__file__).parent / "fixtures" / "legacy_approvals_golden.json"
_VOLATILE = {"t", "created_at", "expires_at", "expired_at", "at", "h", "prev", "cursor", "v", "time", "signed_in_at", "last_sent_at", "withdrawn"}


def _scrub(value, ids):
    """The record with every time and hash replaced and every item id named by what it stood for."""
    if isinstance(value, dict):
        return {k: ("<volatile>" if k in _VOLATILE else _scrub(v, ids)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, ids) for v in value]
    if isinstance(value, str):
        for item_id, label in ids.items():
            value = value.replace(item_id, f"<id:{label}>")
        return re.sub(r"\b[0-9a-f]{64}\b", "<sha>", value)
    return value


async def _tool(mcp, name, **args):
    return json.loads((await mcp.call_tool(name, args)).content[0].text)


def _expire(box, item_id):
    box._items[item_id] = dataclasses.replace(box._items[item_id], expires_at=time.time() - 30)


async def _scenario(web, monkeypatch) -> dict:
    app, mcp, _p, sent = web(ALLOW_CALENDAR_INVITES="approve", **IMESSAGE_ENV)
    dash = mcp._icloud_dashboard
    mail_src, msg_src, cal_src = dash.sources["mail"], dash.sources["imessage"], dash.sources["calendar"]
    ids: dict[str, str] = {}
    rec: dict = {}

    def name(label, item_id):
        ids[item_id] = label
        return item_id

    imessage = {"v": "unconfirmed"}
    smtp = {"fail": False}
    real_smtp = MailService._smtp_send

    def smtp_send(self, msg, rcpts):
        if smtp["fail"]:
            raise RuntimeError("The mail server is not answering")
        return real_smtp(self, msg, rcpts)

    monkeypatch.setattr(MailService, "_smtp_send", smtp_send)
    monkeypatch.setattr(type(msg_src.imessage), "release", lambda self, box, item_id: (
        box.claim(item_id) and {"status": imessage["v"], "to": "Hiking crew", "note": ""}))
    monkeypatch.setattr(type(cal_src.cal), "release", lambda self, q: "Added “Autumn fair”. iCloud now emails jana@example.org.")

    mail_id = name("mail-1", queue(mcp, subject="Lunch", body="See you at noon"))
    msg_id = name("imessage-1", queue_imessage(mcp)[1].id)
    cal_id = name("calendar-1", queue_calendar(mcp)[1].id)
    keys = {"mail": f"mail:{mail_id}", "imessage": f"imessage:{msg_id}", "calendar": f"calendar:{cal_id}"}

    async with client(app) as c:
        token = await signed_in(c)
        h = api(token)

        async def get(path):
            r = await c.get(path)
            return {"status": r.status_code, "body": r.json()}

        async def send(method, path, body, headers=h):
            r = await c.request(method, path, json=body, headers=headers)
            return {"status": r.status_code, "body": r.json()}

        rec["today"] = await get("/dashboard/api/today")
        rec["state"] = await get("/dashboard/api/state")
        rec["items"] = {k: await get(f"/dashboard/api/items/{key}") for k, key in keys.items()}
        shas = {k: v["body"]["sha"] for k, v in rec["items"].items()}

        # edit, look again, bring the agent's text back
        rec["edit"] = {k: await send("PUT", f"/dashboard/api/items/{keys[k]}/text", {"sha": shas[k], "text": f"{k} edited, ÜBER-café"})
                       for k in keys}
        rec["items_edited"] = {k: await get(f"/dashboard/api/items/{key}") for k, key in keys.items()}
        shas = {k: v["body"]["sha"] for k, v in rec["items_edited"].items()}
        rec["status_edited"] = {k: await _tool(mcp, "approval_status", id=item_id)
                                for k, item_id in (("mail", mail_id), ("imessage", msg_id), ("calendar", cal_id))}
        rec["restore"] = await send("PUT", f"/dashboard/api/items/{keys['mail']}/text", {"sha": shas["mail"], "restore": True})
        rec["edit_stale"] = await send("PUT", f"/dashboard/api/items/{keys['mail']}/text", {"sha": shas["mail"], "text": "late"})
        rec["edit_bad"] = await send("PUT", f"/dashboard/api/items/{keys['imessage']}/text", {"sha": shas["imessage"], "text": "   "})
        shas["mail"] = (await get(f"/dashboard/api/items/{keys['mail']}"))["body"]["sha"]

        # a refused approve, a send that fails and stays waiting, then each kind approved
        rec["approve_stale"] = await send("POST", f"/dashboard/api/items/{keys['mail']}/approve", {"sha": "0" * 64})
        smtp["fail"] = True
        rec["approve_failed"] = await send("POST", f"/dashboard/api/items/{keys['mail']}/approve", {"sha": shas["mail"]})
        rec["status_after_failed"] = await _tool(mcp, "approval_status", id=mail_id)
        smtp["fail"] = False
        rec["approve"] = {
            "mail": await send("POST", f"/dashboard/api/items/{keys['mail']}/approve", {"sha": shas["mail"]}),
            "imessage": await send("POST", f"/dashboard/api/items/{keys['imessage']}/approve", {"sha": shas["imessage"]}),
            "calendar": await send("POST", f"/dashboard/api/items/{keys['calendar']}/approve", {"sha": shas["calendar"]}),
        }
        rec["sent_mail"] = [[m["To"], m["Subject"]] for m, _rcpts in sent]
        rec["status_ended"] = {k: await _tool(mcp, "approval_status", id=item_id)
                               for k, item_id in (("mail", mail_id), ("imessage", msg_id), ("calendar", cal_id))}

        # a send that is certain, discarded requests, taken-back requests and requests that run out of time
        imessage["v"] = "sent"
        msg2 = name("imessage-2", queue_imessage(mcp, "Second text")[1].id)
        sha2 = (await get(f"/dashboard/api/items/imessage:{msg2}"))["body"]["sha"]
        rec["approve_sent_imessage"] = await send("POST", f"/dashboard/api/items/imessage:{msg2}/approve", {"sha": sha2})
        discarded = {"mail": name("mail-2", queue(mcp, subject="Discard me", body="x")),
                     "imessage": name("imessage-3", queue_imessage(mcp, "Discard this")[1].id),
                     "calendar": name("calendar-2", queue_calendar(mcp)[1].id)}
        rec["discard"] = {}
        for k, item_id in discarded.items():
            sha = (await get(f"/dashboard/api/items/{k}:{item_id}"))["body"]["sha"]
            rec["discard"][k] = await send("POST", f"/dashboard/api/items/{k}:{item_id}/discard", {"sha": sha})
        rec["status_discarded"] = {k: await _tool(mcp, "approval_status", id=item_id) for k, item_id in discarded.items()}
        taken = {"mail": name("mail-3", queue(mcp, subject="Take me back", body="y")),
                 "imessage": name("imessage-4", queue_imessage(mcp, "Take this back")[1].id),
                 "calendar": name("calendar-3", queue_calendar(mcp)[1].id)}
        rec["withdraw"] = {k: await _tool(mcp, "approval_withdraw", id=item_id, reason="mistake") for k, item_id in taken.items()}
        rec["withdraw_again"] = await _tool(mcp, "approval_withdraw", id=taken["mail"])
        lapsing = {"mail": name("mail-4", queue(mcp, subject="Too late", body="z")),
                   "imessage": name("imessage-5", queue_imessage(mcp, "Too late")[1].id),
                   "calendar": name("calendar-4", queue_calendar(mcp)[1].id)}
        for k, box in (("mail", mail_src.mail.outbox), ("imessage", msg_src.box), ("calendar", cal_src.cal.outbox)):
            _expire(box, lapsing[k])
        rec["status_expired"] = {k: await _tool(mcp, "approval_status", id=item_id) for k, item_id in lapsing.items()}
        rec["status_unknown"] = await _tool(mcp, "approval_status", id="never-queued")
        rec["since_start"] = await _tool(mcp, "approval_status", since="0" * 64)
        rec["today_end"] = await get("/dashboard/api/today")
        rec["state_end"] = await get("/dashboard/api/state")
        rec["activity"] = await get("/dashboard/api/activity?filter=approvals")

    rec["audit"] = [{k: v for k, v in e.items() if k not in ("h", "prev")} for e in reversed(dash.audit.entries(1000, ("approval.",)))]
    rec["push_texts"] = {k: v for k, v in __import__("icloud_mcp.push", fromlist=["TEXTS"]).TEXTS.items() if k in ("mail", "calendar", "imessage")}
    return _scrub(rec, ids)


async def test_mail_calendar_and_imessage_answer_as_before(web, monkeypatch):
    got = await _scenario(web, monkeypatch)
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    want = json.loads(GOLDEN.read_text())
    assert json.loads(json.dumps(got, sort_keys=True)) == want
