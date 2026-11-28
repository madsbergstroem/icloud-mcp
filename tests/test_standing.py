"""Standing rules (docs/SENDGATE.md, package 10, "The standing release" and "The request for a rule"): requests of one type to one
person go out without waiting for the owner, once the owner approved a gate.trust request for it with a fresh sign-in. A made-up
type of the server's own (test.note) stands for the calendar and mail types; the dashboard tests use the real server with mail.
Every name and address is made up.

Each test says what it protects, the wrong result it rejects and what a green run does not prove."""
import sqlite3
import threading
import time

import pytest

from icloud_mcp import dashboard as D
from icloud_mcp.gate import actions as A
from icloud_mcp.gate import standing as ST
from icloud_mcp.gate import trust as TR
from icloud_mcp.gate.external import ExternalApprovals
from icloud_mcp.gate.requests import Requests
from icloud_mcp.gate.store import Store
from test_dashboard import api, client, signed_in, web  # noqa: F401  (web is a fixture)
from test_external_approvals import url
from test_plugin_actions import call, parcel, refused  # noqa: F401  (parcel is a fixture)

JANA, TOM, SAM = "jana@example.org", "tom@example.net", "sam@example.org"

NOTE = A.ActionType(
    id="test.note", title="Note", badge="Note", approve_label="Send",
    params_schema={"type": "object", "additionalProperties": False, "required": ["to", "text"],
                   "properties": {"to": {"type": "string"}, "text": {"type": "string"}, "group": {"type": "boolean"},
                                  "nobody": {"type": "boolean"}}},
    text_field="text", recipients=lambda p: [x.strip() for x in p["to"].split(",") if x.strip()],
    confirm_twice=lambda p: "This goes to a group." if p.get("group") else None,
    standing_identity=lambda p: [] if p.get("nobody") else ([x.strip() for x in p["to"].split(",")] if "@" in p["to"] else None))
OTHER = A.ActionType(id="test.other", title="Other", badge="Other", approve_label="Go",
                     params_schema={"type": "object", "properties": {"to": {"type": "string"}}}, recipients=lambda p: [p.get("to", "")])


class Exec:
    def __init__(self, status="sent"):
        self.status, self.calls = status, []

    def execute(self, request):
        self.calls.append(request)
        return A.Result(self.status, "Done." if self.status == "sent" else "")


class Held:
    """A hold that blocks approving while `on` is set."""
    on = False

    def __call__(self, approving):
        return A.Hold("blocked", "offline", "The service is offline.") if self.on else None


@pytest.fixture
def gate(tmp_path):
    reg = A.ActionRegistry()
    ex, hold, allowed = Exec(), Held(), {"*"}
    reg.register(NOTE, ex, lambda: allowed, builtin=True, hold=hold)
    reg.register(OTHER, Exec(), ["*"], builtin=True)
    ext = ExternalApprovals(Store(str(tmp_path), "external", 3600, 20), reg)
    ext.standing = ST.Standing(ext.box, daily_limit=3)
    pushes, logged = [], []
    ext.box.on_new = pushes.append
    ext.on_standing = lambda q, got: logged.append((q.id, got.status, [r.id for r in got.rules]))
    return ext, ex, pushes, logged, hold, allowed


def ask(ext, to=JANA, text="See you at noon. Sam", **kw):
    return ext.submit("test.note", {"to": to, "text": text, **kw}, None)


def state(ext, got):
    return ext.box.state_of(got.q.id)[0]


# -- the standing release table --------------------------------------------------------------------------------------------------
def test_a_request_every_rule_covers_goes_out_at_once_without_a_push(gate):
    """protects=row 1: released by the server, the agent hears the outcome, the executor runs once, no push (D10-6), one use;
    fails_when=it waits, runs twice, pushes, or no use is counted; no_claim=a real service behind the type."""
    ext, ex, pushes, logged, *_ = gate
    rule = ext.standing.add("test.note", "Jana@Example.org", 30)
    got = ask(ext)
    assert got.status == "sent" and not got.waits and got.new and [r.id for r in got.rules] == [rule.id]
    assert len(ex.calls) == 1 and pushes == [] and state(ext, got) == "sent"
    assert logged == [(got.q.id, "sent", [rule.id])]
    assert ext.standing.live()[0]["uses"] == 1


