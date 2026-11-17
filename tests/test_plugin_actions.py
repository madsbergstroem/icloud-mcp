"""Actions of any kind (docs/SENDGATE.md, package 1): a made-up plugin, "Parcel", comes in through the entry point with one
ActionSpec and no chats. Agents find it with approval_list_actions and ask with approval_request; the owner sees a card, may edit
the note, approves it once; approval_status and approval_withdraw answer for it. Every name, address and slot is made up.

Each test says what it protects, the wrong result it rejects and what a green run does not prove."""
import json

import pytest

from icloud_mcp import actions as A
from icloud_mcp import plugins as P
from icloud_mcp.config import Settings
from icloud_mcp.instructions import build_instructions
from icloud_mcp.server import create_server
from test_dashboard import api, client, signed_in, web  # noqa: F401  (web is a fixture)
from test_external_approvals import audit_text, url
from test_plugins import chatty_plugin

PICKUP = A.ActionType(
    id="parcel.pickup.book", title="Parcel pickup", badge="Parcel", approve_label="Book",
    params_schema={"type": "object", "additionalProperties": False, "required": ["depot", "slot", "note"],
                   "properties": {"depot": {"type": "string", "maxLength": 40}, "slot": {"type": "string", "pattern": "^[0-9]{2}:[0-9]{2}$"},
                                  "note": {"type": "string", "maxLength": 300},
                                  "depot_name": {"type": "string", "readOnly": True}}},
    rows=(("Depot", "depot_name"), ("Slot", "slot")), text_field="note", recipients=lambda p: [p["depot"]])

BOOKED: list[dict] = []
SEEN: dict = {"names": {"d-17": "North depot", "d-21": "Harbour depot"}, "fail": False, "asked": [], "recheck_fails": False}


class PickupExecutor:
    def execute(self, request):
        BOOKED.append(dict(request.params))
        return A.Result("sent", "Booked. Parcel confirmed the slot.")


def _prepare(ctx, params):
    """Asks the made-up service what the depot is called, so the card shows the service's words."""
    SEEN["asked"].append(params.get("depot"))
    if SEEN["fail"]:
        raise RuntimeError("upstream said: <html>secret-body-marker</html>")
    name = SEEN["names"].get(params.get("depot"))
    if name is None:
        raise A.ExternalError("Parcel does not know that depot.", "schema", "params/depot")
    return {**params, "depot_name": name}


def _recheck(ctx, params):
    if SEEN["recheck_fails"]:
        raise RuntimeError("recheck-secret-marker")
    return None if SEEN["names"].get(params["depot"]) == params["depot_name"] else "target_changed"


def parcel_plugin():
    return P.Plugin(
        id="parcel", name="Parcel", status="Set up by whoever runs this server", limits="Pickups only.",
        description="A made-up parcel service.", enabled=lambda s: True, settings_keys=frozenset({"PARCEL_DEPOTS"}),
        actions=(P.ActionSpec(action_type=PICKUP, executor=lambda ctx: PickupExecutor(),
                              allowed=lambda ctx: [x for x in ctx.value("PARCEL_DEPOTS").split(",") if x.strip()],
                              policy=lambda ctx: (lambda p: "Not permitted: no pickups at night." if p["slot"] < "07:00" else None),
                              prepare=_prepare, recheck=_recheck),))


@pytest.fixture
def parcel(monkeypatch):
    BOOKED.clear()
    SEEN.update(names={"d-17": "North depot", "d-21": "Harbour depot"}, fail=False, asked=[], recheck_fails=False)
    monkeypatch.setattr(P, "entry_points", lambda: [("parcel", "test_plugin_actions:parcel_plugin")])
    P._loaded.cache_clear()                       # loaded plugins are cached per process; each test builds its own
    yield
    P._loaded.cache_clear()


async def call(mcp, name, **args):
    return json.loads((await mcp.call_tool(name, args)).content[0].text)


async def refused(mcp, **args):
    """The refusal approval_request raised, as the JSON object its error text holds."""
    with pytest.raises(Exception) as got:
        await mcp.call_tool("approval_request", args)
    text = str(got.value)                         # the SDK puts "Error executing tool approval_request: " before the tool's own text
    return json.loads(text[text.index("{"):])


ASK = {"action": "parcel.pickup.book", "params": {"depot": "d-17", "slot": "10:30", "note": "Two boxes, ring at the side door."}}


