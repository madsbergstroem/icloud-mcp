"""Beeper's networks in the dashboard's account list (beeper.AccountsReader, dashboard_accounts.py) and requests of the external
source named by their type where the dashboard counts kinds. The gateway is the made-up one of test_beeper; every name, id,
address and number is made up."""
import json
import logging

import pytest

from icloud_mcp import actions as A
from icloud_mcp import beeper as B
from test_beeper import ACCOUNTS_TEXT, KEY, READ_KEY, URL, ask, gateway, hub, launch  # noqa: F401  (fixtures)
from test_dashboard import client, signed_in, web  # noqa: F401  (web is a fixture)

PRIVATE = ("alex@example.org", "+15550100", "+15550199")          # what Beeper's answer holds and must never leave


def beeper_of(r: dict) -> dict | None:
    """The Beeper plugin's entry in the list (plugins/beeper), in the shape these tests have always read: state, problem, sending,
    reading and the networks with their account ids. None where the plugin is not set up."""
    p = next((x for x in r["plugins"] if x["id"] == "beeper" and x["enabled"]), None)
    if p is None:
        return None
    lst = p["listing"]
    return {"state": lst["state"], "problem": lst["problem"] if lst["state"] != "ok" else "", "sending": p["messages"]["sending"],
            "reading": p["messages"]["reading"], "networks": [{"network": i["title"], "user": i["sub"], "account_id": i["detail"].removeprefix("Account ID ")}
                                                            for i in lst["items"]]}


async def accounts(app) -> dict:
    async with client(app) as c:
        await signed_in(c)
        r = await c.get("/dashboard/api/accounts")
    assert r.status_code == 200, r.text
    return {**r.json(), "beeper": beeper_of(r.json())}


# -- the networks -----------------------------------------------------------------------------------------------------------------
async def test_each_network_is_listed_with_its_name_and_id_and_beepers_own_account_is_not(web, hub, gateway, caplog):
    caplog.set_level(logging.DEBUG)
    app, mcp, _src = launch(web, hub, BEEPER_READ_KEY=READ_KEY)
    r = await accounts(app)
    assert r["beeper"] == {"state": "ok", "problem": "", "sending": True, "reading": False, "networks": [
        {"network": "WhatsApp", "user": "Alex Example", "account_id": "wa-example-1"},
        {"network": "Signal", "user": "Alex E.", "account_id": "signal-example-2"}]}
    assert [a["label"] for a in r["accounts"]] == ["iCloud"]                 # the saved accounts are still there, first
    text = json.dumps(r) + caplog.text
    assert not [p for p in PRIVATE if p in text] and "matrix" not in json.dumps(r)
    assert "Alex Example" not in caplog.text and "wa-example-1" not in caplog.text   # the log has the outcome only
    assert "beeper accounts: ok (2 networks)" in caplog.text
    assert gateway.seen_keys[-1] == f"Bearer {READ_KEY}"


async def test_the_answer_is_kept_a_few_minutes_and_then_asked_again(web, hub, gateway, monkeypatch):
    app, mcp, _src = launch(web, hub, BEEPER_READ_KEY=READ_KEY)
    clock = [1000.0]
    monkeypatch.setattr(B.time, "monotonic", lambda: clock[0])
    await accounts(app)
    await accounts(app)
    assert gateway.accounts_calls == 1
    clock[0] += B.AccountsReader.KEEP_SECONDS + 1
    await accounts(app)
    assert gateway.accounts_calls == 2
    assert B.AccountsReader.KEEP_SECONDS <= 300


async def test_without_the_read_key_beeper_says_so_and_the_gateway_is_not_asked(web, hub, gateway):
    app, mcp, _src = launch(web, hub)
    r = await accounts(app)
    assert r["beeper"]["state"] == "no_read_key" and r["beeper"]["networks"] == []
    assert r["beeper"]["problem"] == "Accounts not readable: no read key"
    assert gateway.accounts_calls == 0


async def test_without_the_gateway_there_is_no_beeper_part(web, hub, gateway):
    app, mcp, *_ = web(BEEPER_READ_KEY=READ_KEY)
    assert (await accounts(app))["beeper"] is None and gateway.accounts_calls == 0


@pytest.mark.parametrize("mode,state,problem", [
    ("down", "unreachable", "Gateway not reachable"),
    ("refuse", "refused", "Accounts not readable: the gateway refused the read key"),
])
async def test_a_gateway_that_does_not_answer_leaves_one_line_and_the_page_stands(web, hub, gateway, mode, state, problem):
    app, mcp, _src = launch(web, hub, BEEPER_READ_KEY=READ_KEY)
    gateway.mode = mode
    r = await accounts(app)
    assert (r["beeper"]["state"], r["beeper"]["problem"], r["beeper"]["networks"]) == (state, problem, [])
    assert [a["label"] for a in r["accounts"]] == ["iCloud"]


