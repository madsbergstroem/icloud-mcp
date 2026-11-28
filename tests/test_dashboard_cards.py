"""What the app's request cards and side list read from the API: what needs the owner, the Mac helper, group chats by name, the account a
mail goes out from, and an unclear send in Sent today and Activity."""
import json

from test_dashboard import api, client, queue, signed_in, web  # noqa: F401  (web is a fixture)
from test_text_edit import IMESSAGE_ENV, answer_with, queue_imessage


async def test_the_state_counts_what_needs_the_owner_and_says_how_the_mac_is(web):
    app, mcp, *_ = web(**IMESSAGE_ENV, ALLOW_CALENDAR_INVITES="approve")
    queue(mcp)
    queue_imessage(mcp)
    queue_imessage(mcp, text="A second one")
    async with client(app) as c:
        await signed_in(c)
        s = (await c.get("/dashboard/api/state")).json()
        today = (await c.get("/dashboard/api/today")).json()["waiting"]
        # the side list: all, ending within 2 h, a close look (the two group chats), who asked; counted by the rows' own flag
        assert s["waiting"] == 3 and s["needs"]["all"] == 3 and s["needs"]["soon"] == 0 and s["needs"]["soon_seconds"] == 7200
        assert s["needs"]["look"] == sum(w["look"] for w in today) == 2
        assert sorted(w["key"] for w in today if w["look"]) == sorted(w["key"] for w in today if "Group" in w.get("tags", []))
        assert "kinds" not in s
        assert s["mac"] == "offline"                               # a helper is set up (BRIDGE_TOKEN) but has not connected


async def test_the_state_says_when_no_mac_helper_is_set_up(web):
    app, mcp, *_ = web()
    async with client(app) as c:
        await signed_in(c)
        s = (await c.get("/dashboard/api/state")).json()
        assert s["mac"] == "off" and s["needs"] == {"all": 0, "soon": 0, "look": 0, "soon_seconds": 7200, "by": []}


async def test_a_named_group_chat_is_titled_by_its_name_and_the_people_stay_in_the_card(web):
    """protects=package 4b's screen table for a group (variant B): title "iMessage", "to <name>" shown under it, the Group tag,
    the bubble, the group warning, the second confirmation with its reason and the note from the Mac; fails_when=any of them is
    missing or the name is back in the title; no_claim=how the page draws them (the QA run)."""
    app, mcp, *_ = web(**IMESSAGE_ENV)
    src, q = queue_imessage(mcp)
    async with client(app) as c:
        await signed_in(c)
        today = (await c.get("/dashboard/api/today")).json()["waiting"][0]
        assert (today["title"], today["to"], today["tags"], today["to_shown"], today["kind_label"]) == \
               ("iMessage", "Hiking crew", ["Group"], True, "iMessage")
        it = (await c.get(f"/dashboard/api/items/external:{q.id}")).json()
        assert ["Chat with", "Anna (anna@example.org)"] in it["rows"] and ["Chat ID", "chat900"] in it["rows"]
        assert it["warnings"] == ["A group conversation: everyone in it receives this."] and it["text_style"] == "bubble"
        assert (it["needs_override"], it["confirm"]) == (True, "Everyone in Hiking crew receives it.")
        assert it["notes"] == ["Sent from your Mac once you approve."] and it["approve_label"] == "Send"


async def test_a_direct_chat_is_not_a_group_and_shows_the_people(web):
    """protects=package 4b's screen table for one person: no Group tag, no second confirmation, the people in the card;
    fails_when=a direct chat asks twice or shows the chat id; no_claim=none beyond protects."""
    from icloud_mcp.imessage import ACTION_ID, params_of
    app, mcp, *_ = web(**IMESSAGE_ENV)
    src = mcp._icloud_dashboard.sources["external"]
    q = src.queue_external(ACTION_ID, params_of({"chat_id": "anna@example.org", "handle": None, "text": "Hi",
                                                 "to": {"name": "", "group": False, "participants": [{"handle": "anna@example.org", "name": "Anna"}]}}), None)
    async with client(app) as c:
        await signed_in(c)
        today = (await c.get("/dashboard/api/today")).json()["waiting"][0]
        assert (today["title"], today["to"], today.get("tags")) == ("iMessage", "Anna (anna@example.org)", None)
        it = (await c.get(f"/dashboard/api/items/external:{q.id}")).json()
        assert it["rows"] == [["Chat with", "Anna (anna@example.org)"]] and it["warnings"] == [] and it["needs_override"] is False


