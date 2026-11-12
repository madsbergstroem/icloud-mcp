"""The hub cut: a registered action type (actions.py) becomes an approval source (external.py) that the dashboard, approval_status
and approval_withdraw already know how to handle. A made-up type and a fake executor stand in for a connector; nothing here
reaches anything outside the process."""
import dataclasses
import hashlib
import json
import logging
import time
from pathlib import Path

import pytest
from mcp import Client
from mcp.types import Implementation

from icloud_mcp import actions as A
from icloud_mcp.outbox import Outbox
from icloud_mcp import push as P
from test_dashboard import api, client, signed_in, web  # noqa: F401  (web is a fixture)

WORDS = "Meet me at the north gate at nine"           # what the agent wrote; it must never reach the log
PEOPLE = ["anna@example.org"]

ECHO = A.ActionType(
    id="test.echo", title="Echo test", badge="Echo", approve_label="Send echo",
    params_schema={"type": "object", "additionalProperties": False, "required": ["to", "message"],
                   "properties": {"to": {"type": "string"}, "message": {"type": "string", "minLength": 1, "maxLength": 200},
                                  "times": {"type": "integer", "minimum": 1, "maximum": 3}}},
    rows=(("Reaches", "to"), ("Times", "times")), text_field="message",
    recipients=lambda p: [p["to"]], warnings=lambda p: ["The message mentions money."] if "money" in p["message"] else [])
PING = A.ActionType(
    id="test.ping", title="Ping", badge="Ping", approve_label="Ping now",
    params_schema={"type": "object", "additionalProperties": False, "properties": {"target": {"type": "string"}}},
    rows=(("Target", "target"),))


class FakeExecutor:
    def __init__(self):
        self.calls: list[A.ActionRequest] = []
        self.answer: object = A.Result("sent", "Echoed.")

    def execute(self, request):
        self.calls.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def hub(monkeypatch):
    """A registry of its own, so no test leaves a type behind for the next one."""
    reg = A.ActionRegistry()
    monkeypatch.setattr(A, "REGISTRY", reg)
    return reg


def launch(web, hub, *, allowed=PEOPLE, types=(ECHO, PING), **env):
    ex = FakeExecutor()
    for t in types:
        hub.register(t, ex, allowed)
    app, mcp, _p, _sent = web(**env)
    return app, mcp, mcp._icloud_dashboard.sources["external"], ex


def echo(src, text=WORDS, to="anna@example.org", **kw):
    return src.queue_external("test.echo", {"to": to, "message": text, "times": 2}, "Claude", **kw)


def audit_text(mcp):
    return mcp._icloud_dashboard.audit.path.read_text()


async def tool(mcp, name, **args):
    return json.loads((await mcp.call_tool(name, args)).content[0].text)


def url(item_id):
    return f"/dashboard/api/items/external:{item_id}"


# -- nothing registered, nothing changes -----------------------------------------------------------------------------------
async def test_without_a_registered_type_the_server_has_no_external_source(web, hub):
    app, mcp, *_ = web()
    assert "external" not in mcp._icloud_dashboard.sources and "external" not in mcp._icloud_outboxes
    assert not [t.name for t in mcp._tool_manager.list_tools() if "external" in t.name]
    assert not (Path(mcp._icloud_dashboard.s.data_dir) / "external-outbox.json").exists()
    async with client(app) as c:
        await signed_in(c)
        assert "external" not in (await c.get("/dashboard/api/state")).json()["kinds"]


async def test_a_registered_type_adds_the_source_and_no_tool(web, hub):
    app, mcp, src, _ = launch(web, hub)
    assert mcp._icloud_dashboard.sources["external"] is src and mcp._icloud_outboxes["external"] is src.box
    assert src.box.path.name == "external-outbox.json"
    assert not [t.name for t in mcp._tool_manager.list_tools() if "external" in t.name or "echo" in t.name]
    async with client(app) as c:
        await signed_in(c)
        kinds = (await c.get("/dashboard/api/state")).json()["kinds"]
        assert kinds["external:Echo"] == 0 and "external" not in kinds       # counted under the type's badge, not the raw kind