async def test_a_plugin_action_is_listed_queued_shown_edited_approved_once_and_answered_for(web, parcel):
    """protects=a plugin with one ActionSpec and no chats gets the whole flow with no core change;
    fails_when=it is not listed, its card lacks the service's depot name, the edit is not what is carried out, it runs twice,
    or approval_status does not say sent; no_claim=a real service: the executor here only records the call."""
    app, mcp, *_ = web(PARCEL_DEPOTS="d-17,d-21")
    listed = await call(mcp, "approval_list_actions")
    assert [(a["action"], a["allowed"], a["text_field"]) for a in listed["actions"]] == [("parcel.pickup.book", "list", "note")]
    got = await call(mcp, "approval_request", **ASK)
    assert got["status"] == "queued_for_owner_approval" and got["sent"] is False and BOOKED == []
    assert got["approve_at"].endswith(f"#/item/external:{got['outbox_id']}")
    async with client(app) as c:
        token = await signed_in(c)
        it = (await c.get(url(got["outbox_id"]))).json()
        assert it["rows"] == [["Depot", "North depot"], ["Slot", "10:30"]] and it["text"] == ASK["params"]["note"]
        assert it["badge"] == "Parcel" and it["approve_label"] == "Book" and "approvable" not in it
        sha = (await c.put(url(got["outbox_id"]) + "/text", json={"sha": it["sha"], "text": "Two boxes, front door."},
                           headers=api(token))).json()["sha"]
        r = await c.post(url(got["outbox_id"]) + "/approve", json={"sha": sha}, headers=api(token))
        assert r.status_code == 200, r.text
        assert (await c.post(url(got["outbox_id"]) + "/approve", json={"sha": sha}, headers=api(token))).status_code == 409
    assert BOOKED == [{"depot": "d-17", "slot": "10:30", "note": "Two boxes, front door.", "depot_name": "North depot"}]
    status = await call(mcp, "approval_status", id=got["outbox_id"])
    assert (status["status"], status["edited"], status["action_type"]) == ("sent", True, "parcel.pickup.book")


async def test_every_log_line_of_a_request_names_the_hash_of_what_it_was_then(web, parcel):
    """protects=the activity log can say which version was approved (review obligation E-3);
    fails_when=the queued, edited or sent line lacks the hash, or names another version than the one at that moment;
    no_claim=that the log cannot be rewritten as a whole: the chain is unsigned."""
    app, mcp, *_ = web(PARCEL_DEPOTS="d-17")
    got = await call(mcp, "approval_request", **ASK)
    src = mcp._icloud_dashboard.sources["external"]
    first = src.pending()[0].sha256
    async with client(app) as c:
        token = await signed_in(c)
        second = (await c.put(url(got["outbox_id"]) + "/text", json={"sha": first, "text": "Front door."}, headers=api(token))).json()["sha"]
        assert (await c.post(url(got["outbox_id"]) + "/approve", json={"sha": second}, headers=api(token))).status_code == 200
    lines = [json.loads(x) for x in audit_text(mcp).splitlines()]
    mine = [(e["action"], e["details"].get("sha256")) for e in lines if (e.get("details") or {}).get("outbox_id") == got["outbox_id"]]
    assert mine == [("tool.queued", first), ("approval.edited", second), ("approval.sent", second)] and first != second
    assert "Front door" not in audit_text(mcp) and "Two boxes" not in audit_text(mcp)


async def test_bad_params_answer_with_the_path_and_queue_nothing(web, parcel):
    """protects=an agent learns which parameter is wrong; fails_when=a bad slot is queued or the answer has no path or fix;
    no_claim=every schema keyword: jsonschema implements them."""
    _app, mcp, *_ = web(PARCEL_DEPOTS="d-17")
    got = await refused(mcp, action="parcel.pickup.book", params={**ASK["params"], "slot": "half ten"})
    assert (got["status"], got["sent"], got["reason"], got["path"]) == ("refused", False, "schema", "slot") and got["fix"]
    assert got["docs"].endswith("#refusal-codes-schema")
    assert mcp._icloud_dashboard.sources["external"].pending() == []


