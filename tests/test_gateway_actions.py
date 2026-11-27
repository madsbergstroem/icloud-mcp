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
from test_dashboard import PASSWORD, api, client, signed_in, web  # noqa: E402,F401  (web is a fixture)
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
    from icloud_mcp.gate_connectors import probe as _probe
    monkeypatch.setattr(_probe, "FRESH", -1.0)               # every health check runs the probe again (reuse has its own test)
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


async def approve_held(app, mcp, change):
    """Approve after `change` ran: the owner looked, then the service changed."""
    q = mcp._icloud_dashboard.sources["external"].pending()[0]
    change()
    async with client(app) as c:
        token = await signed_in(c)
        return q, await c.post(url(q.id) + "/approve", json={"sha": q.sha256}, headers=api(token))


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


async def test_a_title_that_changed_after_the_owner_looked_sends_nothing_and_waits_as_a_new_version(web, market):
    """protects=criterion 4 and package 2c: the owner approves what they saw; a change at the service is a new version, not a
    discard; fails_when=the send happens, the request ends, the old hash still approves, or the card hides the change;
    no_claim=a change between the recheck and the send."""
    app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    got = await call(mcp, "approval_request", **ASK)
    q, r = await approve_held(app, mcp, lambda: market.titles.__setitem__("c-1", "Bike trailer, sold"))
    assert r.status_code == 409 and r.json()["state"] == "waiting" and r.json()["version"] == 2 and market.sends() == []
    assert (await call(mcp, "approval_status", id=got["outbox_id"]))["status"] == "waiting"
    it = await item(app, got["outbox_id"])
    assert [v["by"] for v in it["versions"]] == ["assistant", "service"]
    assert it["changed"] == [{"label": "Listing", "old": "Bike trailer, two seats", "new": "Bike trailer, sold"}]
    assert it["rows"][0] == ["Listing", "Bike trailer, sold"] and it["sha"] != q.sha256
    async with client(app) as c:
        token = await signed_in(c)
        stale = await c.post(url(q.id) + "/approve", json={"sha": q.sha256}, headers=api(token))
        assert stale.status_code == 409 and market.sends() == []          # the version the owner looked at first cannot be approved
        ok = await c.post(url(q.id) + "/approve", json={"sha": it["sha"]}, headers=api(token))
    assert ok.status_code == 200 and len(market.sends()) == 1


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
    assert [a["action"] for a in (await call(mcp, "approval_list_actions"))["actions"]] == ["market.message.send", "gate.allow"]   # package 10
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


async def test_a_revision_is_in_the_audit_with_both_hashes_and_the_key_stays_bound(web, market):
    """protects=package 2c, D7: the log names the version the owner looked at and the new one; the agent asking again with the same
    request_id gets the same request; fails_when=the audit lacks a hash, or a second request appears; no_claim=the wording."""
    app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    got = await call(mcp, "approval_request", **ASK, request_id="k-1")
    q, _ = await approve_held(app, mcp, lambda: market.titles.__setitem__("c-1", "Bike trailer, one seat"))
    new = mcp._icloud_dashboard.sources["external"].pending()[0]
    log = audit_text(mcp)
    assert "approval.revised" in log and q.sha256 in log and new.sha256 in log
    again = await call(mcp, "approval_request", **ASK, request_id="k-1")
    assert again["outbox_id"] == got["outbox_id"] and len(mcp._icloud_dashboard.sources["external"].pending()) == 1


async def test_the_owners_edit_is_a_version_and_the_assistants_stays_readable(web, market):
    """protects=package 2c: "Edit text" adds a version; the first stays on the card; fails_when=the edit overwrites, or the list
    lacks the assistant's text; no_claim=none beyond protects."""
    app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    got = await call(mcp, "approval_request", **ASK)
    q = mcp._icloud_dashboard.sources["external"].pending()[0]
    async with client(app) as c:
        token = await signed_in(c)
        r = await c.put(url(q.id) + "/text", json={"sha": q.sha256, "text": "Is it free on Sunday too?"}, headers=api(token))
        assert r.status_code == 200, r.text
    it = await item(app, got["outbox_id"])
    assert [(v["by"], v["text"]) for v in it["versions"]] == [("assistant", ASK["params"]["text"]), ("owner", "Is it free on Sunday too?")]
    assert it["versions"][1]["changes"] == [{"label": "Text", "old": ASK["params"]["text"], "new": "Is it free on Sunday too?"}]
    assert "changed" not in it


