"""Actions defined in a file (gate_connectors, docs/SENDGATE.md package 2): a made-up marketplace behind a fake MCP gateway
(dev/fake_gateway.py). Every name, key and listing is made up. Each test says what it protects, the wrong result it rejects and
what a green run does not prove."""
import copy
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dev"))

from fake_gateway import FakeGateway  # noqa: E402

from icloud_mcp import plugins as P  # noqa: E402
from icloud_mcp.gate_connectors import __main__ as cli  # noqa: E402
from icloud_mcp.gate_connectors import actions as G  # noqa: E402
from icloud_mcp.gate_connectors.gateway import Gateway  # noqa: E402
from test_dashboard import api, client, signed_in, web  # noqa: E402,F401  (web is a fixture)
from test_external_approvals import audit_text  # noqa: E402

EXAMPLE = json.loads((ROOT / "docs" / "examples" / "actions.example.json").read_text())
EXEC, AGENT, URL = "exec-key-made-up-7f3a", "agent-key-made-up-91c2", "http://gateway.test/mcp"
TITLES = {"c-1": "Bike trailer, two seats", "c-2": "Garden chairs"}


class Market:
    def __init__(self, grants):
        self.gw = FakeGateway(grants)
        self.answer, self.titles, self.describe_error = "applied", dict(TITLES), False
        self.timeouts: list[float] = []

        @self.gw.tool("market-call_operation")
        def read(site: str, op: str, args: dict) -> dict:
            if self.describe_error:
                raise ValueError("upstream exploded: secret-describe-body")
            return {"data": {"title": self.titles[args["id"]]}}

        @self.gw.tool("market-call_write_operation")
        def write(site: str, op: str, args: dict, idempotencyKey: str = "") -> dict:
            return {"write": self.answer}

    def sends(self):
        return [c for c in self.gw.calls if c[1] == "market-call_write_operation"]


def write_file(tmp_path, doc):
    path = tmp_path / "actions.json"
    path.write_text(json.dumps(doc))
    return path


@pytest.fixture
def market(tmp_path, monkeypatch):
    m = Market({EXEC: {"market-call_operation", "market-call_write_operation"}, AGENT: {"market-call_operation"}})
    path = write_file(tmp_path, EXAMPLE)

    def via_fake(url, key, timeout, make_http=None):
        m.timeouts.append(timeout)
        return Gateway(url, key, timeout, make_http=m.gw.make_http)
    monkeypatch.setattr(G, "Gateway", via_fake)
    for k, v in {"GATE_ACTIONS_FILE": str(path), "GATE_GATEWAY_URL": URL, "GATE_EXEC_KEY": EXEC}.items():
        monkeypatch.setenv(k, v)
    P._loaded.cache_clear()
    yield m
    P._loaded.cache_clear()


async def call(mcp, name, **args):
    return json.loads((await mcp.call_tool(name, args)).content[0].text)


async def refused(mcp, **args):
    with pytest.raises(Exception) as got:
        await mcp.call_tool("approval_request", args)
    text = str(got.value)
    return json.loads(text[text.index("{"):])


ASK = {"action": "market.message.send", "params": {"conversation_id": "c-1", "text": "Is the trailer still free on Saturday?"}}


def url(item_id):
    return f"/dashboard/api/items/external:{item_id}"


async def approve(app, mcp):
    q = mcp._icloud_dashboard.sources["external"].pending()[0]
    async with client(app) as c:
        token = await signed_in(c)
        return q, await c.post(url(q.id) + "/approve", json={"sha": q.sha256}, headers=api(token))