async def test_only_the_beeper_tool_calls_queue_external():
    root = Path(A.__file__).parent
    users = [str(p.relative_to(root)) for p in root.rglob("*.py") if p.name != "external.py" and "queue_external" in p.read_text()]
    assert sorted(users) == ["plugins/__init__.py", "plugins/beeper/gateway.py"]     # the contract names it; Beeper's sender calls it


# -- the type itself -------------------------------------------------------------------------------------------------------
def test_a_type_is_checked_when_it_is_made():
    schema = {"type": "object", "properties": {"a": {"type": "string"}, "n": {"type": "integer"}}}
    ok = dict(id="x.y", title="T", badge="B", approve_label="Go", params_schema=schema)
    A.ActionType(**ok)
    for bad, why in (({"id": "Bad Id"}, "lower-case"), ({"title": " "}, "title is empty"),
                     ({"params_schema": {"type": "wibble"}}, "not a valid JSON Schema"),
                     ({"params_schema": {"type": "array"}}, "an object"),
                     ({"rows": (("Shown", "missing"),)}, "not in the schema"),
                     ({"rows": (("To", "a"),)}, "reserved"), ({"rows": (("Conversation", "a"),)}, "reserved"),
                     ({"text_field": "n"}, "string property"), ({"text_field": "nope"}, "string property")):
        with pytest.raises(ValueError, match=why):
            A.ActionType(**{**ok, **bad})


def test_the_registry_refuses_twins_and_things_that_cannot_execute(hub):
    hub.register(ECHO, FakeExecutor(), PEOPLE)
    with pytest.raises(ValueError, match="already registered"):
        hub.register(ECHO, FakeExecutor(), PEOPLE)
    with pytest.raises(TypeError):
        hub.register(PING, object())
    assert [t.id for t in hub.types()] == ["test.echo"]
    with pytest.raises(ValueError):
        A.Result("done")


# -- queueing --------------------------------------------------------------------------------------------------------------
async def test_a_queued_request_is_the_canonical_json_and_carries_its_type(web, hub):
    _, mcp, src, _ = launch(web, hub)
    q = echo(src)
    want = json.dumps({"action_type": "test.echo", "params": {"message": WORDS, "times": 2, "to": "anna@example.org"}},
                      sort_keys=True, separators=(",", ":")).encode()
    assert q.raw == want and q.sha256 == hashlib.sha256(want).hexdigest()
    assert (q.recipients, q.action_type, q.requested_by, q.edited_from) == (PEOPLE, "test.echo", "Claude", None)
    assert src.pending() == [q]
    again = Outbox(mcp._icloud_dashboard.s.data_dir, 3600, 20, filename="external-outbox.json")
    assert again.pending()[0].action_type == "test.echo" and again.pending()[0].raw == want        # survives a restart


async def test_the_other_kinds_store_no_action_type_so_their_files_are_as_before(web):
    app, mcp, *_ = web()
    from test_dashboard import queue
    queue(mcp)
    stored = json.loads(mcp._icloud_dashboard.sources["mail"].mail.outbox.path.read_text())
    assert all("action_type" not in item for item in stored["items"])


async def test_parameters_are_checked_against_the_schema(web, hub):
    _, _, src, _ = launch(web, hub)
    for params, why in (({"to": "anna@example.org"}, "message"), ({"to": "anna@example.org", "message": ""}, "message"),
                        ({"to": "anna@example.org", "message": "x" * 201}, "message"),
                        ({"to": "anna@example.org", "message": "hi", "times": 9}, "times"),
                        ({"to": "anna@example.org", "message": "hi", "extra": 1}, "extra"),
                        ({"to": 5, "message": "hi"}, "to"), ("not an object", "object"), ({"to": "anna@example.org", "message": {1, 2}}, "JSON")):
        with pytest.raises(A.ExternalError) as e:
            src.queue_external("test.echo", params, "Claude")
        assert why in str(e.value), params
    with pytest.raises(A.ExternalError, match="Unknown action type"):
        src.queue_external("test.nothing", {}, "Claude")
    assert src.pending() == []