def test_one_person_without_a_rule_makes_the_request_wait_and_push_once(gate):
    """protects=row "one recipient without a rule"; fails_when=it goes out for the covered half, or waits without a push;
    no_claim=none beyond protects."""
    ext, ex, pushes, logged, *_ = gate
    ext.standing.add("test.note", JANA, 30)
    got = ask(ext, to=f"{JANA}, {TOM}")
    assert got.waits and got.reason == "no_rule" and ex.calls == [] and [q.id for q in pushes] == [got.q.id] and logged == []
    assert state(ext, got) == "waiting"


@pytest.mark.parametrize("kw", [{"to": "Jana"}, {"nobody": True}])
def test_a_type_that_cannot_say_who_it_reaches_waits_whatever_rules_exist(gate, kw):
    """protects=row "standing_identity answers None or []"; fails_when=a rule releases it; no_claim=none beyond protects."""
    ext, ex, pushes, *_ = gate
    ext.standing.add("test.note", JANA, 30)
    got = ext.submit("test.note", {"to": JANA, "text": "Hi", **kw} if "nobody" in kw else {"text": "Hi", **kw}, None)
    assert got.waits and ex.calls == [] and len(pushes) == 1


def test_a_failed_release_waits_with_the_reason_and_asking_again_runs_nothing_more(gate):
    """protects=criterion 7 and row "the release answers failed"; fails_when=asking again with the same bytes tries the rule
    again, or the failure is not pushed; no_claim=that the use of a failed attempt is given back (it counts, by design)."""
    ext, ex, pushes, *_ = gate
    ex.status = "failed"
    ext.standing.add("test.note", JANA, 30)
    got = ask(ext)
    assert got.waits and got.reason == "failed" and len(ex.calls) == 1 and len(pushes) == 1 and state(ext, got) == "waiting"
    again = ask(ext)
    assert again.q.id == got.q.id and not again.new and again.waits and len(ex.calls) == 1 and len(pushes) == 1


def test_a_second_question_and_a_hold_make_it_wait(gate):
    """protects=rows "confirm_twice returns a reason" and "no hold"; fails_when=a rule answers the second question or passes a
    hold; no_claim=none beyond protects."""
    ext, ex, pushes, _logged, hold, _ = gate
    ext.standing.add("test.note", JANA, 30)
    assert ask(ext, group=True).reason == "confirm"
    hold.on = True
    got = ask(ext, text="Another note")
    assert got.waits and got.reason == "held" and ex.calls == [] and len(pushes) == 2
    assert ext.standing.live()[0]["uses"] == 0                   # nothing was claimed, so nothing was used


def test_the_daily_limit_an_expired_and_a_removed_rule_leave_it_waiting(gate):
    """protects=criterion 4; fails_when=a fourth request in 24 hours goes out, or an expired or removed rule still counts;
    no_claim=the 24 hours of a real clock (the use rows are dated by the test)."""
    ext, ex, *_ = gate
    rule = ext.standing.add("test.note", JANA, 30)
    assert [ask(ext, text=f"Note {i}").status for i in range(4)] == ["sent", "sent", "sent", "waiting"]
    assert ask(ext, text="Note 9").reason == "limit"
    with ext.box._lock:
        ext.box._db.execute("UPDATE standing_use SET at = at - ?", (ST.DAY + 1,))
    assert ask(ext, text="A day later").status == "sent"
    with ext.box._lock:
        ext.box._db.execute("UPDATE standing SET expires_at = ? WHERE id = ?", (time.time() - 1, rule.id))
    assert ask(ext, text="Expired").reason == "no_rule"
    other = ext.standing.add("test.note", TOM, 30)
    assert ext.standing.remove(other.id).id == other.id and ext.standing.remove(other.id) is None
    assert ask(ext, to=TOM, text="Removed").reason == "no_rule"
    assert len(ex.calls) == 4