async def test_the_card_shows_the_service_title_and_one_approval_sends_exactly_the_files_arguments(web, market):
    """protects=criterion 1: the card shows what the service says, and the call is exactly what the file describes;
    fails_when=the agent's words reach the card, the call differs, or it is made twice; no_claim=a real marketplace."""
    app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    got = await call(mcp, "approval_request", **ASK)
    async with client(app) as c:
        await signed_in(c)
        it = (await c.get(url(got["outbox_id"]))).json()
    assert it["rows"][0] == ["Listing", "Bike trailer, two seats"] and it["badge"] == "Market"
    q, r = await approve(app, mcp)
    assert r.status_code == 200, r.text
    assert market.sends() == [(EXEC, "market-call_write_operation",
                               {"site": "example-market", "op": "sendMessage", "args": ASK["params"], "idempotencyKey": f"{q.id}.1"})]


@pytest.mark.parametrize("answer,status,waits", [("applied", "sent", False), ("not_sent", None, True), ("may_have_applied", "unclear", False)])
async def test_the_services_answer_decides_sent_failed_or_unclear(web, market, answer, status, waits):
    """protects=criterion 2: only a listed failed value puts the request back; fails_when=an unknown answer is retried or reads
    sent; no_claim=whether the service's words are true: the file's author vouches for the mapping."""
    app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    got = await call(mcp, "approval_request", **ASK)
    market.answer = answer
    q, r = await approve(app, mcp)
    pending = mcp._icloud_dashboard.sources["external"].pending()
    assert (bool(pending)) is waits
    if waits:
        assert r.status_code == 502 and "did not take it" in r.json()["error"]
        market.answer = "applied"
        _, again = await approve(app, mcp)
        assert again.status_code == 200
        assert [c[2]["idempotencyKey"] for c in market.sends()] == [f"{q.id}.1", f"{q.id}.2"]     # a new key per attempt (Cl1)
    else:
        assert (await call(mcp, "approval_status", id=got["outbox_id"]))["status"] == status


async def test_a_gateway_that_cannot_be_reached_is_failed_and_a_lost_answer_is_unclear_and_never_repeated(web, market):
    """protects=criterion 3: retry only what provably never left; fails_when=a lost answer is retried, or a refused connection reads
    unclear; no_claim=a real network partition."""
    app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    await call(mcp, "approval_request", **ASK)
    market.gw.mode = "down"
    _, r = await approve(app, mcp)
    assert r.status_code == 502 and len(mcp._icloud_dashboard.sources["external"].pending()) == 1 and market.sends() == []
    market.gw.mode, market.gw.only = "lose", {"market-call_write_operation"}
    _, r = await approve(app, mcp)
    assert r.status_code == 200 and r.json()["state"] == "unclear"
    assert len(market.sends()) == 1 and mcp._icloud_dashboard.sources["external"].pending() == []


async def test_a_title_that_changed_after_the_owner_looked_sends_nothing(web, market):
    """protects=criterion 4 and S1: the owner approves what they saw; fails_when=the send happens, or the request waits again;
    no_claim=a change between the recheck and the send."""
    app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    got = await call(mcp, "approval_request", **ASK)
    market.titles["c-1"] = "Bike trailer, sold"
    _, r = await approve(app, mcp)
    assert r.status_code == 409 and r.json()["state"] == "discarded" and market.sends() == []
    status = await call(mcp, "approval_status", id=got["outbox_id"])
    assert (status["status"], status["reason"]) == ("discarded", "target_changed")


async def test_an_empty_list_lets_nobody_through_and_star_lets_anyone(web, market, monkeypatch):
    """protects=criterion 5: a new action reaches nobody until the owner lists someone; fails_when=an empty list queues, or a
    request off the list reaches the service at all (E-6); no_claim=editing the list on the dashboard (package 2b)."""
    _app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="")
    assert (await refused(mcp, **ASK))["reason"] == "not_allowed" and market.gw.calls == []
    P._loaded.cache_clear()
    _app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="*")
    assert (await call(mcp, "approval_request", **ASK))["status"] == "queued_for_owner_approval"