async def test_the_assistant_can_revise_its_own_waiting_request_and_nothing_else(web, market):
    """protects=package 2c replaces: a revision is a new version of the same request, through every check; fails_when=a second
    request appears, the revision skips the owner's list, or another request_id or action is accepted; no_claim=two agents behind
    one client label."""
    app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    got = await call(mcp, "approval_request", **ASK, request_id="k-1")
    revised = {"action": ASK["action"], "params": {**ASK["params"], "text": "Still free on Saturday? I can come at nine."}}
    r = await call(mcp, "approval_request", **revised, request_id="k-1", replaces=got["outbox_id"])
    assert r["outbox_id"] == got["outbox_id"] and r["replaced"] is True and r["version"] == 2
    assert len(mcp._icloud_dashboard.sources["external"].pending()) == 1
    it = await item(app, got["outbox_id"])
    assert [v["by"] for v in it["versions"]] == ["assistant", "assistant"] and it["text"] == revised["params"]["text"]
    assert (await refused(mcp, **revised, request_id="k-2", replaces=got["outbox_id"]))["reason"] == "not_replaceable"
    off_list = {"action": ASK["action"], "params": {**ASK["params"], "conversation_id": "c-2"}}
    assert (await refused(mcp, **off_list, replaces=got["outbox_id"]))["reason"] == "not_allowed"
    _, done = await approve(app, mcp)
    assert done.status_code == 200
    assert (await refused(mcp, **revised, replaces=got["outbox_id"]))["reason"] == "not_replaceable"   # it no longer waits


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


# -- package 2b: agent-key probe, approval held, health lines, the owner's lists ---------------------------------------------------
def with_probe(monkeypatch, *keys):
    monkeypatch.setenv("GATE_AGENT_KEY_PROBE", ",".join(keys))
    P._loaded.cache_clear()


async def health(mcp):
    return (await call(mcp, "icloud_check_health"))["areas"]["gateway"]


async def item(app, item_id):
    async with client(app) as c:
        await signed_in(c)
        return (await c.get(url(item_id))).json()


async def test_without_a_probe_key_the_card_and_health_say_not_checked_and_nothing_blocks(web, market):
    """protects=E1 unset, S-e: "not checked" is a note and never blocks; fails_when=it blocks approval, or neither health nor the card
    says so; no_claim=none beyond protects."""
    app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    got = await call(mcp, "approval_request", **ASK)
    assert "warnings" not in got
    it = await item(app, got["outbox_id"])
    assert "held" not in it and "no probe key is set" in " ".join(it["notes"])
    h = await health(mcp)
    assert h["ok"] is True and [x["level"] for x in h["actions"]] == ["warning"]
    _, r = await approve(app, mcp)
    assert r.status_code == 200 and len(market.sends()) == 1


async def test_a_probe_key_that_sees_the_send_tool_holds_approval_until_the_grant_is_gone(web, market, monkeypatch, caplog):
    """protects=D3, X10, S-e, E1: the request is queued with warnings, approval sends nothing, the card says why, health reports a
    problem; after the grant is removed and the probe runs again, approval works. No probe key value appears anywhere.
    fails_when=a held request is sent, or stays held after a clean probe; no_claim=a gateway that hides a tool from the list."""
    market.gw.grants["Bearer " + AGENT] = {"market-call_operation", "market-call_write_operation"}
    with_probe(monkeypatch, AGENT)
    app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    h = await health(mcp)
    assert h["ok"] is False and h["actions"][0]["level"] == "problem"
    got = await call(mcp, "approval_request", **ASK)
    assert got["status"] == "queued_for_owner_approval" and got["warnings"] == ["bypassable"]
    it = await item(app, got["outbox_id"])
    assert "agent key can call this tool" in it["held"]
    _, r = await approve(app, mcp)
    assert r.status_code == 409 and market.sends() == [] and len(mcp._icloud_dashboard.sources["external"].pending()) == 1
    market.gw.grants["Bearer " + AGENT] = {"market-call_operation"}
    assert (await health(mcp))["ok"] is True
    assert "held" not in await item(app, got["outbox_id"])
    _, r = await approve(app, mcp)
    assert r.status_code == 200 and len(market.sends()) == 1
    texts = [json.dumps(got), json.dumps(h), r.text, audit_text(mcp), caplog.text]
    assert not [t for t in texts if AGENT in t]


