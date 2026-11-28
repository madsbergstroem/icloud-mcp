"""plugin_api version 1 (docs/SENDGATE.md, package 7): what a plugin imports, which version it names, what every callback is
handed, where its secrets live, its lists by request, and that Beeper runs on that contract alone. Every name is made up."""
import ast
import dataclasses
import logging
from pathlib import Path

import pytest

from icloud_mcp import config as C
from icloud_mcp import plugins as P
from icloud_mcp.config import Settings
from test_dashboard import client, signed_in, web  # noqa: F401  (web is a fixture)

ROOT = Path(__file__).resolve().parents[1]
EXEC, READ = "made-up-exec-key-7f3a91", "made-up-read-key-c41d02"


@pytest.fixture(autouse=True)
def fresh_plugins():
    P._loaded.cache_clear()
    yield
    P._loaded.cache_clear()


def _env(monkeypatch, **env):
    for k, v in dict(ICLOUD_USERNAME="jana@example.org", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", **env).items():
        monkeypatch.setenv(k, v)


# -- criterion 1: Beeper's settings left the core --------------------------------------------------------------------------------
def test_settings_has_no_beeper_field():
    """protects=the core's settings know no plugin by name; fails_when=a beeper_* field comes back; no_claim=none."""
    assert not [f.name for f in dataclasses.fields(Settings) if f.name.startswith("beeper")]
    assert "beeper" not in (ROOT / "src" / "icloud_mcp" / "config.py").read_text().lower()


# -- criterion 2: Beeper imports only the contract and itself -------------------------------------------------------------------
def test_beeper_imports_only_plugin_api_and_its_own_package():
    """protects=the shipped plugin lives on the public contract; fails_when=a module of plugins/beeper imports any other module of
    icloud_mcp, at the top or inside a function; no_claim=third-party imports (mcp) are allowed."""
    bad = []
    for path in sorted((ROOT / "src" / "icloud_mcp" / "plugins" / "beeper").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                own = node.level == 1                                       # from . import gateway
                api = (node.level == 3 and node.module == "plugin_api") or (node.level == 3 and not node.module and
                                                                             [a.name for a in node.names] == ["plugin_api"])
                absolute_ok = node.level == 0 and not (node.module or "").startswith("icloud_mcp")
                if not (own or api or absolute_ok or (node.level == 0 and node.module == "icloud_mcp.plugin_api")):
                    bad.append(f"{path.name}: from {'.' * node.level}{node.module or ''}")
            elif isinstance(node, ast.Import):
                bad += [f"{path.name}: import {a.name}" for a in node.names if a.name.startswith("icloud_mcp")]
    assert not bad, bad


def test_plugin_api_names_what_it_promises():
    """protects=the documented names are there; fails_when=one goes missing; no_claim=their behaviour."""
    from icloud_mcp import plugin_api as API
    assert API.API_VERSION == 1 and P.API_VERSIONS == {1}
    for name in API.__all__:
        assert hasattr(API, name), name


# -- criterion 3: the version check ---------------------------------------------------------------------------------------------
def future_plugin():
    return P.Plugin(id="parlor", name="Parlor", status="Set up by whoever runs this server", limits="Made up.",
                    description="A made-up service from a newer server.", enabled=lambda s: True, api=2)


def old_plugin():
    return P.Plugin(id="mailbox", name="Mailbox", status="Set up by whoever runs this server", limits="Made up.",
                    description="A made-up service written before plugin_api.", enabled=lambda s: True)


def test_a_plugin_for_a_newer_api_is_skipped_and_one_without_api_loads_with_a_warning(monkeypatch, caplog):
    """protects=a plugin written against another contract never runs half-broken; fails_when=api=2 loads, the log has no line, or
    a plugin without api is refused during v1; no_claim=the wording of the log line."""
    monkeypatch.setattr(P, "entry_points", lambda: [("parlor", "test_plugin_api:future_plugin"), ("mailbox", "test_plugin_api:old_plugin")])
    with caplog.at_level(logging.WARNING, logger="icloud_mcp.plugins"):
        ids = [p.id for p in P.load_plugins()]
    assert ids == ["beeper", "gateway", "mailbox"]
    lines = [r.getMessage() for r in caplog.records]
    assert sum("parlor" in m and "plugin_api 2" in m for m in lines) == 1
    assert sum("mailbox" in m and "loaded as version 1" in m for m in lines) == 1
    assert P.skipped() == [{"id": "parlor", "name": "Parlor", "why": "Needs a newer server"}]


def odd_plugin():
    return P.Plugin(id="odd", name="Odd", status="Set up by whoever runs this server", limits="Made up.",
                    description="A made-up plugin naming its version as text.", enabled=lambda s: True, api="1")


def test_a_plugin_with_a_version_that_is_no_number_is_skipped_and_the_rest_load(monkeypatch):
    """protects=one badly written plugin never takes the server down; fails_when=api="1" raises while the plugins load or is
    loaded; no_claim=none. Found by the review of package 7."""
    monkeypatch.setattr(P, "entry_points", lambda: [("odd", "test_plugin_api:odd_plugin"), ("mailbox", "test_plugin_api:old_plugin")])
    assert [p.id for p in P.load_plugins()] == ["beeper", "gateway", "mailbox"]
    assert P.skipped() == [{"id": "odd", "name": "Odd", "why": "Names no version this server knows"}]


def test_the_skipped_list_belongs_to_the_set_of_plugins_now_loaded(monkeypatch):
    """protects=Connect names what this server left out, not what an earlier set did; fails_when=a plugin skipped in one load
    still shows after the plugins changed; no_claim=none. Found by the review of package 7."""
    monkeypatch.setattr(P, "entry_points", lambda: [("parlor", "test_plugin_api:future_plugin")])
    assert [x["id"] for x in P.skipped()] == ["parlor"]
    monkeypatch.setattr(P, "entry_points", lambda: [("mailbox", "test_plugin_api:old_plugin")])
    assert P.skipped() == []


async def test_connect_shows_the_skipped_plugin_and_the_server_starts(web, monkeypatch):
    """protects=the owner sees why a plugin is not there, and the rest works; fails_when=the row is missing or the server does not
    start; no_claim=the look of the row."""
    monkeypatch.setattr(P, "entry_points", lambda: [("parlor", "test_plugin_api:future_plugin")])
    app, mcp, *_ = web()
    async with client(app) as c:
        await signed_in(c)
        r = (await c.get("/dashboard/api/accounts")).json()
    assert r["skipped"] == [{"id": "parlor", "name": "Parlor", "why": "Needs a newer server"}]
    assert "parlor" not in {p["id"] for p in r["plugins"]}
    assert "icloud_check_health" in {t.name for t in await mcp.list_tools()}


# -- criterion 4: every callback gets a PluginContext ---------------------------------------------------------------------------
SEEN: dict[str, object] = {}


def _seen(kind, ctx, out=None):
    SEEN[kind] = ctx
    return out


class _Backend:
    name = "recorder"

    def accounts(self):
        return []

    def list_chats(self, flt):
        return []

    def read_chat(self, chat_id, limit, cursor):
        raise NotImplementedError

    def search(self, query, chat_id, since, before, limit):
        from icloud_mcp.plugin_api import Matches
        return Matches(())

    def filters(self):
        from icloud_mcp.plugin_api import Filters
        return Filters()


class _Sender:
    name, takes_handle = "recorder", False

    def send_request(self, req):
        raise NotImplementedError


def recorder_plugin():
    from icloud_mcp import plugin_api as API
    send = API.ActionType(id="recorder.message.send", title="Recorder message", badge="Recorder", approve_label="Send",
                          params_schema={"type": "object", "properties": {"chat_id": {"type": "string"}, "text": {"type": "string"}},
                                         "required": ["chat_id", "text"], "additionalProperties": False},
                          rows=(("Chat", "chat_id"),), text_field="text", recipients=lambda p: [p["chat_id"]])
    tidy = API.ActionType(id="recorder.tidy", title="Tidy", badge="Recorder", approve_label="Tidy",
                          params_schema={"type": "object", "properties": {"shelf": {"type": "string"}}, "required": ["shelf"],
                                         "additionalProperties": False},
                          rows=(("Shelf", "shelf"),), recipients=lambda p: [p["shelf"]])

    class Ex:
        def execute(self, request):
            return API.Result("sent", "Done.")
    return API.Plugin(
        id="recorder", name="Recorder", status="Set up by whoever runs this server", limits="Made up.", description="Records contexts.",
        capabilities=API.Capabilities(messages=API.MessagesCapability(
            prefix="recorder", action_type=send,
            executor=lambda c: _seen("executor", c, Ex()),
            sender=lambda c, ext: _seen("sender", c, _Sender()),
            backend=lambda c: _seen("backend", c, _Backend()),
            allowed=lambda c: _seen("allowed", c, ["*"]),
            policy=lambda c: _seen("policy", c, None),
            search_chats=lambda c, q: _seen("search_chats", c, ("ok", [])),
            resolve=lambda c, ids: _seen("resolve", c, ("ok", [])),
            networks=lambda c: _seen("networks", c, []),
            sending_on=lambda c: _seen("sending_on", c, True),
            reading_on=lambda c: _seen("reading_on", c, True),
            archive=lambda c: _seen("archive", c, None))),
        settings_keys=frozenset({"RECORDER_SHELVES"}),
        enabled=lambda c: _seen("enabled", c, True),
        secrets=lambda c: _seen("secrets", c, ()),
        health=lambda c: _seen("health", c, {"ok": True}),
        accounts=lambda c: _seen("accounts", c, lambda: API.Listing("Shelves")),
        agent_accounts=lambda c: _seen("agent_accounts", c, ("ok", [])),
        actions=(API.ActionSpec(action_type=tidy, executor=lambda c: _seen("action_executor", c, Ex()),
                                allowed=lambda c: _seen("action_allowed", c, ["*"]), policy=lambda c: _seen("action_policy", c, None)),),
        api=1)


async def test_every_callback_kind_receives_a_plugin_context_and_nothing_of_the_core(web, monkeypatch):
    """protects=a plugin is handed its own view, never the server's settings; fails_when=any callback gets Settings, or the context
    lets a plugin read the owner's mail password; no_claim=what Python lets a plugin import."""
    SEEN.clear()
    monkeypatch.setattr(P, "entry_points", lambda: [("recorder", "test_plugin_api:recorder_plugin")])
    app, mcp, *_ = web()
    await mcp.call_tool("icloud_check_health", {})
    await mcp.call_tool("icloud_list_accounts", {})
    async with client(app) as c:
        await signed_in(c)
        await c.get("/dashboard/api/accounts")
    for kind in ("enabled", "secrets", "health", "accounts", "agent_accounts", "backend", "sending_on", "reading_on", "executor",
                 "policy", "sender", "action_executor", "action_policy"):
        ctx = SEEN.get(kind)
        assert isinstance(ctx, P.PluginContext), kind
        assert ctx.plugin_id == "recorder"
        with pytest.raises(AttributeError):
            ctx.app_password                       # noqa: B018  -- reading the core's settings through it
        with pytest.raises(AttributeError):
            ctx.imap_username                      # noqa: B018
    assert len({id(SEEN[k]) for k in ("enabled", "health", "backend")}) == 1      # one context per plugin and server


def test_the_context_reads_only_the_plugins_own_settings_and_secrets(monkeypatch):
    """protects=one plugin cannot read another's settings or secrets through its context; fails_when=value gives a key the plugin
    did not declare, or secret gives a name outside its secret_keys; no_claim=none."""
    _env(monkeypatch, BEEPER_GATEWAY_URL="https://gateway.example.net/mcp", BEEPER_EXEC_KEY=EXEC, RECORDER_SHELVES="s-1")
    monkeypatch.setattr(P, "entry_points", lambda: [("recorder", "test_plugin_api:recorder_plugin")])
    s = Settings.from_env()
    rec = next(p for p in P.load_plugins() if p.id == "recorder")
    beeper = next(p for p in P.load_plugins() if p.id == "beeper")
    ctx = P.context(rec, s)
    assert ctx.value("RECORDER_SHELVES") == "s-1"
    assert ctx.value("BEEPER_GATEWAY_URL", "none") == "none"             # Beeper's, not the recorder's
    with pytest.raises(KeyError):
        ctx.secret("BEEPER_EXEC_KEY")
    assert P.context(beeper, s).secret("BEEPER_EXEC_KEY") == EXEC
    with pytest.raises(AttributeError):
        ctx.plugin_id = "beeper"


# -- criterion 5: secrets never show ----------------------------------------------------------------------------------------------
async def test_beeper_keys_appear_in_no_plugin_value_no_settings_answer_and_no_error(web, monkeypatch):
    """protects=a gateway key never leaves the server through the dashboard, the values or an error; fails_when=one of the made-up
    keys is found in plugin_values, repr(Settings), a dashboard answer or a masked error; no_claim=the log file."""
    app, mcp, *_ = web(BEEPER_GATEWAY_URL="https://gateway.example.net/mcp", BEEPER_EXEC_KEY=EXEC, BEEPER_READ_KEY=READ,
                       BEEPER_SEND_ALLOWLIST="4817")
    s = mcp._icloud_dashboard.s
    assert not [k for k, _ in s.plugin_values if k in ("BEEPER_EXEC_KEY", "BEEPER_READ_KEY")]
    assert EXEC not in repr(s) and READ not in repr(s) and EXEC not in str(s.plugin_values)
    beeper = next(p for p in P.load_plugins() if p.id == "beeper")
    assert set(P.secrets_of(beeper, s)) == {EXEC, READ}                  # masked in every error
    async with client(app) as c:
        await signed_in(c)
        pages = [(await c.get(u)).text for u in ("/dashboard/api/settings", "/dashboard/api/accounts", "/dashboard/api/state")]
    for page in pages:
        assert EXEC not in page and READ not in page
    from icloud_mcp import server as SV
    assert {EXEC, READ} <= set(SV._error_secrets)
    assert EXEC not in SV.scrub_error(f"gateway refused {EXEC}", SV._error_secrets)


# -- criterion 6: lists by request for plugins ----------------------------------------------------------------------------------
def test_an_entry_for_a_plugins_list_applies_at_once_without_a_beeper_name_in_the_core(monkeypatch):
    """protects=gate.allow works for a plugin's list through its values (D7-5); fails_when=the entry does not apply, a list that
    is not requestable takes one, or a second add duplicates it; no_claim=the card (test_gate_allow)."""
    _env(monkeypatch, BEEPER_GATEWAY_URL="https://gateway.example.net/mcp", BEEPER_SEND_ALLOWLIST="4817")
    s = Settings.from_env()
    C.add_live(s, "BEEPER_SEND_ALLOWLIST", "4818")
    C.add_live(s, "BEEPER_SEND_ALLOWLIST", "4818")
    assert s.plugin_value("BEEPER_SEND_ALLOWLIST") == "4817,4818"
    assert C.live_matches(s, "BEEPER_SEND_ALLOWLIST", "4818,4817")
    beeper = next(p for p in P.load_plugins() if p.id == "beeper")
    assert "4818" in beeper.capabilities.messages.allowed(P.context(beeper, s))
    with pytest.raises(KeyError):
        C.add_live(s, "BEEPER_SEND_NETWORKS", "Signal")                    # not requestable


def parlor_lists_plugin():
    from icloud_mcp.plugin_api import Group, Plugin, Spec, changed
    return Plugin(id="parlor", name="Parlor", status="Set up by whoever runs this server", limits="Made up.", description="Made up.",
                  settings_keys=frozenset({"PARLOR_ROOMS", "PARLOR_GUESTS"}), enabled=lambda c: True, api=1,
                  settings_group=lambda: Group("parlor", "Parlor", "Rooms.", [
                      Spec("PARLOR_ROOMS", "Rooms", "list", loosens=changed, requestable=True),
                      Spec("PARLOR_GUESTS", "Guests", "list", loosens=changed, requestable=True, empty_means_anyone=True)]))


def test_a_made_up_plugins_requestable_list_works_the_same_way(monkeypatch):
    """protects=a second plugin's list needs no core change; fails_when=its entry does not apply or an empty list that means anyone
    is narrowed to one; no_claim=none."""
    monkeypatch.setattr(P, "entry_points", lambda: [("parlor", "test_plugin_api:parlor_lists_plugin")])
    _env(monkeypatch, PARLOR_ROOMS="")
    s = Settings.from_env()
    C.add_live(s, "PARLOR_ROOMS", "room-2")
    C.add_live(s, "PARLOR_GUESTS", "sam@example.org")
    assert s.plugin_value("PARLOR_ROOMS") == "room-2" and s.plugin_value("PARLOR_GUESTS", "") == ""


# -- criterion 8: today's .env on CT 219 loads unchanged --------------------------------------------------------------------------
ENV_TODAY = """ICLOUD_USERNAME=jana@example.org
ICLOUD_APP_PASSWORD=aaaa-bbbb-cccc-dddd
BEEPER_GATEWAY_URL=https://gateway.example.net/mcp
BEEPER_EXEC_KEY=made-up-exec-key-7f3a91
BEEPER_READ_KEY=made-up-read-key-c41d02
BEEPER_SEND_ALLOWLIST=329,4817
BEEPER_CHAT_TOOL=beeper_jana-get_chat
BEEPER_SEND_TOOL=beeper_jana_send-send_message
BEEPER_TIMEOUT_SECONDS=200
BEEPER_ACCOUNTS_TOOL=beeper_jana-get_accounts
BEEPER_READ_SERVER=beeper_jana
"""


def test_an_env_file_in_todays_format_loads_with_every_beeper_name(monkeypatch):
    """protects=the deploy needs no change to .env; fails_when=a BEEPER_* name is no longer read or read differently;
    no_claim=the live gateway."""
    from icloud_mcp import beeper as B
    for line in ENV_TODAY.splitlines():
        k, _, v = line.partition("=")
        monkeypatch.setenv(k, v)
    s = Settings.from_env()
    beeper = next(p for p in P.load_plugins() if p.id == "beeper")
    ctx = P.context(beeper, s)
    assert beeper.enabled(ctx) and B.configured(ctx)
    assert (B.gateway_url(ctx), B.exec_key(ctx), B.read_key(ctx)) == ("https://gateway.example.net/mcp", EXEC, READ)
    assert (B.chat_tool(ctx), B.send_tool(ctx), B.accounts_tool(ctx)) == ("beeper_jana-get_chat", "beeper_jana_send-send_message",
                                                                          "beeper_jana-get_accounts")
    assert B.timeout_seconds(ctx) == 120.0                                # bounded as before, 5 to 120
    assert B.send_allowlist(ctx) == ("329", "4817")
    assert B.read_tool(ctx, "search_chats") == "beeper_jana-search_chats"