@pytest.mark.parametrize("change,pointer", [
    (lambda d: d["actions"][0].update(colour="blue"), "/actions/0"),
    (lambda d: d["actions"][0].update(rows=[["Listing", "nope"]]), "/actions/0/rows/0"),
    (lambda d: d["actions"].append(copy.deepcopy(d["actions"][0])), "/actions/1/action"),
    (lambda d: d["actions"][0].pop("result"), "/actions/0"),
    (lambda d: d["actions"][0]["arguments"].update(x="$secret"), "/actions/0/arguments/x"),
    (lambda d: d["actions"][0]["arguments"].update(x="$params.conversation_title"), "/actions/0/arguments/x"),
    (lambda d: d["actions"][0]["describe"]["arguments"].update(k="$attempt_key"), "/actions/0/describe/arguments/k"),
    (lambda d: d["actions"][0]["result"].update(path="data.items.0"), "/actions/0/result/path"),
    (lambda d: d["actions"][0].update(approve_label="Send it right now"), "/actions/0/approve_label"),
    (lambda d: d["actions"][0].update(rows=[["Listing", "text"]]), "/actions/0/rows/0"),
    (lambda d: d["actions"][0]["describe"].update(recheck=["text"]), "/actions/0/describe/recheck"),
    (lambda d: d["actions"][0]["describe"].update(tool="market-call_write_operation"), "/actions/0/describe/tool"),
    (lambda d: d["actions"][0]["result"]["failed"].append("applied"), "/actions/0/result"),
    (lambda d: d["actions"][0]["params_schema"].update(required=["text"]), "/actions/0/recipient_fields"),
    (lambda d: d["actions"][0].update(recipient_fields=["conversation_title"]), "/actions/0/recipient_fields"),
    (lambda d: d["actions"][0]["params_schema"].pop("additionalProperties"), "/actions/0/params_schema"),
    (lambda d: d["actions"][0]["params_schema"]["properties"]["conversation_title"].pop("maxLength"),
     "/actions/0/params_schema/properties/conversation_title"),
    (lambda d: d["actions"][0]["params_schema"]["properties"]["conversation_title"].update(type="object"),
     "/actions/0/params_schema/properties/conversation_title"),
    (lambda d: d["actions"][0].update(allow_setting="SEND_ALLOWLIST"), "/actions/0/allow_setting"),
    (lambda d: d["actions"][0].update(allow_setting="GATE_EXEC_ALLOWLIST"), "/actions/0/allow_setting"),
    (lambda d: d["actions"][0].update(allow_setting="MARKET_SEND"), "/actions/0/allow_setting"),
])
def test_a_file_that_breaks_a_rule_is_refused_with_the_place_in_it(tmp_path, change, pointer):
    """protects=criterion 6 and X8, X16, B1, H1: a mistake shows at the start, with where it is; fails_when=a broken file loads, or
    the pointer is wrong; no_claim=a file that is valid and wrong about the service."""
    doc = copy.deepcopy(EXAMPLE)
    change(doc)
    with pytest.raises(G.LoadError) as got:
        G.load(write_file(tmp_path, doc))
    assert got.value.pointer.startswith(pointer), (got.value.pointer, got.value.message)


def test_dollar_dollar_is_a_literal_and_only_whole_values_are_replaced():
    """protects=X8's grammar; fails_when=a $ inside a text or $$ is replaced; no_claim=none beyond protects."""
    got = G.substitute({"a": "$params", "b": "$params.x", "c": "$$params", "d": "cost $5", "e": ["$attempt_key"]}, {"x": 1}, "id.2")
    assert got == {"a": {"x": 1}, "b": 1, "c": "$params", "d": "cost $5", "e": ["id.2"]}


