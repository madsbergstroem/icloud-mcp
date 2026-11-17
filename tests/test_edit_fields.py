"""Package 2d (docs/SENDGATE.md): the owner changes every field an action marks editable before approving, in one form, and a
recipient the owner types in by hand is allowed for that request only. A made-up marketplace behind the fake gateway; every name
and id is made up. Each test says what it protects, the wrong result it rejects and what a green run does not prove."""
import copy
import dataclasses
import json

import pytest

from test_gateway_actions import (ASK, EXAMPLE, G, P, approve, call, item, market, refused, url, write_file)  # noqa: F401  (market is a fixture)
from test_dashboard import api, client, signed_in, web  # noqa: F401  (web is a fixture)
from test_external_approvals import audit_text


def editable_doc():
    """The example with the conversation shown in a row and editable, beside the offer the example already lets the owner change."""
    doc = copy.deepcopy(EXAMPLE)
    a = doc["actions"][0]
    a["rows"].append(["Chat", "conversation_id"])
    a["editable_fields"] = ["offer", "conversation_id"]
    return doc


@pytest.fixture
def editable(market, tmp_path, monkeypatch):
    monkeypatch.setenv("GATE_ACTIONS_FILE", str(write_file(tmp_path, editable_doc())))
    P._loaded.cache_clear()
    return market


async def put_fields(app, q, fields):
    async with client(app) as c:
        token = await signed_in(c)
        return await c.put(url(q.id) + "/fields", json={"sha": q.sha256, "fields": fields}, headers=api(token))


def pending(mcp):
    return mcp._icloud_dashboard.sources["external"].pending()[0]


OFFER = {**ASK, "params": {**ASK["params"], "offer": 35}}


async def test_one_save_changes_several_fields_as_one_version_and_the_send_carries_them(web, editable):
    """protects=2d: the form changes the offer and the text in one save, the card lists both, and approving sends exactly that;
    fails_when=two versions, a change missing from the list, or the service gets the assistant's values; no_claim=mail."""
    app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    got = await call(mcp, "approval_request", **OFFER)
    it = await item(app, got["outbox_id"])
    assert [f["name"] for f in it["edit"]["fields"]] == ["offer", "conversation_id", "text"]      # the rows, then the text
    r = await put_fields(app, pending(mcp), {"offer": 40, "text": "Could you do 40?"})
    assert r.status_code == 200 and r.json()["changed"], r.text
    it = await item(app, got["outbox_id"])
    assert [v["by"] for v in it["versions"]] == ["assistant", "owner"]
    assert {c["label"] for c in it["versions"][1]["changes"]} == {"Offer (EUR)", "Text"}
    assert it["edit"]["edited"] is True
    _, r = await approve(app, mcp)
    assert r.status_code == 200, r.text
    sent = editable.sends()[0][2]["args"]
    assert sent["offer"] == 40 and sent["text"] == "Could you do 40?" and sent["conversation_id"] == "c-1"
    assert "Offer (EUR), Text" in audit_text(mcp)


async def test_a_recipient_the_owner_types_in_is_allowed_for_this_request_only(web, editable):
    """protects=2d and the owner's comment of 2026-11-17: the list guards against the assistant, not the owner; a recipient typed
    in by hand saves, the service is asked about the new target, and approving sends there; fails_when=the save is refused, the
    card keeps the old listing, approving refuses, or the list itself changes; no_claim=the policy hook (connectors have none)."""
    app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    got = await call(mcp, "approval_request", **ASK)
    r = await put_fields(app, pending(mcp), {"conversation_id": "c-2"})
    assert r.status_code == 200 and "allowed for this request only" in r.json()["note"], r.text
    it = await item(app, got["outbox_id"])
    assert dict(it["rows"])["Listing"] == "Garden chairs"                     # describe ran again for the new target
    chat = next(f for f in it["edit"]["fields"] if f["name"] == "conversation_id")
    assert chat["recipient"] and chat["owner_allowed"]
    assert set(it["edited_rows"]) == {"Chat", "Listing"} and "changed" not in it   # marked on the card, without the service's warning
    _, r = await approve(app, mcp)
    assert r.status_code == 200, r.text
    assert editable.sends()[0][2]["args"]["conversation_id"] == "c-2"
    other = {**ASK, "params": {**ASK["params"], "conversation_id": "c-2", "text": "other"}}
    assert (await refused(mcp, **other))["reason"] == "not_allowed"          # the assistant still cannot reach c-2: the list is unchanged