async def test_a_mail_says_which_account_it_goes_out_from(web):
    app, mcp, *_ = web()
    qid = queue(mcp)
    async with client(app) as c:
        await signed_in(c)
        it = (await c.get(f"/dashboard/api/items/external:{qid}")).json()
        assert it["row_tags"] == {"From": "iCloud"}                   # the server's one account, by its provider, on the From row (6b)


async def test_an_unclear_send_is_flagged_in_sent_today_and_activity_spells_imessage_right(web, monkeypatch):
    app, mcp, *_ = web(**IMESSAGE_ENV)
    src, q = queue_imessage(mcp)
    answer_with(src, monkeypatch, "unconfirmed")
    async with client(app) as c:
        token = await signed_in(c)
        r = await c.post(f"/dashboard/api/items/external:{q.id}/approve", json={"sha": q.sha256, "override": True}, headers=api(token))
        assert r.json()["state"] == "unclear"                        # a failure Messages reported is as uncertain as a missing report
        sent = (await c.get("/dashboard/api/today")).json()["sent_today"]
        assert [(e["what"], e["to"], e["unclear"]) for e in sent] == [("iMessage", "Hiking crew", True)]          # D4-2: the log names the chat
        entry = (await c.get("/dashboard/api/activity?filter=approvals")).json()["days"][0]["entries"][0]
        assert entry["what"].startswith("approved iMessage “iMessage”") and entry["tag"] == "Unclear"


async def test_every_calendar_type_reads_as_its_own_card(web):
    """protects=package 5b's screen table for each of the five types (variant C): the title and badge are the type's own, the
    event's name is the subject before "to …", a reply reaches the organizer, the main button and the fixed note with its second
    sentence are the type's, the rows are When, Calendar and Where, and only a new or changed event's description can be edited;
    fails_when=a type shows the event's name as its title, counts people in its note, lists the guests as a row, or lets a
    cancellation's description be edited; no_claim=how the page draws them (the QA run, desktop and 375 px)."""
    from test_text_edit import queue_calendar
    app, mcp, *_ = web(ALLOW_CALENDAR_INVITES="approve")
    want = {"create": ("Invitation", "Add and invite", "invitation to the guests. Nothing is in your calendar yet.", True),
            "update": ("Event update", "Save and notify", "update to the guests. If the event changes", True),
            "delete": ("Cancellation", "Delete and notify", "cancellation to the guests. If the event changes", False),
            "cancel": ("Cancellation", "Cancel and notify", "cancellation to the guests. If the event changes", False),
            "reply": ("Reply", "Send answer", "sends your answer to the organizer. If the event changes", False)}
    async with client(app) as c:
        await signed_in(c)
        for op, (title, button, note, editable) in want.items():
            src, q = queue_calendar(mcp, op=op)
            it = (await c.get(f"/dashboard/api/items/external:{q.id}")).json()
            assert (it["title"], it["badge"], it["kind_label"], it["subject"], it["to"], it["to_shown"], it["approve_label"]) == \
                   (title, title, title, "Autumn fair", "jana@example.org", True, button), op
            assert len(it["notes"]) == 1 and note in it["notes"][0] and "people" not in it["notes"][0], op
            assert [k for k, _ in it["rows"]] == ["When", "Calendar"] and it["text_caption"] == "The description exactly as it will be saved."
            assert it["edit"]["allowed"] is editable and it["text"] == "Bring ideas", op
        listed = {w["key"]: w for w in (await c.get("/dashboard/api/today")).json()["waiting"]}
        assert {(w["title"], w["subject"]) for w in listed.values()} == {(t, "Autumn fair") for t, *_ in want.values()}