def test_a_broken_file_or_a_missing_key_stops_the_start_and_names_the_fix(tmp_path, monkeypatch):
    """protects=X12: the start names the file, the place and the command, or the missing variable; fails_when=the server starts
    without the actions, or the message lacks the fix; no_claim=the Docker entry point's own exit handling."""
    doc = copy.deepcopy(EXAMPLE)
    doc["actions"][0].pop("tool")
    path = write_file(tmp_path, doc)
    with pytest.raises(SystemExit) as got:
        G.check_start({"GATE_ACTIONS_FILE": str(path), "GATE_GATEWAY_URL": URL, "GATE_EXEC_KEY": EXEC})
    assert str(path) in str(got.value) and "/actions/0" in str(got.value) and G.CHECK in str(got.value)
    with pytest.raises(SystemExit, match="GATE_EXEC_KEY"):
        G.check_start({"GATE_ACTIONS_FILE": str(write_file(tmp_path, EXAMPLE)), "GATE_GATEWAY_URL": URL})
    assert G.check_start({}) == []


async def test_neither_key_reaches_an_error_a_log_line_or_an_answer(web, market, caplog):
    """protects=criterion 7; fails_when=a key's value appears anywhere an agent, the owner or the log can read it; no_claim=keys in
    the process environment, which whoever runs the server controls."""
    app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    got = await call(mcp, "approval_request", **ASK)
    market.gw.mode = "refuse"
    _, r = await approve(app, mcp)
    texts = [json.dumps(got), r.text, audit_text(mcp), caplog.text]
    assert not [t for t in texts if EXEC in t or AGENT in t]
    assert set(G.secrets_of({"GATE_EXEC_KEY": EXEC, "GATE_GATEWAY_URL": "https://bot:pw-made-up@gateway.example.org/mcp"})) >= {EXEC, "bot", "pw-made-up"}


async def test_describe_that_fails_refuses_at_queue_and_puts_the_request_back_at_approval(web, market):
    """protects=S2: no card from a service that could not be asked, and no send when it cannot be asked again; fails_when=a card
    is queued, the service's error text reaches the agent, or the request is lost; no_claim=a describe that hangs past its timeout."""
    app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    market.describe_error = True
    got = await refused(mcp, **ASK)
    assert got["reason"] == "describe_failed" and got["retryable"] is True and "secret-describe-body" not in json.dumps(got)
    market.describe_error = False
    await call(mcp, "approval_request", **ASK)
    market.describe_error = True
    _, r = await approve(app, mcp)
    assert r.status_code == 502 and market.sends() == [] and len(mcp._icloud_dashboard.sources["external"].pending()) == 1


async def test_a_disabled_action_is_not_offered_and_timeout_seconds_is_used(web, market, tmp_path, monkeypatch):
    """protects=D6: enabled and timeout_seconds take effect; fails_when=a disabled action is listed or the timeout is ignored;
    no_claim=a real slow gateway."""
    doc = copy.deepcopy(EXAMPLE)
    second = copy.deepcopy(doc["actions"][0])
    second.update(action="market.message.hold", enabled=False)
    doc["actions"][0]["timeout_seconds"] = 7
    doc["actions"].append(second)
    monkeypatch.setenv("GATE_ACTIONS_FILE", str(write_file(tmp_path, doc)))
    P._loaded.cache_clear()
    _app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    assert [a["action"] for a in (await call(mcp, "approval_list_actions"))["actions"]] == ["market.message.send"]
    await call(mcp, "approval_request", **ASK)
    assert market.timeouts and set(market.timeouts) == {7.0}


def test_the_check_command_previews_a_good_file_and_points_at_a_bad_one(tmp_path, capsys):
    """protects=X1: an author can check a file before the server reads it; fails_when=a good file exits non-zero or a bad one
    exits zero, or the pointer is missing; no_claim=the wording of the preview."""
    assert cli.main(["x", "check", str(write_file(tmp_path, EXAMPLE))]) == 0
    out = capsys.readouterr().out
    assert "market.message.send" in out and "MARKET_SEND_ALLOWLIST" in out and "never repeated" in out
    doc = copy.deepcopy(EXAMPLE)
    doc["version"] = 2
    assert cli.main(["x", "check", str(write_file(tmp_path, doc))]) == 2
    assert "/version" in capsys.readouterr().err