async def test_a_recipient_outside_the_owners_list_is_refused_and_nothing_is_queued(web, hub):
    _, _, src, _ = launch(web, hub)
    with pytest.raises(A.ExternalError, match="bob@example.org"):
        echo(src, to="bob@example.org")
    assert src.pending() == []
    assert echo(src, to="ANNA@example.org").recipients == ["ANNA@example.org"]        # the list is compared without case


async def test_a_type_that_names_people_and_has_no_list_cannot_queue_anything(web, hub):
    _, _, src, _ = launch(web, hub, allowed=())
    with pytest.raises(A.ExternalError, match="Not permitted"):
        echo(src)
    assert src.queue_external("test.ping", {"target": "box"}, "Claude").recipients == []        # it names nobody: nothing to allow


async def test_a_full_queue_refuses_with_the_owners_limit_named(web, hub):
    _, _, src, _ = launch(web, hub, OUTBOX_MAX="2")
    echo(src, "one"), echo(src, "two")
    with pytest.raises(A.ExternalError, match="OUTBOX_MAX=2"):
        echo(src, "three")
    assert len(src.pending()) == 2


async def test_the_same_key_gives_the_same_entry_and_a_different_request_under_it_is_refused(web, hub):
    _, mcp, src, _ = launch(web, hub)
    pushed = []
    src.box.on_new = lambda q: pushed.append(q.id)
    first = echo(src, key="job-1")
    assert echo(src, key="job-1").id == first.id and echo(src, "other words", key="job-1").id == first.id      # words only: the owner may have edited them
    with pytest.raises(A.ExternalConflict):
        src.queue_external("test.echo", {"to": "anna@example.org", "message": WORDS, "times": 3}, "Claude", key="job-1")
    with pytest.raises(A.ExternalConflict):
        src.queue_external("test.ping", {"target": "x"}, "Claude", key="job-1")
    assert echo(src).id == first.id                                                            # the same bytes need no key
    assert len(src.pending()) == 1 and pushed == [first.id]
    for bad in ("", "   ", "k" * 201):
        with pytest.raises(A.ExternalError, match="key"):
            echo(src, key=bad)


# -- the dashboard ---------------------------------------------------------------------------------------------------------
async def test_the_list_and_the_card_come_from_the_type(web, hub, monkeypatch):
    app, mcp, src, _ = launch(web, hub)
    notes = []
    monkeypatch.setattr(type(mcp._icloud_dashboard.push), "notify", lambda self, kind, key="": notes.append((kind, key)))
    q = echo(src, "Please send the money")
    assert notes == [("external", f"external:{q.id}")]
    assert P.TEXTS["external"] == ("Approval needed", "An action is waiting for your approval.")
    async with client(app) as c:
        await signed_in(c)
        today = (await c.get("/dashboard/api/today")).json()["waiting"]
        assert today == [{"key": f"external:{q.id}", "kind": "external", "kind_label": "Echo", "badge": "Echo", "title": "Echo test", "to": "anna@example.org",
                          "by": "Claude", "created_at": q.created_at, "expires_at": q.expires_at, "warning": True, "blocked": False,
                          "group": False, "action_type": "test.echo"}]
        assert (await c.get("/dashboard/api/state")).json()["kinds"]["external:Echo"] == 1
        it = (await c.get(url(q.id))).json()
        assert it["rows"] == [["Reaches", "anna@example.org"], ["Times", "2"]] and it["text"] == "Please send the money"
        assert it["approve_label"] == "Send echo" and it["warnings"] == ["The message mentions money."] and it["needs_override"] is False
        assert it["edit"] == {"allowed": True, "reason": "", "text": "Please send the money", "limit": 200, "edited": False, "original": None}
        assert it["account"] == "" and it["files"] == [] and it["missing"] == [] and it["action_type"] == "test.echo"


async def test_a_card_whose_own_functions_fail_still_opens(web, hub):
    broken = dataclasses.replace(PING, id="test.broken", warnings=lambda p: 1 / 0, recipients=lambda p: 1 / 0)
    app, mcp, src, _ = launch(web, hub, types=(broken,))
    q = src.box.add(A.canonical("test.broken", {"target": "x"}), [], None, action_type="test.broken")
    async with client(app) as c:
        await signed_in(c)
        it = (await c.get(url(q.id))).json()
        assert it["warnings"] == ["The cautions for this request could not be worked out."] and it["title"] == "Ping"