async def test_two_probe_keys_one_bypassing_is_a_problem_and_a_key_that_cannot_list_is_not_checked(web, market, monkeypatch):
    """protects=E1 with several keys; fails_when=one clean key hides the other's grant, or a key that cannot list reads ok;
    no_claim=none beyond protects."""
    other = "agent-key-made-up-second"
    market.gw.grants["Bearer " + other] = {"market-call_operation", "market-call_write_operation"}
    with_probe(monkeypatch, AGENT, other)
    _app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    assert (await health(mcp))["actions"][0]["level"] == "problem"
    del market.gw.grants["Bearer " + other]                              # the bypassing key now cannot list: it stays bypassable
    a = (await health(mcp))["actions"][0]
    assert a["level"] == "problem"                                        # only a run where every key listed clears it


def test_a_key_that_cannot_list_never_clears_bypassable_and_a_clean_run_does():
    """protects=D3 after the adversarial review: a transient failure of the bypassing key keeps approval off; fails_when=a failed
    list reopens approval, or a fully clean run keeps it closed; no_claim=none beyond protects."""
    from icloud_mcp.gate_connectors import probe as PR
    answers = {"k": {"send"}}

    class C:
        def __init__(self, key):
            self.key = key

        def list_tools(self):
            got = answers[self.key]
            if got is None:
                raise RuntimeError("down")
            return got
    pr = PR.Probe(["k"], [("a.one", "send")], C)
    run = lambda: (setattr(pr, "checked_at", None), pr.run())   # noqa: E731  -- as if past the FRESH window
    run()
    assert pr.state("a.one").state == "bypassable"
    answers["k"] = None
    run()
    assert pr.state("a.one").state == "bypassable" and pr.hold("a.one").level == "blocked"
    answers["k"] = {"read"}
    run()
    assert pr.state("a.one").state == "ok" and pr.hold("a.one") is None
    fresh = PR.Probe(["k"], [("a.one", "send")], C)
    answers["k"] = None
    fresh.run()
    assert fresh.state("a.one") == PR.State("unchecked", "unreachable") and fresh.hold("a.one").level == "note"


async def test_one_list_per_probe_key_per_run_and_the_start_does_not_wait_on_a_hanging_gateway(web, market, monkeypatch):
    """protects=E-14; fails_when=a run lists more than once per key, or the start waits for the probe; no_claim=none beyond protects."""
    other = "agent-key-made-up-second"
    market.gw.grants["Bearer " + other] = {"market-call_operation"}
    with_probe(monkeypatch, AGENT, other)
    market.gw.mode, market.gw.delay = "slow", 3.0
    import time as _t
    t0 = _t.monotonic()
    _app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    assert _t.monotonic() - t0 < 2.0
    market.gw.mode = None
    import threading as _th
    for t in [t for t in _th.enumerate() if t.name == "gate-probe"]:
        t.join(10)                                         # the start's own run, still on the hanging gateway
        assert not t.is_alive()
    market.gw.lists.clear()
    await health(mcp)
    assert sorted(market.gw.lists) == sorted([AGENT, other])


def test_health_lines_put_problems_first_then_warnings_then_ok():
    """protects=H2 in the report; fails_when=a bypassable action among four is not first; no_claim=the dashboard's folding (clicked
    in the browser)."""
    from icloud_mcp.gate_connectors.probe import Probe, State
    pr = Probe(["k"], [("a.one", "t1"), ("a.two", "t2"), ("a.three", "t3"), ("a.four", "t4")], lambda k: None)
    pr._states = {"a.one": State("ok"), "a.two": State("unchecked", "unreachable"), "a.three": State("ok"), "a.four": State("bypassable")}
    assert [x["action"] for x in pr.lines({})] == ["a.four", "a.two", "a.one", "a.three"]