def test_the_probe_finds_a_tool_an_agent_key_can_see(tmp_path, monkeypatch, capsys):
    """protects=X1 --probe and E-16: the bypass shows before anything is approved; fails_when=a send tool visible to an agent key
    passes, or a clean setup fails; no_claim=a gateway that hides a tool from the list but still allows the call."""
    clean = Market({EXEC: {"market-call_operation", "market-call_write_operation"}, AGENT: {"market-call_operation"}})
    monkeypatch.setenv("GATE_GATEWAY_URL", URL)
    monkeypatch.setenv("GATE_EXEC_KEY", EXEC)
    monkeypatch.setenv("GATE_AGENT_KEY_PROBE", AGENT)
    path = str(write_file(tmp_path, EXAMPLE))
    assert cli.main(["x", "check", path, "--probe"], make_http=clean.gw.make_http) == 0
    leaky = Market({EXEC: {"market-call_operation", "market-call_write_operation"}, AGENT: {"market-call_operation", "market-call_write_operation"}})
    assert cli.main(["x", "check", path, "--probe"], make_http=leaky.gw.make_http) == 1
    assert "agents could bypass" in capsys.readouterr().err


def test_the_docs_name_every_key_of_the_file_and_the_example_loads():
    """protects=X3, X14: the reference covers the format and its example works; fails_when=a key of the file schema is missing from
    the reference, or the example does not load; no_claim=that the reference's wording is right."""
    doc = (ROOT / "docs" / "SENDGATE.md").read_text()
    ref = doc[doc.index("## Connector file reference"):doc.index("## Refusal codes")]
    keys = list(G.FILE_SCHEMA["properties"]) + list(G.FILE_SCHEMA["$defs"]["action"]["properties"]) \
        + ["describe", "copy", "recheck", "path", "sent", "failed", "$params", "$attempt_key", "$$"]
    assert not [k for k in keys if f"`{k}" not in ref], [k for k in keys if f"`{k}" not in ref]
    assert [fa.action for fa in G.load(ROOT / "docs" / "examples" / "actions.example.json")] == ["market.message.send"]
    assert json.loads((ROOT / "docs" / "examples" / "actions.schema.json").read_text()) == G.FILE_SCHEMA


def test_result_values_match_in_type_and_value_and_a_missing_optional_field_is_left_out():
    """protects=the review's true-is-1 finding and optional placeholders; fails_when=true matches 1 or "1", or a missing
    $params.<name> reaches the call as null; no_claim=none beyond protects."""
    assert G._same(True, True) and G._same(1, 1.0) and G._same("a", "a")
    assert not G._same(True, 1) and not G._same(1, "1") and not G._same(0, False)
    assert G.substitute({"a": "$params.x", "b": "$params.y"}, {"x": 1}, "k") == {"a": 1}


async def test_a_list_without_the_id_refuses_before_any_call_to_the_service(web, market):
    """protects=E-6: a request off the owner's list never reaches the gateway, not even describe; fails_when=it queues or the
    gateway sees a call; no_claim=none beyond protects."""
    _app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-2")
    assert (await refused(mcp, **ASK))["reason"] == "not_allowed" and market.gw.calls == []


async def test_a_field_the_file_does_not_declare_never_reaches_the_service(web, market):
    """protects=the review's hidden-field finding: what the owner does not see is not sent; fails_when=an extra field queues or is
    passed on; no_claim=none beyond protects."""
    _app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    extra = {"action": ASK["action"], "params": {**ASK["params"], "cc": "someone-else"}}
    await refused(mcp, **extra)
    assert market.sends() == []
    fa = G.load(os.environ["GATE_ACTIONS_FILE"])[0]
    assert G._agent_params(fa, {**ASK["params"], "cc": "x", "conversation_title": "forged"}) == ASK["params"]