def test_an_unclear_release_is_unclear_and_never_repeated(gate):
    """protects=the executor's three answers through a rule: unclear stays unclear; fails_when=it is put back or retried;
    no_claim=none beyond protects."""
    ext, ex, pushes, logged, *_ = gate
    ex.status = "unclear"
    ext.standing.add("test.note", JANA, 30)
    got = ask(ext)
    assert got.status == "unclear" and state(ext, got) == "unclear" and len(ex.calls) == 1 and pushes == [] and logged[0][1] == "unclear"
    assert len([c for c in ex.calls if c.id == got.q.id]) == 1


def test_a_list_that_no_longer_allows_the_person_ends_it_at_the_release(gate):
    """protects=the release runs the owner's list again: a rule never widens who may be reached; fails_when=it goes out;
    no_claim=none beyond protects."""
    ext, ex, pushes, _l, _h, allowed = gate
    ext.standing.add("test.note", JANA, 30)
    allowed.clear()
    allowed.add(TOM)
    with pytest.raises(A.ExternalError):                         # refused at queueing: nothing waits, nothing goes out
        ask(ext)
    assert ex.calls == [] and pushes == []


def test_two_requests_racing_for_the_last_use_release_exactly_one(tmp_path):
    """protects=criterion 8: rule, use and claim in one transaction; fails_when=both go out; no_claim=two processes (one process
    per DATA_DIR holds the lock)."""
    reg, ex = A.ActionRegistry(), Exec()
    reg.register(NOTE, ex, ["*"], builtin=True)
    ext = ExternalApprovals(Store(str(tmp_path), "external", 3600, 20), reg)
    ext.standing = ST.Standing(ext.box, daily_limit=1)
    ext.standing.add("test.note", JANA, 30)
    start, out = threading.Barrier(2), []

    def one(i):
        start.wait()
        out.append(ask(ext, text=f"Race {i}").status)
    threads = [threading.Thread(target=one, args=(i,)) for i in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(out) == ["sent", "waiting"] and len(ex.calls) == 1


def test_a_rule_removed_before_the_claim_releases_nothing(gate, monkeypatch):
    """protects=criterion 8, second half: the rule is read inside the claim's transaction; fails_when=a rule seen earlier still
    releases it; no_claim=none beyond protects."""
    ext, ex, *_ = gate
    rule = ext.standing.add("test.note", JANA, 30)
    real = ext.standing.claim

    def late(item_id, type_id, people):
        ext.standing.remove(rule.id)                             # the owner removes it while the request is being checked
        return real(item_id, type_id, people)
    monkeypatch.setattr(ext.standing, "claim", late)
    got = ask(ext)
    assert got.waits and got.reason == "no_rule" and ex.calls == []


def test_the_version_before_package_10_starts_on_the_same_data_and_every_request_waits(gate, tmp_path):
    """protects=criterion 6: the tables are new beside the store's, and without the rules every request waits; fails_when=the
    store refuses the file or a request goes out; no_claim=an actual install of the older release."""
    ext, *_ = gate
    ext.standing.add("test.note", JANA, 30)
    ext.box._db.close()
    reg, ex = A.ActionRegistry(), Exec()
    reg.register(NOTE, ex, ["*"], builtin=True)
    older = ExternalApprovals(Store(str(tmp_path), "external", 3600, 20), reg)       # no standing: as before package 10
    got = older.submit("test.note", {"to": JANA, "text": "After the rollback"}, None)
    assert got.waits and ex.calls == []
    assert sqlite3.connect(str(tmp_path / "approvals.db")).execute("SELECT COUNT(*) FROM standing").fetchone()[0] == 1


@pytest.mark.parametrize("person, want", [(" Jana@Example.ORG ", JANA), ("@example.org", None), ("*", None), ("", None),
                                          ("jana@example.org, tom@example.net", None), ("Jana", None), ("jana@localhost", None)])
def test_a_rule_names_one_person_by_address(person, want):
    """protects=D10-2, exact match only; fails_when=a domain, a wildcard or several pass; no_claim=deliverability of the address."""
    assert ST.identity(person) == want


def test_a_new_rule_replaces_the_older_one_and_days_are_bounded(gate):
    """protects=one rule per type and person, D10-3; fails_when=two live rules, or 0 or 366 days are taken; no_claim=none."""
    ext, *_ = gate
    first = ext.standing.add("test.note", JANA, 30)
    second = ext.standing.add("test.note", JANA, 90)
    assert [r["id"] for r in ext.standing.live()] == [second.id] and ext.standing.get(first.id).revoked_at is not None
    for days in (0, 366):
        with pytest.raises(ValueError):
            ext.standing.add("test.note", TOM, days)


# -- gate.trust ------------------------------------------------------------------------------------------------------------------
def _trust(gate):
    ext, *_ = gate
    reg = ext.registry
    reg.register(A.ActionType(id="parcel.book", title="Booking", badge="Booking", approve_label="Book",
                              params_schema={"type": "object", "properties": {}}), Exec(), ["*"])
    tr = TR.Trust(reg)
    assert TR.register(reg, tr)
    tr.standing = ext.standing
    return ext, tr


@pytest.mark.parametrize("params, says", [
    ({"action_type": "test.unknown"}, "cannot go out without approval"),
    ({"action_type": "gate.trust"}, "cannot go out without approval"),
    ({"action_type": "test.other"}, "cannot go out without approval"),       # registered, but has not opted in
    ({"action_type": "parcel.book"}, "cannot go out without approval"),      # a plugin's type never opts in
    ({"recipient": "@example.org"}, "one person"),
    ({"recipient": "*"}, "one person"),
    ({"recipient": SAM}, "not on your list"),
])
def test_a_rule_that_cannot_be_asked_for_is_refused_and_nothing_queued(gate, params, says):
    """protects=criterion 2; fails_when=it is queued or refused with another code; no_claim=none beyond protects."""
    ext, tr = _trust(gate)
    allowed = gate[5]
    allowed.clear()
    allowed.update({JANA, TOM})
    req = Requests(ext, lambda kind, i: f"https://mcp.example.com/{kind}/{i}")
    p = {"action_type": "test.note", "recipient": JANA, "reason": "Jana and I plan lunch every week.", **params}
    before = len(ext.box.pending())
    with pytest.raises(Exception) as e:
        req.request("gate.trust", p, None)
    assert '"reason": "policy"' in str(e.value) and says in str(e.value)
    assert len(ext.box.pending()) == before


def test_a_rule_is_asked_for_shown_from_its_fields_and_written_when_approved(gate):
    """protects=the card rows and what approving does; fails_when=the card shows the type's id, the rule is not written, or a
    request to that person still waits afterwards; no_claim=the step-up sign-in (tested on the dashboard below)."""
    ext, tr = _trust(gate)
    got = ext.submit("gate.trust", tr.prepare({"action_type": "test.note", "recipient": "Jana@example.org", "reason": "Weekly lunch."}), None)
    assert got.waits
    card = ext.describe(got.q)
    assert card["title"] == "Allow without approval" and ("Action", "Note") in card["rows"] and ("Person", JANA) in card["rows"]
    assert [k for k, _ in card["rows"]] == ["Action", "Person", "Until"]
    note = ext.release(got.q)
    assert str(note).startswith("Allowed until") and [r["recipient"] for r in ext.standing.live()] == [JANA]
    assert ask(ext).status == "sent"
    assert TR.action_type(tr.types()).step_up is True


def test_a_rule_without_its_store_fails_and_waits(gate):
    """protects=the executor never answers unclear; fails_when=it ends sent without a row; no_claim=none beyond protects."""
    ext, tr = _trust(gate)
    tr.standing = None
    got = ext.submit("gate.trust", tr.prepare({"action_type": "test.note", "recipient": JANA, "reason": "Lunch."}), None)
    with pytest.raises(A.ExternalError):
        ext.release(got.q)
    assert state(ext, got) == "waiting"


def test_only_the_servers_own_types_may_say_who_they_reach():
    """protects=a connector cannot opt in; fails_when=a plugin type with standing_identity registers; no_claim=none."""
    reg = A.ActionRegistry()
    with pytest.raises(ValueError, match="standing_identity"):
        reg.register(NOTE, Exec(), ["*"])


# -- on the dashboard, with the real server and mail -------------------------------------------------------------------------------
async def test_approving_a_rule_needs_a_fresh_sign_in_then_mail_to_that_person_goes_out_logged_by_the_rule(web, parcel, monkeypatch):
    """protects=criteria 3, 5 and 9 for approval_request and mail: an old sign-in is refused and the request keeps waiting; a
    fresh one writes the rule; a mail to the person then goes out at once, without a push, answered as sent and logged with the
    rule as the actor; removing the rule makes the next mail wait; fails_when=any of these differs; no_claim=SMTP delivery (faked)."""
    app, mcp, _p, sent = web(PARCEL_DEPOTS="d-17")
    d = mcp._icloud_dashboard
    pushes = []
    monkeypatch.setattr(d.push, "notify", lambda kind, key="": pushes.append(key))
    got = await call(mcp, "approval_request", action="gate.trust",
                     params={"action_type": "mail.send", "recipient": JANA, "reason": "Jana and I write about lunch every week."})
    assert got["status"] == "queued_for_owner_approval" and len(pushes) == 1
    async with client(app) as c:
        token = await signed_in(c)
        it = (await c.get(url(got["outbox_id"]))).json()
        assert it["title"] == "Allow without approval" and ["Action", "Email"] in it["rows"] and ["Person", JANA] in it["rows"]
        fresh = D._STEP_UP
        monkeypatch.setattr(D, "_STEP_UP", -1)               # every sign-in counts as old
        r = await c.post(url(got["outbox_id"]) + "/approve", json={"sha": it["sha"]}, headers=api(token))
        assert r.status_code == 403 and r.json().get("confirm") is True
        assert (await call(mcp, "approval_status", id=got["outbox_id"]))["status"] == "waiting"
        monkeypatch.setattr(D, "_STEP_UP", fresh)
        r = await c.post(url(got["outbox_id"]) + "/approve", json={"sha": it["sha"]}, headers=api(token))
        assert r.status_code == 200, r.text
        rules = (await c.get("/dashboard/api/standing")).json()["rules"]
        assert [(x["recipient"], x["title"], x["uses"]) for x in rules] == [(JANA, "Email", 0)]

        mailed = await call(mcp, "mail_send_message", to=[JANA], subject="Lunch on Friday", body="Hi Jana, noon as usual? Sam")
        assert mailed["status"] == "sent" and mailed["sent"] is True and mailed["without_approval"] is True
        assert len(sent) == 1 and len(pushes) == 1                # no push for the mail that went out
        acts = (await c.get("/dashboard/api/activity?days=1&action=approvals")).json()
        line = next(e for day in acts["days"] for e in day["entries"] if e["action"] == "Without approval")
        assert line["actor"] == "Your rule" and JANA in line["note"] and line["target"].startswith("Email")
        assert any(k == "Rule" for k, _ in line["details"])

        r = await c.post(f"/dashboard/api/standing/{rules[0]['id']}/remove", json={}, headers=api(token))
        assert r.status_code == 200 and (await c.get("/dashboard/api/standing")).json()["rules"] == []
    again = await call(mcp, "mail_send_message", to=[JANA], subject="Lunch next week", body="Hi Jana, next week too? Sam")
    assert again["status"] == "queued_for_owner_approval" and len(sent) == 1 and len(pushes) == 2


async def test_a_mail_with_an_attachment_always_waits(web, parcel):
    """protects=the owner's word on the mock of package 6: a mail with a file never goes without a look; fails_when=a rule
    releases it; no_claim=none beyond protects."""
    app, mcp, _p, sent = web(PARCEL_DEPOTS="d-17")
    ext = mcp._icloud_dashboard.sources["external"]
    ext.standing.add("mail.send", JANA, 30)
    got = await call(mcp, "mail_send_message", to=[JANA], subject="Menu", body="See the file. Sam", attachments=[{"filename": "menu.txt", "content_base64": "U291cCwgYnJlYWQ=", "content_type": "text/plain"}])
    assert got["status"].startswith("queued") and sent == []
