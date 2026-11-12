"""The hub's own tools over every service: icloud_list_accounts (every account, the plugins' through their agent_accounts hook) and
messages_archive_chat (the optional `archive` part of the messages capability). Beeper is the gateway of test_beeper; "Parlor" is a
made-up second plugin that brings both hooks with no change to the core. Every name, address, id and number here is made up."""
import json
import logging

import pytest

from icloud_mcp import beeper as B
from icloud_mcp import messages as M
from icloud_mcp import plugins as P
from test_beeper import GROUP, KEY, READ_KEY, SHORT, SINGLE, URL, gateway, hub, launch  # noqa: F401  (fixtures)
from test_dashboard import web  # noqa: F401  (web is a fixture)
from test_external_approvals import audit_text

PASSWORDS = ("aaaa-bbbb-cccc-dddd", "other-password-123", "qqqq-rrrr-ssss-tttt")


async def call(mcp, name, **args):
    return json.loads((await mcp.call_tool(name, args)).content[0].text)


def tools(mcp):
    return {t.name for t in mcp._tool_manager.list_tools()}


def reading(web, hub, **env):
    return launch(web, hub, **{"BEEPER_READ_KEY": READ_KEY, "BEEPER_READ_CHATS": "true", **env})


@pytest.fixture
def chats(gateway):
    gateway.chats[SHORT] = {"id": SHORT, "title": "72345", "type": "single", "network": "SMS", "accountID": "sms",
                            "participants": {"items": [], "total": 2}}
    return gateway


# -- Parlor: a made-up second plugin ----------------------------------------------------------------------------------------------
ARCHIVED: dict[str, bool] = {}


class ParlorBackend:
    name = "parlor"

    def accounts(self):
        return [M.Account("parlor-acct-1", "Parlor", "parlor")]

    def list_chats(self, flt):
        return [M.Chat("parlor:room-1", "Book circle", "group", "parlor-acct-1", last_at="2026-11-12T08:00:00"),
                M.Chat("parlor:room-2", "Quiet room", "group", "parlor-acct-1", last_at="2026-11-12T07:00:00")]

    def read_chat(self, chat_id, limit, cursor):
        raise M.MessagesError("not in this test")

    def search(self, query, chat_id, since, before, limit):
        return M.Matches(())

    def filters(self):
        return M.Filters(shown=lambda native: native != "room-2")          # the owner hides room-2


class ParlorArchiver:
    name = "parlor"

    def archive_chat(self, chat_id, archived):
        if chat_id == "room-lost":
            raise M.Unclear("No answer came back.")
        ARCHIVED[chat_id] = archived
        return {}


def parlor_plugin():
    return P.Plugin(
        id="parlor", name="Parlor", status="Set up by whoever runs this server", limits="Parlor keeps no history.",
        description="Rooms on Parlor, a made-up chat service.",
        capabilities=P.Capabilities(messages=P.MessagesCapability(
            prefix="parlor", backend=lambda s: ParlorBackend(), reading_on=lambda s: True, archive=lambda s: ParlorArchiver())),
        enabled=lambda s: True,
        agent_accounts=lambda s: ("ok", [P.AccountRef("parlor-acct-1", "Parlor", "Robin Example")]))


def broken_plugin():
    def boom(s):
        raise RuntimeError("secret-looking detail")
    return P.Plugin(id="broken", name="Broken", status="", limits="", description="", enabled=lambda s: True, agent_accounts=boom)


@pytest.fixture
def parlor(monkeypatch):
    ARCHIVED.clear()
    monkeypatch.setattr(P, "entry_points", lambda: [("parlor", "test_hub_tools:parlor_plugin"), ("broken", "test_hub_tools:broken_plugin")])


# -- icloud_list_accounts ---------------------------------------------------------------------------------------------------------
async def test_the_deployment_account_is_listed_without_its_password(web):
    _app, mcp, *_ = web()
    got = await call(mcp, "icloud_list_accounts")
    (a,) = got["accounts"]
    assert (a["id"], a["provider"], a["address"], a["default_for"], a["read_only"]) == ("env", "iCloud", "me@icloud.com", a["areas"], False)
    assert set(a) == {"id", "provider", "address", "areas", "default_for", "read_only"}
    assert got["channels"] == [] and "default account" in got["note"]
    assert not any(pw in json.dumps(got) for pw in PASSWORDS)