async def test_editing_the_text_changes_the_hash_and_bringing_it_back_restores_it(web, hub):
    app, mcp, src, _ = launch(web, hub)
    q = echo(src)
    async with client(app) as c:
        token = await signed_in(c)
        r = await c.put(url(q.id) + "/text", json={"sha": q.sha256, "text": "Meet at ten, ÜBER-café"}, headers=api(token))
        assert r.status_code == 200 and r.json()["changed"] is True and r.json()["sha"] != q.sha256
        new = src.pending()[0]
        want = A.canonical("test.echo", {"to": "anna@example.org", "message": "Meet at ten, ÜBER-café", "times": 2})
        assert new.raw == want and new.sha256 == hashlib.sha256(want).hexdigest() == r.json()["sha"]
        assert (new.id, new.recipients, new.action_type, new.requested_by, new.expires_at) == (q.id, q.recipients, "test.echo", "Claude", q.expires_at)
        assert new.edited_from == WORDS
        assert (await tool(mcp, "approval_status", id=q.id)) == {"id": q.id, "kind": "external", "status": "waiting", "edited": True,
                                                                 "at": (await tool(mcp, "approval_status", id=q.id))["at"], "action_type": "test.echo"}
        stale = await c.put(url(q.id) + "/text", json={"sha": q.sha256, "text": "late"}, headers=api(token))
        assert stale.status_code == 409
        r = await c.put(url(q.id) + "/text", json={"sha": new.sha256, "restore": True}, headers=api(token))
        assert r.status_code == 200 and r.json()["sha"] == q.sha256 and src.pending()[0].raw == q.raw        # the same bytes, so the same hash
        assert (await tool(mcp, "approval_status", id=q.id))["edited"] is False
    log = [json.loads(x) for x in audit_text(mcp).splitlines() if "approval.edit" in x]
    assert [e["action"] for e in log] == ["approval.edited", "approval.edit_undone"] and all(e["details"]["action_type"] == "test.echo" for e in log)
    assert "north gate" not in audit_text(mcp) and "ÜBER" not in audit_text(mcp)


async def test_an_edit_that_breaks_the_schema_or_has_no_text_to_edit_is_refused(web, hub):
    app, mcp, src, _ = launch(web, hub)
    q = echo(src)
    ping = src.queue_external("test.ping", {"target": "box"}, "Claude")
    async with client(app) as c:
        token = await signed_in(c)
        for bad, why in (("x" * 201, "does not fit"), ("", "does not fit"), (None, "missing")):
            r = await c.put(url(q.id) + "/text", json={"sha": q.sha256, "text": bad}, headers=api(token))
            assert r.status_code == 422 and why in r.json()["error"] and src.pending()[0].sha256 == q.sha256, bad
        it = (await c.get(url(ping.id))).json()
        assert it["edit"]["allowed"] is False and "no text to edit" in it["edit"]["reason"]
        r = await c.put(url(ping.id) + "/text", json={"sha": ping.sha256, "text": "x"}, headers=api(token))
        assert r.status_code == 422 and "no text to edit" in r.json()["error"]


async def test_approve_carries_the_edited_request_out_once_and_logs_no_text(web, hub):
    app, mcp, src, ex = launch(web, hub)
    q = echo(src)
    async with client(app) as c:
        token = await signed_in(c)
        sha = (await c.put(url(q.id) + "/text", json={"sha": q.sha256, "text": "Edited words"}, headers=api(token))).json()["sha"]
        r = await c.post(url(q.id) + "/approve", json={"sha": sha}, headers=api(token))
        assert r.status_code == 200 and r.json() == {"ok": True, "note": "Echoed."}
        assert (await c.post(url(q.id) + "/approve", json={"sha": sha}, headers=api(token))).status_code == 409
    assert len(ex.calls) == 1 and src.pending() == []
    call = ex.calls[0]
    assert (call.id, call.action_type, call.requested_by, call.key) == (q.id, "test.echo", "Claude", None)
    assert dict(call.params) == {"to": "anna@example.org", "message": "Edited words", "times": 2}
    assert await tool(mcp, "approval_status", id=q.id) == {"id": q.id, "kind": "external", "status": "sent", "edited": True,
                                                           "at": (await tool(mcp, "approval_status", id=q.id))["at"], "action_type": "test.echo"}
    (sent,) = [json.loads(x) for x in audit_text(mcp).splitlines() if '"approval.sent"' in x]
    assert sent["summary"] == "Approved echo “Echo test”" and sent["details"] == {
        "kind": "external", "outbox_id": q.id, "delivery": "sent", "action_type": "test.echo"}       # no recipients, no words
    assert "anna@example.org" not in audit_text(mcp) and "Edited words" not in audit_text(mcp)