async def test_a_depot_not_on_the_owners_list_is_refused_and_the_list_is_never_shown(web, parcel):
    """protects=the owner's list stays the owner's; fails_when=a depot off the list is queued, or any list entry appears in an
    answer; no_claim=a list read from a file the agent can change: the list is a setting only the owner edits."""
    _app, mcp, *_ = web(PARCEL_DEPOTS="d-17")
    got = await refused(mcp, action="parcel.pickup.book", params={**ASK["params"], "depot": "d-21"})
    assert got["reason"] == "not_allowed" and "d-17" not in json.dumps(got)
    assert "d-17" not in json.dumps(await call(mcp, "approval_list_actions"))
    assert mcp._icloud_dashboard.sources["external"].pending() == []
    assert (await refused(mcp, action="parcel.pickup.book", params={**ASK["params"], "slot": "05:00"}))["reason"] == "policy"


async def test_a_field_the_server_fills_in_is_refused_when_the_agent_sets_it(web, parcel):
    """protects=the card shows the service's words, not the agent's; fails_when=an agent-set depot_name is queued;
    no_claim=a prepare that lies: the plugin is trusted code."""
    _app, mcp, *_ = web(PARCEL_DEPOTS="d-17")
    got = await refused(mcp, action="parcel.pickup.book", params={**ASK["params"], "depot_name": "Your bank"})
    assert (got["reason"], got["path"]) == ("read_only", "params/depot_name")
    assert mcp._icloud_dashboard.sources["external"].pending() == []


async def test_the_same_request_id_returns_the_first_request_and_other_params_are_refused(web, parcel):
    """protects=an agent that asks twice queues one card; fails_when=a repeat queues a second entry, or the same key with other
    params is accepted; no_claim=keys after the request ended: package 3 keeps them."""
    _app, mcp, *_ = web(PARCEL_DEPOTS="d-17")
    first = await call(mcp, "approval_request", **ASK, request_id="pickup-1")
    again = await call(mcp, "approval_request", **ASK, request_id="pickup-1")
    assert again["outbox_id"] == first["outbox_id"] and again["already_queued"] is True and "already_queued" not in first
    other = await refused(mcp, action=ASK["action"], params={**ASK["params"], "slot": "11:00"}, request_id="pickup-1")
    assert (other["reason"], other["path"]) == ("key_conflict", "request_id")
    assert len(mcp._icloud_dashboard.sources["external"].pending()) == 1


async def test_an_unknown_action_and_a_channel_owned_type_are_refused_by_name(web, parcel, monkeypatch):
    """protects=one way to ask for each thing; fails_when=a chat message is queued through approval_request, or the list
    offers it; no_claim=that the channel tool itself works: test_plugins covers it."""
    monkeypatch.setattr(P, "entry_points", lambda: [("parcel", "test_plugin_actions:parcel_plugin"), ("chatty", "test_plugins:chatty_plugin")])
    _app, mcp, *_ = web(PARCEL_DEPOTS="d-17", CHATTY_SEND_ALLOWLIST="room-1")
    assert (await refused(mcp, action="parcel.nothing", params={}))["reason"] == "unknown_action"
    got = await refused(mcp, action="chatty.message.send", params={"chat_id": "room-1", "text": "Hi", "room": "x"})
    assert got["reason"] == "channel_tool" and "messages_send_request" in got["message"]
    assert [a["action"] for a in (await call(mcp, "approval_list_actions"))["actions"]] == ["parcel.pickup.book"]


async def test_a_service_that_cannot_be_asked_refuses_as_retryable_without_its_error_text(web, parcel):
    """protects=a failing service never queues a card with the agent's words and never leaks its body;
    fails_when=it queues, or the service's text reaches the agent; no_claim=a timeout: the made-up service raises at once."""
    _app, mcp, *_ = web(PARCEL_DEPOTS="d-17,d-99")
    SEEN["fail"] = True
    got = await refused(mcp, **ASK)
    assert (got["reason"], got["retryable"], got["side_effects"]) == ("describe_failed", True, "none")
    assert "secret-body-marker" not in json.dumps(got)
    SEEN["fail"] = False
    assert (await refused(mcp, action=ASK["action"], params={**ASK["params"], "depot": "d-99"}))["path"] == "params/depot"