async def test_the_assistants_recipient_keeps_the_list_after_the_owner_edits_something_else(web, editable):
    """protects=2d: only a recipient the owner introduced passes the list; one the assistant set is checked as before, so a list
    narrowed after the edit still holds it; fails_when=editing the offer turns the assistant's recipient into the owner's;
    no_claim=none beyond protects."""
    app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    await call(mcp, "approval_request", **OFFER)
    assert (await put_fields(app, pending(mcp), {"offer": 30})).status_code == 200
    q = pending(mcp)
    assert json.loads(q.raw).get("owner_allowed") is None
    reg = mcp._icloud_dashboard.sources["external"].registry      # the owner's list without c-1 now (a changed setting applies after a restart)
    reg._items["market.message.send"] = dataclasses.replace(reg.get("market.message.send"), allowed=lambda: ["c-2"])
    _, r = await approve(app, mcp)
    assert r.status_code != 200 and editable.sends() == []


@pytest.mark.parametrize("fields,status", [
    ({"conversation_title": "Something else"}, 422),        # the service fills it in
    ({"offer": -5}, 422),                                    # outside the schema
    ({}, 422),
])
async def test_a_change_the_form_does_not_offer_or_the_schema_does_not_take_is_refused(web, editable, fields, status):
    """protects=2d: only the fields the file marks editable change, and only to values the schema takes; fails_when=a readOnly
    field changes, a bad value is stored, or a refusal still makes a version; no_claim=none beyond protects."""
    app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    got = await call(mcp, "approval_request", **OFFER)
    q = pending(mcp)
    r = await put_fields(app, q, fields)
    assert r.status_code == status, r.text
    assert pending(mcp).sha256 == q.sha256 and "versions" not in await item(app, got["outbox_id"])


async def test_the_old_version_cannot_be_saved_over(web, editable):
    """protects=2d with 2c: a save names the version it was made on; fails_when=a stale form overwrites a newer version;
    no_claim=none beyond protects."""
    app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    await call(mcp, "approval_request", **OFFER)
    q = pending(mcp)
    assert (await put_fields(app, q, {"offer": 30})).status_code == 200
    assert (await put_fields(app, q, {"offer": 31})).status_code == 409


async def test_a_type_with_only_a_text_keeps_its_text_editor(web, market, tmp_path, monkeypatch):
    """protects=2d: without editable_fields nothing changes for an action (no form, the text route as before); fails_when=the
    form appears or the fields route accepts a change; no_claim=none beyond protects."""
    app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    got = await call(mcp, "approval_request", **OFFER)
    assert "fields" in (await item(app, got["outbox_id"]))["edit"]           # the example lets the offer change
    doc = copy.deepcopy(EXAMPLE)
    doc["actions"][0].pop("editable_fields")
    monkeypatch.setenv("GATE_ACTIONS_FILE", str(write_file(tmp_path, doc)))
    P._loaded.cache_clear()
    app, mcp, *_ = web(MARKET_SEND_ALLOWLIST="c-1")
    got = await call(mcp, "approval_request", **{**ASK, "request_id": "text-only"})
    it = await item(app, got["outbox_id"])
    assert "fields" not in it["edit"] and it["edit"]["allowed"]
    assert (await put_fields(app, pending(mcp), {"text": "x"})).status_code == 422


@pytest.mark.parametrize("change,pointer", [
    (lambda a: a.update(editable_fields=["conversation_title"]), "/actions/0"),        # readOnly: the service fills it in
    (lambda a: a.update(editable_fields=["nope"]), "/actions/0"),
    (lambda a: (a.update(editable_fields=["offer"]), a.__setitem__("rows", a["rows"][:1])), "/actions/0"),   # not shown on the card
])
def test_a_file_whose_editable_field_breaks_a_rule_is_refused(tmp_path, change, pointer):
    """protects=2d: an editable field is one the agent sets and the card shows, so every change is visible; fails_when=such a
    file loads; no_claim=none beyond protects."""
    doc = copy.deepcopy(EXAMPLE)
    change(doc["actions"][0])
    with pytest.raises(G.LoadError) as got:
        G.load(write_file(tmp_path, doc))
    assert got.value.pointer.startswith(pointer) and "editable field" in got.value.message