async def test_failed_means_it_never_arrived_so_the_request_waits_again(web, hub):
    app, mcp, src, ex = launch(web, hub)
    q = echo(src)
    ex.answer = A.Result("failed", "The connector could not reach the other side.\nNothing was sent.")
    async with client(app) as c:
        token = await signed_in(c)
        r = await c.post(url(q.id) + "/approve", json={"sha": q.sha256}, headers=api(token))
        assert r.status_code == 502 and r.json()["error"] == "Not sent (still waiting): The connector could not reach the other side. Nothing was sent."
        assert src.pending() == [q] and (await tool(mcp, "approval_status", id=q.id))["status"] == "waiting"
        ex.answer = A.Result("sent", "Echoed on the second try.")
        r = await c.post(url(q.id) + "/approve", json={"sha": q.sha256}, headers=api(token))
        assert r.status_code == 200 and r.json()["note"] == "Echoed on the second try."
    assert len(ex.calls) == 2 and (await tool(mcp, "approval_status", id=q.id))["status"] == "sent"
    failed = [json.loads(x) for x in audit_text(mcp).splitlines() if '"approval.failed"' in x]
    assert len(failed) == 1 and failed[0]["ok"] is False and failed[0]["details"]["action_type"] == "test.echo"


@pytest.mark.parametrize("how", ["unclear", "raises", "not a result"])
async def test_unclear_is_never_repeated_and_reads_unclear(web, hub, how, caplog):
    app, mcp, src, ex = launch(web, hub)
    q = echo(src)
    ex.answer = {"unclear": A.Result("unclear", "The other side did not answer in time."), "raises": RuntimeError("the north gate secret"),
                 "not a result": "sent"}[how]
    async with client(app) as c:
        token = await signed_in(c)
        with caplog.at_level(logging.INFO):
            r = await c.post(url(q.id) + "/approve", json={"sha": q.sha256}, headers=api(token))
        assert r.status_code == 200 and r.json()["state"] == "unclear" and r.json()["note"]
        assert src.pending() == []                                                            # not put back
        assert (await c.post(url(q.id) + "/approve", json={"sha": q.sha256}, headers=api(token))).status_code == 409
        shown = (await c.get("/dashboard/api/activity?filter=approvals")).json()["days"][0]["entries"]
        assert [(e["tag"], e["note"]) for e in shown if e["tag"] == "Unclear"] == [
            ("Unclear", "It may or may not have gone through. Nothing is sent again.")]          # not the iMessage wording
    assert len(ex.calls) == 1
    got = await tool(mcp, "approval_status", id=q.id)
    assert (got["status"], got["kind"], got["action_type"]) == ("unclear", "external", "test.echo")
    (line,) = [json.loads(x) for x in audit_text(mcp).splitlines() if '"approval.sent"' in x]       # the way an unconfirmed iMessage is logged
    assert line["details"]["delivery"] == "unconfirmed" and line["details"]["action_type"] == "test.echo"
    assert "north gate" not in caplog.text and "north gate" not in audit_text(mcp)


async def test_discard_runs_nothing_and_says_how_it_ended(web, hub):
    app, mcp, src, ex = launch(web, hub)
    q = echo(src)
    async with client(app) as c:
        token = await signed_in(c)
        r = await c.post(url(q.id) + "/discard", json={"sha": q.sha256}, headers=api(token))
        assert r.status_code == 200 and r.json()["note"] == "Discarded. Nothing was sent."
    assert ex.calls == [] and src.pending() == []
    assert (await tool(mcp, "approval_status", id=q.id))["status"] == "discarded"
    assert (await tool(mcp, "approval_status", id=q.id))["action_type"] == "test.echo"