async def test_a_target_that_changed_after_the_owner_looked_is_not_carried_out(web, parcel):
    """protects=the owner approves what they saw (review obligation E-2); fails_when=the executor runs although the depot was
    renamed, or the request waits again; no_claim=a change between the recheck and the send itself."""
    app, mcp, *_ = web(PARCEL_DEPOTS="d-17")
    got = await call(mcp, "approval_request", **ASK)
    q = mcp._icloud_dashboard.sources["external"].pending()[0]
    SEEN["names"]["d-17"] = "North depot (closed)"
    async with client(app) as c:
        token = await signed_in(c)
        r = await c.post(url(q.id) + "/approve", json={"sha": q.sha256}, headers=api(token))
    assert r.status_code == 409 and r.json()["state"] == "discarded" and "target changed" in r.json()["error"]
    assert BOOKED == [] and mcp._icloud_dashboard.sources["external"].pending() == []
    status = await call(mcp, "approval_status", id=got["outbox_id"])
    assert (status["status"], status["reason"]) == ("discarded", "target_changed") and "Ask again only if" in status["note"]


async def test_an_agent_takes_its_request_back_and_the_log_names_the_action(web, parcel):
    """protects=a withdrawn action is logged under its type's title, never a parameter; fails_when=the line is untitled or holds
    the note; no_claim=who the client really is: its name is what it says about itself."""
    _app, mcp, *_ = web(PARCEL_DEPOTS="d-17")
    got = await call(mcp, "approval_request", **ASK)
    assert (await call(mcp, "approval_withdraw", id=got["outbox_id"]))["withdrawn"] is True
    (line,) = [json.loads(x) for x in audit_text(mcp).splitlines() if '"approval.withdrawn"' in x]
    assert line["summary"] == "Withdrew action “Parcel pickup”" and "side door" not in json.dumps(line)


async def test_with_only_channel_types_the_list_is_empty_with_a_hint(web, monkeypatch):
    """protects=an agent is told why there is nothing to ask for; fails_when=the list errors or offers the chat type;
    no_claim=none beyond protects."""
    monkeypatch.setattr(P, "entry_points", lambda: [("chatty", "test_plugins:chatty_plugin")])
    _app, mcp, *_ = web(CHATTY_SEND_ALLOWLIST="room-1")
    got = await call(mcp, "approval_list_actions")
    assert got["actions"] == [] and "messages_send_request" in got["hint"]


def _server(tmp_path, monkeypatch, **env):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path),
                     MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16, PARCEL_DEPOTS="d-17", **env).items():
        monkeypatch.setenv(k, v)
    s = Settings.from_env()
    mcp, _ = create_server(s)
    return s, mcp


async def test_without_the_dashboard_neither_tool_is_served_nor_named(tmp_path, monkeypatch, parcel):
    """protects=no tool queues what nobody can approve; fails_when=either tool is listed or named in the instructions without the
    dashboard; no_claim=the instructions of every setting: test_instructions covers those."""
    s, mcp = _server(tmp_path, monkeypatch, DASHBOARD="false")
    names = {t.name for t in await mcp.list_tools()}
    assert not names & {"approval_list_actions", "approval_request"}
    text = build_instructions(s, names)
    assert "approval_request" not in text and "approval_list_actions" not in text
    health = await call(mcp, "icloud_check_health")
    assert health["approval_tools"] == "off: the dashboard is not enabled"


async def test_with_the_dashboard_both_tools_are_served_and_named(tmp_path, monkeypatch, parcel):
    """protects=an agent can find the tools; fails_when=either is missing from the list or the instructions;
    no_claim=the wording of the instructions."""
    s, mcp = _server(tmp_path, monkeypatch)
    names = {t.name for t in await mcp.list_tools()}
    assert {"approval_list_actions", "approval_request"} <= names
    assert "approval_request" in build_instructions(s, names)
    assert (await call(mcp, "icloud_check_health"))["approval_tools"] == "on"


def test_the_contract_types_are_frozen_and_read_settings_through_the_context():
    """protects=a plugin reads only its own settings through the context; fails_when=PluginContext.value does not read the
    plugin's setting; no_claim=versioning of the contract: package 7."""
    class FakeSettings:
        def plugin_value(self, key, default=""):
            return {"PARCEL_DEPOTS": "d-17"}.get(key, default)
    ctx = P.PluginContext(FakeSettings(), "parcel")
    assert ctx.value("PARCEL_DEPOTS") == "d-17" and ctx.value("NOPE", "x") == "x"
    with pytest.raises(Exception):
        ctx.plugin_id = "other"                                                              # frozen
    assert chatty_plugin().actions == ()


