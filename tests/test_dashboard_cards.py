"""What the app's request cards and side list read from the API: kinds waiting, the Mac helper, group chats by name, the account a
mail goes out from, and an unclear send in Sent today and Activity."""
import json

from test_dashboard import api, client, queue, signed_in, web  # noqa: F401  (web is a fixture)
from test_text_edit import IMESSAGE_ENV, queue_imessage


async def test_the_state_counts_what_waits_by_kind_and_says_how_the_mac_is(web):
    app, mcp, *_ = web(**IMESSAGE_ENV, ALLOW_CALENDAR_INVITES="approve")
    queue(mcp)
    queue_imessage(mcp)
    queue_imessage(mcp, text="A second one")
    async with client(app) as c:
        await signed_in(c)
        s = (await c.get("/dashboard/api/state")).json()
        assert s["kinds"] == {"mail": 1, "calendar": 0, "imessage": 2} and s["waiting"] == 3
        assert s["mac"] == "offline"                               # a helper is set up (BRIDGE_TOKEN) but has not connected


async def test_the_state_says_when_no_mac_helper_is_set_up(web):
    app, mcp, *_ = web()
    async with client(app) as c:
        await signed_in(c)
        s = (await c.get("/dashboard/api/state")).json()
        assert s["mac"] == "off" and s["kinds"]["mail"] == 0


async def test_a_named_group_chat_is_titled_by_its_name_and_the_people_stay_in_the_card(web):
    app, mcp, *_ = web(**IMESSAGE_ENV)
    src, q = queue_imessage(mcp)
    async with client(app) as c:
        await signed_in(c)
        today = (await c.get("/dashboard/api/today")).json()["waiting"][0]
        assert (today["title"], today["to"], today["group"], today["kind"]) == ("iMessage to Hiking crew", "Hiking crew", True, "imessage")
        it = (await c.get(f"/dashboard/api/items/imessage:{q.id}")).json()
        assert ["Chat with", "Anna (anna@example.org)"] in it["rows"] and ["Conversation", "chat900"] in it["rows"]
        assert it["group"] is True and it["warnings"] == ["A group conversation: everyone in it receives this."]


async def test_a_direct_chat_is_not_a_group_and_shows_the_people(web):
    app, mcp, *_ = web(**IMESSAGE_ENV)
    src = mcp._icloud_dashboard.sources["imessage"]
    raw = json.dumps({"chat_id": "anna@example.org", "handle": "anna@example.org", "text": "Hi",
                      "to": {"name": "", "group": False, "participants": [{"handle": "anna@example.org", "name": "Anna"}]}}).encode()
    q = src.box.add(raw, ["anna@example.org"])
    async with client(app) as c:
        await signed_in(c)
        today = (await c.get("/dashboard/api/today")).json()["waiting"][0]
        assert (today["title"], today["group"]) == ("iMessage to Anna (anna@example.org)", False)
        it = (await c.get(f"/dashboard/api/items/imessage:{q.id}")).json()
        assert it["rows"] == [["Chat with", "Anna (anna@example.org)"]] and it["warnings"] == []


async def test_a_mail_says_which_account_it_goes_out_from(web):
    app, mcp, *_ = web()
    qid = queue(mcp)
    async with client(app) as c:
        await signed_in(c)
        it = (await c.get(f"/dashboard/api/items/mail:{qid}")).json()
        assert it["account"] == "iCloud"                             # the server's one account, by its provider


async def test_an_unclear_send_is_flagged_in_sent_today_and_activity_spells_imessage_right(web, monkeypatch):
    app, mcp, *_ = web(**IMESSAGE_ENV)
    src, q = queue_imessage(mcp)
    monkeypatch.setattr(type(src.imessage), "release", lambda self, box, item_id: (
        box.claim(item_id) and {"status": "failed", "to": "Hiking crew", "note": ""}))
    async with client(app) as c:
        token = await signed_in(c)
        r = await c.post(f"/dashboard/api/items/imessage:{q.id}/approve", json={"sha": q.sha256}, headers=api(token))
        assert r.json()["state"] == "unclear"                        # a failure Messages reported is as uncertain as a missing report
        sent = (await c.get("/dashboard/api/today")).json()["sent_today"]
        assert [(e["what"], e["unclear"]) for e in sent] == [("iMessage", True)]
        entry = (await c.get("/dashboard/api/activity?filter=approvals")).json()["days"][0]["entries"][0]
        assert entry["what"].startswith("approved iMessage “iMessage to Hiking crew”") and entry["tag"] == "Unclear"
