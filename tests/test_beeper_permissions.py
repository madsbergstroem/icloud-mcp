"""Who Beeper may write to (plugins/beeper, dashboard_accounts.register_picker_api): the three rules (list, networks, groups), the
chats on the list by name, finding a chat to add, and the read key that reads and never sends. The gateway is the made-up one of
test_beeper; the text shapes are the ones Beeper's MCP answers with, with made-up values."""
import logging

import pytest

from icloud_mcp import beeper as B
from icloud_mcp.config import Settings
from test_beeper import GROUP, KEY, READ_KEY, SINGLE, URL, ask, gateway, hub, launch  # noqa: F401  (fixtures)
from test_dashboard import client, signed_in, web  # noqa: F401  (web is a fixture)

# search_chats as the owner's gateway answers it (structuredContent is null), values made up
SEARCH_TEXT = """# Chats

Found 1+ chats (showing 1)
Next page (older): cursor='eyJ0', direction='before'
Previous page (newer): cursor='eyJ1', direction='after'

## Riverside climbing (chatID: !AbC123:beeper.local)
Chat on Signal (signal-example-2) with Jamie Rivera, Sam Taylor, Alex Example.
**Type**: group
**Last Activity**: 2026-11-12T08:41:38.000Z
This chat is muted.

# Using this information

- Use the chatID with get_chat.
"""


def test_search_chats_text_is_read_as_beeper_writes_it():
    got = B._chats_of(B.Answer(False, None, SEARCH_TEXT))
    assert got == [("!AbC123:beeper.local", B.Chat("Riverside climbing", "Signal", True, 3, "2026-11-12T08:41:38.000Z"))]
    assert B._chats_of(B.Answer(False, None, "# Chats\n\nFound 0 chats\n\n# Using this information\n")) == []
    assert B._chats_of(B.Answer(True, None, SEARCH_TEXT)) is None


def test_the_read_tools_are_named_after_the_read_server(monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="a@example.org", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", BEEPER_READ_SERVER="beeper_alex").items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("BEEPER_ACCOUNTS_TOOL", raising=False)
    s = Settings.from_env()
    assert [B.read_tool(s, n) for n in ("get_chat", "search_chats", "get_accounts")] == \
        ["beeper_alex-get_chat", "beeper_alex-search_chats", "beeper_alex-get_accounts"]
    monkeypatch.setenv("BEEPER_ACCOUNTS_TOOL", "beeper_old-get_accounts")              # still honoured where it is set
    assert B.read_tool(Settings.from_env(), "get_accounts") == "beeper_old-get_accounts"
    monkeypatch.delenv("BEEPER_READ_SERVER")
    assert B.read_tool(Settings.from_env(), "get_chat") == "beeper-get_chat"            # without a read server, the old names


# -- the three rules ------------------------------------------------------------------------------------------------------------
async def test_lists_of_single_chats_as_they_are_live_keep_working_with_the_new_defaults(web, hub, gateway):
    gateway.chats.update({cid: {"id": cid, "title": f"Person {cid}", "type": "single", "network": "Telegram", "accountID": "tg",
                                "participants": {"items": [], "total": 2}} for cid in ("329", "2561", "363")})
    app, mcp, src = launch(web, hub, allow="329,2561", BEEPER_SEND_GROUPS="")             # groups and networks left at their defaults
    for cid in ("329", "2561"):
        assert (await ask(mcp, cid))["status"] == "queued_for_owner_approval"


async def test_group_chats_are_off_by_default_even_when_the_group_is_on_the_list(web, hub, gateway):
    app, mcp, src = launch(web, hub, BEEPER_SEND_GROUPS="")
    with pytest.raises(Exception, match="group chats are off"):
        await ask(mcp, GROUP)
    assert (await ask(mcp, SINGLE))["status"] == "queued_for_owner_approval" and len(src.pending()) == 1


async def test_only_the_networks_that_are_on_may_be_written_to(web, hub, gateway):
    app, mcp, src = launch(web, hub, BEEPER_SEND_NETWORKS="Signal")
    with pytest.raises(Exception, match="writing on WhatsApp is off"):
        await ask(mcp, SINGLE)                                                             # a WhatsApp chat
    assert (await ask(mcp, GROUP))["status"] == "queued_for_owner_approval"              # Signal, a group on the list, groups on


async def test_the_rules_are_checked_again_when_the_owner_approves(web, hub, gateway):
    """A rule that changed after the request was queued (here: the policy now refuses groups) stops it at approval; nothing is sent."""
    import dataclasses
    app, mcp, src = launch(web, hub)
    await ask(mcp, GROUP)
    (q,) = src.pending()
    reg = src.registry.get(B.ACTION_ID)
    src.registry._items[B.ACTION_ID] = dataclasses.replace(reg, policy=lambda p: "Not permitted: group chats are off.")
    with pytest.raises(Exception, match="group chats are off"):
        src.release(q)
    assert gateway.sent == []


# -- the picker -----------------------------------------------------------------------------------------------------------------
async def _get(app, path):
    async with client(app) as c:
        await signed_in(c)
        return await c.get("/dashboard/api" + path)


async def test_the_list_by_name_with_a_chat_beeper_no_longer_knows(web, hub, gateway, caplog):
    caplog.set_level(logging.DEBUG)
    app, mcp, _src = launch(web, hub, allow=f"{SINGLE},{GROUP},!gone:beeper.test", BEEPER_READ_KEY=READ_KEY)
    r = (await _get(app, "/plugins/beeper/recipients")).json()
    assert r["state"] == "ok" and r["any"] is False and r["groups"] is True and r["networks"] == ["*"]
    assert [(c["title"], c["network"], c["group"], c["people"], c["found"]) for c in r["chats"]] == [
        ("Example Person", "WhatsApp", False, 2, True), ("Example Club", "Signal", True, 3, True), ("", "", False, None, False)]
    assert r["all_networks"] == ["WhatsApp", "Signal"] and r["keys"]["recipients"] == "BEEPER_SEND_ALLOWLIST"
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("icloud_mcp"))   # not the made-up gateway's own
    assert "Example Club" not in ours and "Jamie Rivera" not in ours and "!gone" not in ours
    assert "beeper chats: ok (3 on the list, 1 not found)" in ours