def test_every_refusal_code_has_its_own_section_in_the_docs():
    """protects=each refusal's docs link leads somewhere (review obligations X13, X14); fails_when=a code is added to
    actions.REFUSALS without its section in docs/SENDGATE.md; no_claim=that the section's wording is right."""
    from pathlib import Path
    doc = (Path(__file__).resolve().parents[1] / "docs" / "SENDGATE.md").read_text()
    missing = [c for c in A.REFUSALS if f"### Refusal codes: {c}\n" not in doc]
    assert not missing, missing


REFUSAL_CASES = {
    "unknown_action": dict(action="parcel.nothing", params={}),
    "channel_tool": None,                                                # needs the chat plugin: test_an_unknown_action_and_a_channel_owned_type...
    "schema": dict(action=ASK["action"], params={**ASK["params"], "slot": "half ten"}),
    "read_only": dict(action=ASK["action"], params={**ASK["params"], "depot_name": "x"}),
    "not_allowed": dict(action=ASK["action"], params={**ASK["params"], "depot": "d-21"}),
    "policy": dict(action=ASK["action"], params={**ASK["params"], "slot": "05:00"}),
    "describe_failed": "fail",
    "key_conflict": "key",
    "queue_full": "full",
    "store_unavailable": "store",
    "not_replaceable": dict(action=ASK["action"], params=ASK["params"], replaces="no-such-request"),
}


@pytest.mark.parametrize("code", sorted(A.REFUSALS))
async def test_every_refusal_code_answers_with_its_fix_and_queues_nothing(web, parcel, code):
    """protects=one refusal shape for every code, each with its fix line (review obligations A1, X12); fails_when=a code comes
    back without fix or docs, as anything but a tool error, or queues something; no_claim=channel_tool, covered by its own test."""
    case = REFUSAL_CASES[code]
    if case is None:
        pytest.skip("covered by test_an_unknown_action_and_a_channel_owned_type_are_refused_by_name")
    _app, mcp, *_ = web(PARCEL_DEPOTS="d-17", **({"OUTBOX_MAX": "1"} if case == "full" else {}))
    src = mcp._icloud_dashboard.sources["external"]
    if case == "fail":
        SEEN["fail"] = True
        case = ASK
    elif case == "key":
        await call(mcp, "approval_request", **ASK, request_id="k-1")
        case = dict(action=ASK["action"], params={**ASK["params"], "slot": "11:00"}, request_id="k-1")
    elif case == "store":
        from icloud_mcp.gate import store as S
        def busy(*a, **k):
            raise S.StoreUnavailable("The approval store cannot take a request right now. Nothing was queued.")
        src.box.add = busy
        case = ASK
    elif case == "full":
        await call(mcp, "approval_request", **ASK)
        case = dict(action=ASK["action"], params={**ASK["params"], "slot": "11:00"})
    before = len(src.pending())
    got = await refused(mcp, **case)
    assert (got["status"], got["sent"], got["reason"]) == ("refused", False, code)
    assert got["fix"] == A.REFUSALS[code] and got["docs"].endswith(f"#refusal-codes-{code}")
    assert len(src.pending()) == before


@pytest.mark.parametrize("depots,word", [("*", "anyone"), ("", "nobody"), ("d-17,d-21", "list")])
async def test_the_list_says_anyone_nobody_or_list_and_empty_lets_nobody_through(web, parcel, depots, word):
    """protects=`*` means anyone and empty means nobody (review obligation Cl2); fails_when=an empty list lets a request through,
    or the word is wrong; no_claim=mail's SEND_ALLOWLIST, where empty means anyone until package 6."""
    _app, mcp, *_ = web(PARCEL_DEPOTS=depots)
    assert (await call(mcp, "approval_list_actions"))["actions"][0]["allowed"] == word
    if word == "nobody":
        assert (await refused(mcp, **ASK))["reason"] == "not_allowed"
    else:
        assert (await call(mcp, "approval_request", **ASK))["status"] == "queued_for_owner_approval"


async def test_a_target_off_the_list_never_reaches_the_service(web, parcel):
    """protects=the service is never asked about a target the owner did not allow (review obligation E-6); fails_when=prepare
    runs for a depot off the list; no_claim=rules that need what prepare fills in: those run after it."""
    _app, mcp, *_ = web(PARCEL_DEPOTS="d-17")
    await refused(mcp, action=ASK["action"], params={**ASK["params"], "depot": "d-21"})
    await refused(mcp, action=ASK["action"], params={**ASK["params"], "slot": "half ten"})
    assert SEEN["asked"] == []