async def test_a_copied_value_of_another_shape_refuses_without_showing_it(web, market):
    """protects=the review's describe-shape finding; fails_when=a card is queued from a value the schema does not allow, or the value
    reaches the agent; no_claim=a value of the right shape and wrong content."""
    _app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    market.titles["c-1"] = "x" * 500 + "secret-long-title"
    got = await refused(mcp, **ASK)
    assert got["reason"] == "describe_failed" and "secret-long-title" not in json.dumps(got)


async def test_a_new_card_after_a_discard_has_its_own_hash_and_the_audit_names_the_reason(web, market):
    """protects=S1: after the target changed, the agent asks again and the owner sees the new title; fails_when=the old card comes
    back, the hash repeats, or the audit lacks the reason; no_claim=the dashboard's wording."""
    app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    await call(mcp, "approval_request", **ASK)
    first = mcp._icloud_dashboard.sources["external"].pending()[0]
    market.titles["c-1"] = "Bike trailer, one seat"
    await approve(app, mcp)
    assert "target_changed" in audit_text(mcp)
    await call(mcp, "approval_request", **ASK)
    second = mcp._icloud_dashboard.sources["external"].pending()[0]
    assert second.id != first.id and second.sha256 != first.sha256
    _, r = await approve(app, mcp)
    assert r.status_code == 200 and len(market.sends()) == 1


def test_a_broken_file_skips_the_plugin_but_stops_the_server_start(tmp_path, monkeypatch, web):
    """protects=X12 split: loading plugins elsewhere (a CLI, a test) does not exit the process, the server start does; fails_when=
    load_plugins raises SystemExit, or create_server starts without the actions; no_claim=none beyond protects."""
    doc = copy.deepcopy(EXAMPLE)
    doc["actions"][0].pop("tool")
    for k, v in {"GATE_ACTIONS_FILE": str(write_file(tmp_path, doc)), "GATE_GATEWAY_URL": URL, "GATE_EXEC_KEY": EXEC}.items():
        monkeypatch.setenv(k, v)
    P._loaded.cache_clear()
    try:
        assert "gateway" not in [p.id for p in P.load_plugins()]
        with pytest.raises(SystemExit, match="/actions/0"):
            web(MARKET_SEND_ALLOWLIST="c-1")
    finally:
        P._loaded.cache_clear()


@pytest.mark.parametrize("grants,expect", [
    ({EXEC: {"market-call_operation"}, AGENT: {"market-call_operation"}}, "does NOT see market-call_write_operation"),
    ({EXEC: {"market-call_operation", "market-call_write_operation"}}, "bypass was not checked"),
])
def test_the_probe_fails_on_a_missing_grant_or_an_agent_key_it_cannot_check(tmp_path, monkeypatch, capsys, grants, expect):
    """protects=X1 --probe: an unchecked bypass is a problem, not a pass; fails_when=either case exits 0; no_claim=none beyond
    protects."""
    m = Market(grants)
    for k, v in {"GATE_GATEWAY_URL": URL, "GATE_EXEC_KEY": EXEC, "GATE_AGENT_KEY_PROBE": AGENT}.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["x", "check", str(write_file(tmp_path, EXAMPLE)), "--probe"], make_http=m.gw.make_http) == 1
    got = capsys.readouterr()
    assert expect in got.out + got.err and EXEC not in got.out + got.err and AGENT not in got.out + got.err


def test_the_probe_fails_when_the_gateway_is_down(tmp_path, monkeypatch, capsys):
    """protects=X1 --probe; fails_when=a gateway that cannot be reached passes; no_claim=none beyond protects."""
    m = Market({EXEC: {"market-call_operation", "market-call_write_operation"}})
    m.gw.mode = "down"
    monkeypatch.setenv("GATE_GATEWAY_URL", URL)
    monkeypatch.setenv("GATE_EXEC_KEY", EXEC)
    assert cli.main(["x", "check", str(write_file(tmp_path, EXAMPLE)), "--probe"], make_http=m.gw.make_http) == 1
    assert "could not list tools" in capsys.readouterr().err