async def test_saved_accounts_with_defaults_read_only_and_no_secret(web, tmp_path):
    (tmp_path / "accounts.json").write_text(json.dumps({"version": 2, "accounts": [
        {"id": "aa11bb22", "provider": "icloud", "username": "me@icloud.com", "password": PASSWORDS[2], "areas": ["calendar", "contacts"]},
        {"id": "cc33dd44", "provider": "other", "username": "login-name-7", "email_address": "robin@example.org",
         "password": PASSWORDS[1], "areas": ["mail"], "read_only": True, "imap": "imap.example.org:993", "smtp": "smtp.example.org:587"},
    ], "defaults": {"mail": "cc33dd44", "calendar": "aa11bb22", "contacts": "aa11bb22"}}))
    _app, mcp, *_ = web()
    got = await call(mcp, "icloud_list_accounts")
    by_id = {a["id"]: a for a in got["accounts"]}
    assert by_id["aa11bb22"] == {"id": "aa11bb22", "provider": "iCloud", "address": "me@icloud.com", "areas": ["calendar", "contacts"],
                                 "default_for": ["calendar", "contacts"], "read_only": False}
    assert by_id["cc33dd44"] == {"id": "cc33dd44", "provider": "example.org", "address": "robin@example.org", "areas": ["mail"],
                                 "default_for": ["mail"], "read_only": True}
    text = json.dumps(got)
    assert not any(x in text for x in (*PASSWORDS, "imap.example.org", "smtp.example.org", "login-name-7"))   # no password, server or sign-in


async def test_beeper_networks_come_through_the_read_key_without_address_or_number(web, hub, gateway, caplog):
    _app, mcp, _src = launch(web, hub, BEEPER_READ_KEY=READ_KEY)                         # reading stays off
    with caplog.at_level(logging.INFO):
        got = await call(mcp, "icloud_list_accounts")
    (beeper,) = got["channels"]
    assert beeper == {"service": "beeper", "name": "Beeper", "capabilities": ["messages"], "chat_prefix": "beeper", "reading": False,
                      "sending": True, "archiving": False,
                      "accounts": [{"account": "wa-example-1", "network": "WhatsApp", "name": "Alex Example"},
                                   {"account": "signal-example-2", "network": "Signal", "name": "Alex E."}]}      # Beeper's own account left out
    text = json.dumps(got)
    assert "+1555" not in text and "alex@example.org" not in text and "matrix" not in text
    assert set(gateway.seen_keys) == {f"Bearer {READ_KEY}"}
    await call(mcp, "icloud_list_accounts")
    assert gateway.accounts_calls == 1                                                    # kept a few minutes, not asked on every call
    logged = " ".join(r.getMessage() for r in caplog.records if r.name == "icloud_mcp.hub_accounts")
    assert "accounts=1 channels=1 channel_accounts=2" in logged and "Alex" not in logged and "WhatsApp" not in logged


async def test_without_the_read_key_the_service_says_why(web, hub, gateway):
    _app, mcp, _src = launch(web, hub)
    (beeper,) = (await call(mcp, "icloud_list_accounts"))["channels"]
    assert beeper["accounts"] == [] and beeper["problem"] == "no_read_key" and gateway.accounts_calls == 0


async def test_a_second_plugin_brings_its_accounts_and_a_broken_one_does_not_break_the_list(web, parlor):
    _app, mcp, *_ = web()
    got = await call(mcp, "icloud_list_accounts")
    by = {c["service"]: c for c in got["channels"]}
    assert by["parlor"] == {"service": "parlor", "name": "Parlor", "capabilities": ["messages"], "chat_prefix": "parlor", "reading": True,
                            "sending": False, "archiving": True,
                            "accounts": [{"account": "parlor-acct-1", "network": "Parlor", "name": "Robin Example"}]}
    assert by["broken"]["problem"] == "unreadable" and by["broken"]["accounts"] == [] and "secret-looking" not in json.dumps(got)


# -- messages_archive_chat ---------------------------------------------------------------------------------------------------------
async def test_not_offered_without_reading_or_on_a_read_only_server(web, hub, gateway):
    _app, mcp, _src = launch(web, hub, BEEPER_READ_KEY=READ_KEY)                         # set up, reading off
    assert "messages_archive_chat" not in tools(mcp)
    _app, mcp, _src = reading(web, hub, READ_ONLY="true")
    assert "messages_list_chats" in tools(mcp) and "messages_archive_chat" not in tools(mcp)


async def test_without_the_exec_key_there_is_nothing_to_archive_with(web, hub, gateway):
    _app, mcp, *_ = web(BEEPER_GATEWAY_URL=URL, BEEPER_READ_KEY=READ_KEY, BEEPER_READ_CHATS="true")
    assert "messages_list_chats" in tools(mcp) and "messages_archive_chat" not in tools(mcp)