async def test_the_server_page_sorts_problems_first(web, market, monkeypatch):
    """protects=H2 on the dashboard; fails_when=the bypassable action is not above the lines that are fine; no_claim=none beyond protects."""
    market.gw.grants["Bearer " + AGENT] = {"market-call_operation", "market-call_write_operation"}
    with_probe(monkeypatch, AGENT)
    app, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    async with client(app) as c:
        token = await signed_in(c)
        await c.post("/dashboard/api/server/check", headers=api(token))
        areas = (await c.get("/dashboard/api/server")).json()["areas"]
    levels = [a["level"] for a in areas]
    market_line = next(i for i, a in enumerate(areas) if a["area"] == "Market · Marketplace reply")
    assert areas[market_line]["level"] == "problem" and areas[market_line]["note"] == "Bypassable"
    assert levels == sorted(levels, key=lambda x: {"problem": 0, "warning": 1}.get(x, 2))


async def test_the_owners_list_is_a_setting_that_says_it_reaches_nobody_and_widening_asks_to_confirm(web, market):
    """protects=S5, E2, J1: each allow_setting is a field of the connector's settings page; empty shows the hint; widening without a
    recent sign-in asks to confirm and names the action; narrowing does not. fails_when=any of these; no_claim=the hint's link focus
    (clicked in the browser)."""
    from test_settings import stale
    app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="")
    d = mcp._icloud_dashboard
    async with client(app) as c:
        token = await signed_in(c)
        st = (await c.get("/dashboard/api/settings")).json()
        g = next(x for x in st["groups"] if x["slug"] == "gateway")
        f = next(x for x in g["fields"] if x["key"] == "MARKET_SEND_ALLOWLIST")
        assert f["value"] == "" and "Marketplace reply" in f["empty_hint"] and g["summary"] == "1 reach nobody"
        stale(d)
        r = await c.post("/dashboard/api/settings/gateway", headers=api(token), json={"values": {"MARKET_SEND_ALLOWLIST": "c-1"}})
        assert r.status_code == 403 and r.json()["confirm"] is True and "Marketplace reply" in r.json()["why"]
        await c.post("/dashboard/api/signin", headers=api(token), json={"password": PASSWORD})
        r = await c.post("/dashboard/api/settings/gateway", headers=api(token), json={"values": {"MARKET_SEND_ALLOWLIST": "c-1,c-2"}})
        assert r.status_code == 200
        stale(d)
        r = await c.post("/dashboard/api/settings/gateway", headers=api(token), json={"values": {"MARKET_SEND_ALLOWLIST": "c-1"}})
        assert r.status_code == 200                                               # narrowing needs no confirm


def test_a_hold_that_raises_blocks_and_one_that_answers_nonsense_is_ignored():
    """protects=the gate's hold contract: approving never depends on a function that failed; fails_when=a raising hold lets approval
    through, or a value that is not a Hold blocks; no_claim=none beyond protects."""
    from icloud_mcp.gate import actions as A
    def boom():
        raise RuntimeError("x")
    reg = lambda hold: A.Registration(None, None, lambda: (), hold=hold)   # noqa: E731
    assert reg(lambda approving: boom()).hold_now().level == "blocked"
    assert reg(lambda approving: "blocked").hold_now() is None and reg(None).hold_now() is None
    assert reg(lambda approving: A.Hold("note", "unchecked", "n")).hold_now().level == "note"


@pytest.mark.parametrize("old,new,widens", [("", "c-1", True), ("c-1", "*", True), ("c-1", "c-1,c-2", True), ("c-1", "c-2", True),
                                             ("c-1", "C-1", False), ("c-1,c-2", "c-2", False), ("c-1", " c-1 ,", False), ("", "", False)])
def test_only_adding_someone_counts_as_widening(old, new, widens, market):
    """protects=S5 widening-only confirm; fails_when=adding * or an entry is not loosening, or a removal or a case change asks;
    no_claim=none beyond protects."""
    found = G.load(os.environ["GATE_ACTIONS_FILE"])
    spec_ = next(sp for sp in G.settings_group(found).specs if sp.key == "MARKET_SEND_ALLOWLIST")
    assert spec_.loosens(old, new) is widens