async def test_the_same_request_id_holds_even_when_the_service_words_change(web, parcel):
    """protects=a repeat with the same key and the same agent params returns the first card although prepare's answer changed;
    fails_when=it is refused as key_conflict or queues a second card; no_claim=keys after the request ended (package 3)."""
    _app, mcp, *_ = web(PARCEL_DEPOTS="d-17")
    first = await call(mcp, "approval_request", **ASK, request_id="k-2")
    SEEN["names"]["d-17"] = "North depot, gate B"
    again = await call(mcp, "approval_request", **ASK, request_id="k-2")
    assert again["outbox_id"] == first["outbox_id"] and again["already_queued"] is True


async def test_a_recheck_that_raises_puts_the_request_back_and_leaks_nothing(web, parcel):
    """protects=a recheck that cannot ask the service carries nothing out and keeps the request; fails_when=the executor runs,
    the request is lost, or the service's text reaches the owner or the log; no_claim=a recheck that hangs (package 3)."""
    app, mcp, *_ = web(PARCEL_DEPOTS="d-17")
    await call(mcp, "approval_request", **ASK)
    q = mcp._icloud_dashboard.sources["external"].pending()[0]
    SEEN["recheck_fails"] = True
    async with client(app) as c:
        token = await signed_in(c)
        r = await c.post(url(q.id) + "/approve", json={"sha": q.sha256}, headers=api(token))
    assert r.status_code == 502 and "recheck-secret-marker" not in r.text and BOOKED == []
    assert [x.id for x in mcp._icloud_dashboard.sources["external"].pending()] == [q.id]
    assert "recheck-secret-marker" not in audit_text(mcp)


async def test_a_recheck_answer_that_is_not_a_known_code_is_logged_as_target_changed(web, parcel, monkeypatch):
    """protects=the log holds a code from a fixed set, never a plugin's own text; fails_when=the recheck's words reach the log;
    no_claim=what a plugin does with its own logs."""
    import test_plugin_actions as me
    monkeypatch.setattr(me, "_recheck", lambda ctx, p: "depot renamed to secret-recheck-words")
    P._loaded.cache_clear()
    app, mcp, *_ = web(PARCEL_DEPOTS="d-17")
    await call(mcp, "approval_request", **ASK)
    q = mcp._icloud_dashboard.sources["external"].pending()[0]
    async with client(app) as c:
        token = await signed_in(c)
        r = await c.post(url(q.id) + "/approve", json={"sha": q.sha256}, headers=api(token))
    assert r.status_code == 409 and "secret-recheck-words" not in audit_text(mcp)
    assert '"reason":"target_changed"' in audit_text(mcp)


async def test_a_list_or_rule_that_raises_refuses_without_its_text(web, parcel, monkeypatch):
    """protects=an error in the owner's list or rule never reaches the agent; fails_when=the exception text comes back;
    no_claim=none beyond protects."""
    import test_plugin_actions as me
    plugin = me.parcel_plugin()
    spec = plugin.actions[0]
    broken = P.ActionSpec(action_type=spec.action_type, executor=spec.executor, prepare=spec.prepare, recheck=spec.recheck,
                          allowed=lambda ctx: (_ for _ in ()).throw(RuntimeError("list-secret-marker")))
    monkeypatch.setattr(me, "parcel_plugin", lambda: P.Plugin(**{**plugin.__dict__, "actions": (broken,)}))
    P._loaded.cache_clear()
    _app, mcp, *_ = web()
    got = await refused(mcp, **ASK)
    assert got["reason"] == "policy" and "list-secret-marker" not in json.dumps(got)