async def test_a_request_that_runs_out_of_time_reads_expired_with_its_type(web, hub):
    _, mcp, src, ex = launch(web, hub)
    q = echo(src)
    src.box._items[q.id] = dataclasses.replace(src.box._items[q.id], expires_at=time.time() - 30)
    got = await tool(mcp, "approval_status", id=q.id)
    assert (got["status"], got["kind"], got["action_type"]) == ("expired", "external", "test.echo") and ex.calls == []
    since = await tool(mcp, "approval_status", since="0" * 64)
    assert [(i["id"], i["status"], i["action_type"]) for i in since["items"]] == [(q.id, "expired", "test.echo")]


async def test_the_agent_that_queued_it_takes_it_back_and_only_that_agent(web, hub):
    app, mcp, src, ex = launch(web, hub)
    q = echo(src)                                                                            # queued as "Claude"
    async with Client(mcp, client_info=Implementation(name="Someone else", version="1.0"), mode="legacy") as c:
        other = await c.call_tool("approval_withdraw", {"id": q.id})
    assert other.is_error and "Another client queued this request" in other.content[0].text and src.pending() == [q]
    async with Client(mcp, client_info=Implementation(name="Claude", version="1.0"), mode="legacy") as c:
        got = json.loads((await c.call_tool("approval_withdraw", {"id": q.id, "reason": "mistake"})).content[0].text)
    assert got == {"id": q.id, "withdrawn": True, "status": "withdrawn"} and src.pending() == [] and ex.calls == []
    status = await tool(mcp, "approval_status", id=q.id)
    assert (status["status"], status["action_type"]) == ("withdrawn", "test.echo")
    (line,) = [json.loads(x) for x in audit_text(mcp).splitlines() if '"approval.withdrawn"' in x]
    assert line["details"]["kind"] == "external" and line["details"]["action_type"] == "test.echo" and line["details"]["reason"] == "mistake"
    assert "north gate" not in audit_text(mcp)


async def test_the_owners_list_is_checked_again_when_approving(web, hub):
    people = list(PEOPLE)
    app, mcp, src, ex = launch(web, hub, allowed=lambda: people)
    q = echo(src)
    people.clear()                                                                           # the owner took the name off the list meanwhile
    async with client(app) as c:
        token = await signed_in(c)
        r = await c.post(url(q.id) + "/approve", json={"sha": q.sha256}, headers=api(token))
        assert r.status_code == 502 and "Not permitted" in r.json()["error"]
    assert ex.calls == [] and src.pending() == [q]


async def test_a_type_that_is_gone_cannot_be_approved_but_can_be_discarded(web, hub):
    app, mcp, src, ex = launch(web, hub)
    q = echo(src)
    del src.registry._items["test.echo"]                                                     # e.g. a connector was switched off and the server restarted
    async with client(app) as c:
        token = await signed_in(c)
        it = (await c.get(url(q.id))).json()
        assert it["title"] == "Unknown action test.echo" and it["approve_label"] == "Approve" and it["edit"]["allowed"] is False
        r = await c.post(url(q.id) + "/approve", json={"sha": q.sha256}, headers=api(token))
        assert r.status_code == 502 and "not available" in r.json()["error"] and src.pending() == [q]
        r = await c.post(url(q.id) + "/discard", json={"sha": q.sha256}, headers=api(token))
        assert r.status_code == 200 and src.pending() == []
    assert ex.calls == []


async def test_a_request_edited_between_the_look_and_the_click_is_not_carried_out(web, hub):
    app, mcp, src, ex = launch(web, hub)
    q = echo(src)
    shown = src.pending()[0]                                                                 # what the owner's page showed
    src.box.replace(q.id, q.sha256, A.canonical("test.echo", {"to": "anna@example.org", "message": "Changed", "times": 2}))
    with pytest.raises(A.ExternalError, match="changed just now"):
        src.release(shown)
    assert ex.calls == [] and src.pending()[0].raw != q.raw                                  # still waiting, as it is now