async def test_a_hold_that_turns_on_after_the_owner_looked_stops_the_release_itself(web, market):
    """protects=the second hold check in release, behind the dashboard's: a probe that flips between the look and the click sends
    nothing; fails_when=release claims or sends while held; no_claim=none beyond protects."""
    from icloud_mcp.gate import actions as A
    _app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    await call(mcp, "approval_request", **ASK)
    src = mcp._icloud_dashboard.sources["external"]
    q = src.pending()[0]
    reg = src.registry.get("market.message.send")
    object.__setattr__(reg, "hold", lambda approving: A.Hold("blocked", "bypassable", "held now"))
    with pytest.raises(A.ExternalError, match="held now"):
        src.release(q)
    assert market.sends() == [] and len(src.pending()) == 1


def test_a_run_right_after_another_takes_its_result():
    """protects=the performance review's queueing: callers that waited for a run reuse it; fails_when=a second call right after
    lists again; no_claim=none beyond protects."""
    from icloud_mcp.gate_connectors import probe as PR
    calls = []

    class C:
        def __init__(self, key):
            calls.append(key)

        def list_tools(self):
            return {"read"}
    pr = PR.Probe(["k1", "k2"], [("a.one", "send")], C)
    pr.run()
    pr.run()
    assert sorted(calls) == ["k1", "k2"] and pr.state("a.one").state == "ok"


def test_required_mode_blocks_when_not_checked_and_an_old_ok_is_checked_again_before_approving(monkeypatch):
    """protects=the owner's 2b decisions (2026-11-15): GATE_PROBE_REQUIRED turns "not checked" into a block, and an approval after
    MAX_AGE probes first; fails_when=required mode only notes, or a grant added after an old clean run is approved; no_claim=none
    beyond protects."""
    from icloud_mcp.gate_connectors import probe as PR
    seen = {"k": {"read"}}

    class C:
        def __init__(self, key):
            self.key = key

        def list_tools(self):
            if seen[self.key] is None:
                raise RuntimeError("down")
            return seen[self.key]
    strict = PR.Probe(["k"], [("a.one", "send")], C, required=True)
    assert strict.hold("a.one").level == "blocked"                       # no run yet
    nokey = PR.Probe([], [("a.one", "send")], C, required=True)
    assert nokey.hold("a.one").level == "blocked" and "GATE_PROBE_REQUIRED" in nokey.hold("a.one").text
    pr = PR.Probe(["k"], [("a.one", "send")], C)
    pr.run()
    assert pr.hold("a.one") is None
    seen["k"] = {"read", "send"}                                           # a grant added after the clean run
    assert pr.hold("a.one") is None                                        # a look does not probe
    monkeypatch.setattr(PR, "MAX_AGE", -1.0)
    monkeypatch.setattr(PR, "FRESH", -1.0)                                # as if both windows had passed
    assert pr.hold("a.one", approving=True).level == "blocked"             # an approval after MAX_AGE does


async def test_a_recheck_that_answers_an_empty_mapping_sends_nothing(web, market):
    """protects=the adversarial review's empty-mapping finding: anything but None from a recheck stops the send; fails_when=the
    executor runs; no_claim=none beyond protects."""
    _app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    await call(mcp, "approval_request", **ASK)
    src = mcp._icloud_dashboard.sources["external"]
    q = src.pending()[0]
    object.__setattr__(src.registry.get("market.message.send"), "recheck", lambda params: {})
    with pytest.raises(Exception):
        src.release(q)
    assert market.sends() == []


async def test_the_assistants_revision_is_in_the_log_with_the_hash_it_replaced(web, market):
    """protects=the adversarial review's audit finding for replaces; fails_when=the log lacks replaced, the version or the old hash;
    no_claim=none beyond protects."""
    _app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    got = await call(mcp, "approval_request", **ASK)
    first = mcp._icloud_dashboard.sources["external"].pending()[0].sha256
    await call(mcp, "approval_request", action=ASK["action"], params={**ASK["params"], "text": "Free on Sunday?"}, replaces=got["outbox_id"])
    log = audit_text(mcp)
    assert '"replaced": true' in log.replace("'", '"').lower() or "replaced" in log
    assert first in log and "previous_sha256" in log