async def test_finding_a_chat_by_name_and_the_recent_ones(web, hub, gateway):
    app, mcp, _src = launch(web, hub, allow=SINGLE, BEEPER_READ_KEY=READ_KEY)
    found = (await _get(app, "/plugins/beeper/find?q=club")).json()
    assert [(c["title"], c["group"], c["people"], c["added"]) for c in found["chats"]] == [("Example Club", True, 3, False)]
    recent = (await _get(app, "/plugins/beeper/find?q=")).json()
    assert [(c["title"], c["added"]) for c in recent["chats"]] == [("Example Person", True), ("Example Club", False)]
    assert gateway.search_calls == [{"query": "club", "limit": 20}, {"query": "", "limit": 10}]
    assert set(gateway.seen_keys[-2:]) == {f"Bearer {READ_KEY}"}                          # the read key reads; it never sends
    gone = await _get(app, "/plugins/beeper/resolve?id=!gone:beeper.test")
    assert gone.status_code == 404 and "Nothing was added" in gone.json()["error"]
    one = (await _get(app, f"/plugins/beeper/resolve?id={GROUP}")).json()["chat"]
    assert (one["id"], one["group"], one["people"]) == (GROUP, True, 3)


async def test_without_the_read_key_the_page_says_so_and_asks_nobody(web, hub, gateway):
    app, mcp, _src = launch(web, hub)
    r = (await _get(app, "/plugins/beeper/recipients")).json()
    assert r["state"] == "no_read_key" and r["chats"] == [] and r["ids"] == [SINGLE, GROUP]
    assert (await _get(app, "/plugins/beeper/find?q=x")).json()["state"] == "no_read_key" and gateway.search_calls == []


def test_the_read_key_cannot_send(gateway):
    """The gateway's grants: a send with the read key is turned away, so nothing here could send with it even by mistake."""
    g = B.Gateway(URL, READ_KEY, 5, make_http=gateway.make_http)
    with pytest.raises(B.GatewayError):
        g.call("beeper_send-send_message", {"chatID": SINGLE, "text": "x"})
    assert gateway.sent == []
    assert KEY != READ_KEY