async def test_a_chat_is_archived_and_back_with_the_exec_key_at_once(web, hub, chats, caplog):
    _app, mcp, src = reading(web, hub)
    chats.seen_keys.clear()
    with caplog.at_level(logging.INFO):
        got = await call(mcp, "messages_archive_chat", chat_id=f"beeper:{GROUP}")
    assert got == {"chat_id": f"beeper:{GROUP}", "archived": True} and chats.archived == {GROUP: True}
    assert chats.archive_calls == [{"chatID": GROUP, "archived": True}]
    assert set(chats.seen_keys) == {f"Bearer {KEY}"}                                     # the exec key, never the read key
    assert src.pending() == []                                                            # no approval: it took effect
    again = await call(mcp, "messages_archive_chat", chat_id=f"beeper:{GROUP}", archived=False)
    assert again["archived"] is False and chats.archived == {GROUP: False}
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "chat archive: tool=messages_archive_chat backend=beeper op=archive result=ok" in logged
    assert GROUP not in logged and "Example Club" not in logged                         # no chat id, no title in the server log
    activity = audit_text(mcp)
    assert "Archived a beeper chat" in activity and "Unarchived a beeper chat" in activity and "Example Club" not in activity


def test_the_read_key_cannot_archive(chats):
    s_read = B.Gateway(URL, READ_KEY, 5, make_http=chats.make_http)
    with pytest.raises(B.GatewayError) as e:
        s_read.call("beeper-archive_chat", {"chatID": SINGLE, "archived": True})
    assert e.value.kind == "auth" and chats.archived == {}


@pytest.mark.parametrize("chat_id, env, words", [
    (f"beeper:{SINGLE}", {"BEEPER_HIDDEN_CHATS": SINGLE}, "No conversation"),            # hidden from agents
    (f"beeper:{SINGLE}", {"BEEPER_VISIBLE_CHATS": GROUP}, "No conversation"),            # outside "only these chats"
    (f"beeper:{SHORT}", {}, "No conversation"),                                          # a service sender, hidden by default
    ("beeper:!gone:beeper.test", {}, "No conversation"),                                 # Beeper does not know it
    ("imessage:chat-1", {}, "not offered here"),                                          # no archiving on that channel
    ("Example Club", {}, "starts with its backend"),
])
async def test_what_agents_may_not_see_cannot_be_archived(web, hub, chats, chat_id, env, words):
    _app, mcp, _src = reading(web, hub, **env)
    with pytest.raises(Exception, match=words):
        await call(mcp, "messages_archive_chat", chat_id=chat_id)
    assert chats.archive_calls == []
    assert "Tried to archive" in audit_text(mcp) or words == "starts with its backend"


async def test_a_refusal_from_beeper_and_a_gateway_down_change_nothing(web, hub, chats):
    _app, mcp, _src = reading(web, hub)
    chats.archive_error = True
    with pytest.raises(Exception, match="turned the request away"):
        await call(mcp, "messages_archive_chat", chat_id=f"beeper:{SINGLE}")
    chats.archive_error, chats.mode = False, "down"
    with pytest.raises(Exception, match="Nothing changed"):
        await call(mcp, "messages_archive_chat", chat_id=f"beeper:{SINGLE}")
    assert chats.archived == {}


async def test_another_plugin_archives_through_the_same_tool_with_no_core_change(web, parlor):
    _app, mcp, *_ = web()
    assert "messages_archive_chat" in tools(mcp)
    got = await call(mcp, "messages_archive_chat", chat_id="parlor:room-1")
    assert got == {"chat_id": "parlor:room-1", "archived": True} and ARCHIVED == {"room-1": True}
    with pytest.raises(Exception, match="No conversation"):
        await call(mcp, "messages_archive_chat", chat_id="parlor:room-2")              # the owner hides it: refused before Parlor is asked
    with pytest.raises(Exception, match="No answer came back"):
        await call(mcp, "messages_archive_chat", chat_id="parlor:room-lost")
    assert ARCHIVED == {"room-1": True} and "(unclear)" in audit_text(mcp)


def test_a_hub_takes_an_archiver_only_for_a_channel_it_reads():
    with pytest.raises(ValueError, match="not read here"):
        M.Hub(archivers=[ParlorArchiver()])


async def test_the_surface_of_both_tools(web, hub, chats):
    import jsonschema

    from icloud_mcp.instructions import build_instructions
    _app, mcp, _src = reading(web, hub)
    listed = {t.name: t for t in await mcp.list_tools()}
    for name in ("icloud_list_accounts", "messages_archive_chat"):
        jsonschema.Draft202012Validator.check_schema(listed[name].input_schema)
    arch = listed["messages_archive_chat"]
    assert arch.annotations.read_only_hint is False and arch.annotations.idempotent_hint is True and arch.annotations.destructive_hint is False
    assert arch.input_schema["required"] == ["chat_id"] and listed["icloud_list_accounts"].annotations.read_only_hint is True
    text = mcp._lowlevel_server.instructions
    assert "messages_archive_chat" in text and "icloud_list_accounts" in text
    assert "messages_archive_chat" not in build_instructions(mcp_settings(mcp), {"messages_list_chats", "icloud_list_accounts"})


def mcp_settings(mcp):
    from icloud_mcp.config import Settings
    return Settings.from_env()
