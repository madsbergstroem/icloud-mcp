"""The side list's groups (what needs the owner): which waiting request counts as ending soon or needing a close look, who asked,
and that /state carries only counts and the agents' own names. Every name and address is made up."""
from icloud_mcp.dashboard_api import close_look, needs

from test_dashboard import client, queue, signed_in, web  # noqa: F401  (web is a fixture)
from test_text_edit import IMESSAGE_ENV, queue_imessage

NOW = 1_000_000.0


def test_a_close_look_is_any_sign_home_marks_on_the_row():
    plain = {"warning": False, "blocked": False, "group": False}
    assert not close_look(plain)
    for sign in ({"warning": True}, {"blocked": True}, {"held": True}, {"group": True}, {"tags": ["Group"]}):
        assert close_look({**plain, **sign}), sign
    assert not close_look({**plain, "tags": []})


def test_needs_counts_soon_look_and_each_agent_most_first():
    rows = [(False, "NORA", NOW + 3600), (True, "NORA", NOW + 7200), (True, "Winnifred", NOW + 7201), (False, "", NOW + 60),
            (False, "claude code", NOW + 86400), (False, "Bea", NOW + 86400)]
    got = needs(rows, NOW)
    assert got["all"] == 6 and got["look"] == 2 and got["soon_seconds"] == 7200
    assert got["soon"] == 3                                   # 1 h, exactly 2 h and 1 min; 2 h and 1 s is not soon
    assert got["by"] == [["NORA", 2], ["Bea", 1], ["claude code", 1], ["Winnifred", 1]]     # most first, then by name; no empty name


def test_needs_with_nothing_waiting():
    assert needs([], NOW) == {"all": 0, "soon": 0, "look": 0, "soon_seconds": 7200, "by": []}


async def test_state_names_no_recipient_text_or_title_and_follows_the_queue(web):
    app, mcp, *_ = web(**IMESSAGE_ENV, ALLOW_CALENDAR_INVITES="approve")
    queue(mcp, subject="Autumn fair plans")
    async with client(app) as c:
        await signed_in(c)
        first = (await c.get("/dashboard/api/state")).json()
        queue_imessage(mcp, text="Running late for the fair")
        second = (await c.get("/dashboard/api/state")).json()
    assert first["needs"]["all"] == 1 and first["needs"]["look"] == 0
    assert second["needs"]["all"] == 2 and second["needs"]["look"] == 1     # a new request is counted at once (a new fingerprint)
    raw = str(second["needs"])
    for private in ("Autumn", "fair", "Running late", "anna@example.org", "Hiking crew", "Anna"):
        assert private not in raw