async def test_a_broken_action_is_skipped_and_the_server_starts(web, parcel, monkeypatch, caplog):
    """protects=one broken ActionSpec does not take the server down; fails_when=the start fails or the good action is gone;
    no_claim=what the broken plugin would have done."""
    import test_plugin_actions as me
    plugin = me.parcel_plugin()
    bad = P.ActionSpec(action_type=A.ActionType(id="parcel.bad.thing", title="Bad", badge="Bad", approve_label="Go",
                                                params_schema={"type": "object"}),
                       executor=lambda ctx: (_ for _ in ()).throw(RuntimeError("executor-secret-marker")))
    monkeypatch.setattr(me, "parcel_plugin", lambda: P.Plugin(**{**plugin.__dict__, "actions": (bad, plugin.actions[0])}))
    P._loaded.cache_clear()
    _app, mcp, *_ = web(PARCEL_DEPOTS="d-17")
    assert [a["action"] for a in (await call(mcp, "approval_list_actions"))["actions"]] == ["parcel.pickup.book"]
    assert "parcel.bad.thing" in caplog.text and "executor-secret-marker" not in caplog.text


def test_readonly_below_the_top_level_and_readonly_without_prepare_are_refused_at_start():
    """protects=a readOnly field is always one the gate enforces (review obligations Cl3, F5); fails_when=a nested readOnly or a
    readOnly field nobody fills in is accepted; no_claim=$ref schemas: refused the same way, readOnly inside them is nested."""
    nested = {"type": "object", "properties": {"box": {"type": "object", "properties": {"label": {"type": "string", "readOnly": True}}}}}
    with pytest.raises(ValueError, match="top-level"):
        A.ActionType(id="x.y", title="T", badge="B", approve_label="Go", params_schema=nested)
    flat = A.ActionType(id="x.z", title="T", badge="B", approve_label="Go",
                        params_schema={"type": "object", "properties": {"label": {"type": "string", "readOnly": True}}})
    with pytest.raises(ValueError, match="no prepare"):
        A.ActionRegistry().register(flat, PickupExecutor())


async def test_the_two_tools_fit_the_naming_rule_and_stay_small(tmp_path, monkeypatch, parcel):
    """protects=the new tools follow area_verb_noun's exception list and add little to every tools/list (review obligation AC6);
    fails_when=either is missing from NAMED_AFTER_WHAT_THEY_ANSWER or together adds more than 6,000 bytes; no_claim=the budget of a
    server with many plugins: each plugin adds no tool, only actions."""
    from mcp.types import ListToolsResult
    from test_tool_surface import NAMED_AFTER_WHAT_THEY_ANSWER
    _s, mcp = _server(tmp_path, monkeypatch)
    tools = [t for t in await mcp.list_tools() if t.name in ("approval_list_actions", "approval_request")]
    assert {t.name for t in tools} <= NAMED_AFTER_WHAT_THEY_ANSWER and len(tools) == 2
    assert len(ListToolsResult(tools=tools).model_dump_json(by_alias=True, exclude_none=True).encode()) <= 6000


def test_the_card_script_holds_the_reviewed_order_and_words():
    """protects=the action card's order and outcome words from the approved mock (review obligations H1, S-b/c/d, S-g);
    fails_when=one of them is dropped from the script; no_claim=how it renders: the owner clicked through it in the preview."""
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "src" / "icloud_mcp" / "static" / "app" / "today.js").read_text()
    for needle in ("it.rows.slice(0, 1)", "it.rows.slice(1)", "Not sure it reached ${esc(it.badge)}.", "Nothing will be sent again; look there first.",
                   "<b>Did not go out:</b>", 'it.approvable === false', 'e.body.state === "discarded"'):
        assert needle in js, needle


async def test_a_request_id_whose_request_was_sent_queues_nothing_and_answers_sent(web, parcel):
    """protects=an agent that asks again with the same request_id after the owner approved never gets a second booking (package 3,
    criterion 3); fails_when=a second card is queued; no_claim=after AUDIT_KEEP_DAYS, when the row and its key are gone."""
    app, mcp, *_ = web(PARCEL_DEPOTS="d-17")
    first = await call(mcp, "approval_request", **ASK, request_id="pickup-sent")
    q = mcp._icloud_dashboard.sources["external"].pending()[0]
    async with client(app) as c:
        token = await signed_in(c)
        assert (await c.post(url(q.id) + "/approve", json={"sha": q.sha256}, headers=api(token))).status_code == 200
    again = await call(mcp, "approval_request", **ASK, request_id="pickup-sent")
    assert (again["status"], again["sent"], again["outbox_id"], again["already_decided"]) == ("sent", True, first["outbox_id"], True)
    assert mcp._icloud_dashboard.sources["external"].pending() == [] and len(BOOKED) == 1