async def test_the_exec_key_cannot_stand_in_for_the_read_key(web, hub, gateway):
    app, mcp, _src = launch(web, hub, BEEPER_READ_KEY=KEY)           # set by mistake to the key that sends
    assert (await accounts(app))["beeper"]["state"] == "refused"


@pytest.mark.parametrize("answer", ["Chat not found", "# Accounts\n\n## WhatsApp\n**User**: Alex Example\n", ""])
async def test_an_answer_that_is_no_list_of_accounts_reads_as_unreadable_or_empty(web, hub, gateway, answer):
    app, mcp, _src = launch(web, hub, BEEPER_READ_KEY=READ_KEY)
    gateway.accounts_answer = answer
    b = (await accounts(app))["beeper"]
    assert b["networks"] == []
    assert b["state"] == ("ok" if answer.startswith("# Accounts") else "unreadable")


async def test_sending_off_is_said_when_only_the_read_key_is_set(web, hub, gateway):
    app, mcp, *_ = web(BEEPER_GATEWAY_URL=URL, BEEPER_READ_KEY=READ_KEY)
    b = (await accounts(app))["beeper"]
    assert b["state"] == "ok" and b["sending"] is False


async def test_the_read_key_is_masked_in_errors_and_not_on_the_settings_page(web, hub, gateway):
    app, mcp, _src = launch(web, hub, BEEPER_READ_KEY=READ_KEY)
    from icloud_mcp import server as S
    assert READ_KEY in S._error_secrets
    async with client(app) as c:
        await signed_in(c)
        page = (await c.get("/dashboard/api/settings")).text
    assert READ_KEY not in page and "BEEPER_READ_KEY" not in page


def test_beepers_text_answer_and_structured_data_give_the_same_networks():
    want = [B.Network("WhatsApp", "Alex Example", "wa-example-1"), B.Network("Signal", "Alex E.", "signal-example-2")]
    assert B._networks_of(B.Answer(False, None, ACCOUNTS_TEXT)) == want
    assert B._networks_of(B.Answer(False, {"result": ACCOUNTS_TEXT}, "")) == want
    data = [{"accountID": "wa-example-1", "network": "WhatsApp", "user": {"fullName": "Alex Example", "email": "alex@example.org"}},
            {"accountID": "matrix", "network": "Beeper", "user": {"fullName": "Alex Example"}},
            {"accountID": "signal-example-2", "network": "Signal", "user": {"fullName": "Alex E.", "phoneNumber": "+15550199"}}]
    assert B._networks_of(B.Answer(False, data, "")) == want
    assert B._networks_of(B.Answer(False, {"accounts": data}, "")) == want
    assert B._networks_of(B.Answer(True, None, ACCOUNTS_TEXT)) is None


def test_a_reader_that_trips_over_something_unexpected_still_answers(monkeypatch):
    from icloud_mcp.config import Settings
    monkeypatch.setenv("BEEPER_GATEWAY_URL", URL)
    monkeypatch.setenv("BEEPER_READ_KEY", READ_KEY)
    reader = B.AccountsReader(Settings.from_env())
    monkeypatch.setattr(reader.gateway, "call", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert reader.read() == {"state": "unreadable", "networks": []}


# -- waiting by kind --------------------------------------------------------------------------------------------------------------
async def test_a_beeper_request_counts_as_beeper_and_an_unknown_type_as_other(web, hub, gateway):
    app, mcp, src = launch(web, hub)
    async with client(app) as c:
        await signed_in(c)
        assert (await c.get("/dashboard/api/state")).json()["kinds"]["external:Beeper"] == 0     # a line even with nothing waiting
        await ask(mcp)
        src.box.add(A.canonical("example.gone", {"x": 1}), ["someone"], action_type="example.gone")
        kinds = (await c.get("/dashboard/api/state")).json()["kinds"]
        today = (await c.get("/dashboard/api/today")).json()["waiting"]
    assert kinds["external:Beeper"] == 1 and kinds["external:Other"] == 1 and "external" not in kinds
    assert sorted(w["kind_label"] for w in today if w["kind"] == "external") == ["Beeper", "Other"]
    assert all("kind_label" not in w for w in today if w["kind"] != "external")
