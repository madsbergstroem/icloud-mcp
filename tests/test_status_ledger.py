"""The status ledger (docs/SENDGATE.md, package 9a): one immutable sequence of status events in approvals.db, written in the
store's own transaction, read by approval_status for `id` and `since`, whether the activity log is on or off. Every request here
is made up. Each test says what it protects, the wrong result it rejects and what a green run does not prove.

tests/fixtures/status_ledger_golden.json holds the answers approval_status gave before 9a (commit 1f42796) for the scenario below,
with a fixed clock and fixed ids. It was written by running this file's golden test against that commit's src/ with
UPDATE_GOLDEN=1; run it the same way to write it again."""
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import types
from pathlib import Path

import pytest

from icloud_mcp.gate import audit as audit_mod
from icloud_mcp.gate import status as status_mod
from icloud_mcp.gate import store as S
from icloud_mcp.gate.audit import AuditLog
from icloud_mcp.gate.status import ApprovalLedger

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).parent / "fixtures" / "status_ledger_golden.json"
BEFORE = "1f42796"                       # the last commit before package 9a
GENESIS = "0" * 64
EVENT_FIELDS = {"seq", "request_id", "kind", "action_type", "status", "reason", "edited", "at", "reconciled"}


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def time(self):
        return self.now

    def tick(self, seconds=5.0):
        self.now += seconds


def fixed(monkeypatch):
    """A clock the store and the log share, and ids in the order requests are queued."""
    clock, n = Clock(), iter(range(1, 1000))
    monkeypatch.setattr(S, "time", clock)
    monkeypatch.setattr(audit_mod, "time", clock)
    monkeypatch.setattr(S, "secrets", types.SimpleNamespace(token_urlsafe=lambda _n: f"Rq{next(n):014d}"))
    return clock


def raw_of(action_type, who, text):
    return json.dumps({"action_type": action_type, "params": {"to": f"{who}@example.org", "text": text}}, sort_keys=True).encode()


def kinds(type_id):
    return "mail" if type_id.startswith("mail.") else None


def scenario(tmp_path, monkeypatch, log_on=True):
    """Every way a request ends, driven as the dashboard, withdraw.py and the ledger drive the store and write the log. Uses only
    what the store, the log and the ledger offered before 9a, so it runs against that version too."""
    clock = fixed(monkeypatch)
    d = str(tmp_path)
    st = S.Store(d, "external", 3600, 50)
    audit = AuditLog(d, enabled=log_on)
    ledger = ApprovalLedger({"external": st}, audit, kinds=kinds)
    ids = {}

    def add(name, action_type="t.echo"):
        clock.tick()
        q = st.add(raw_of(action_type, name, f"Hello {name}"), [f"{name}@example.org"], action_type=action_type, requested_by="planner")
        ids[name] = q.id
        return q

    def log(action, q, actor="owner", **details):
        audit.record(action, actor=actor, via="passkey", summary="x", kind="external", outbox_id=q.id, action_type=q.action_type,
                     sha256=q.sha256, **details)

    def run(q, how, delivery):
        clock.tick()
        st.claim(q.id, "executing")
        st.finish(q.id, how)
        log("approval.sent", q, delivery=delivery)

    waiting = add("ada")
    run(add("ben"), "sent", "sent")
    run(add("cleo"), "unclear", "unconfirmed")
    q = add("dan")
    clock.tick()
    st.claim(q.id, "discarded")
    log("approval.discarded", q)
    mid = audit._last
    for name, code in (("eva", "target_changed"), ("finn", "not_allowed")):
        q = add(name)
        clock.tick()
        st.claim(q.id, "executing")
        st.finish(q.id, "discarded", code)
        log("approval.discarded", q, actor="server", reason=code)
    q = add("gus")
    clock.tick()
    st.claim(q.id, "withdrawn")
    audit.record("approval.withdrawn", actor="assistant", via="planner", summary="x", kind="external", outbox_id=q.id, reason="duplicate",
                 action_type=q.action_type, sha256=q.sha256)
    q = add("hana")
    st._db.execute("UPDATE request SET expires_at = ? WHERE id = ?", (clock.now - 30, q.id))
    clock.tick()
    st.pending()                                                         # the lazy expiry notices it
    q = add("ivo")                                                        # failed once, back, then sent
    clock.tick()
    st.restore(st.claim(q.id, "executing"))
    log("approval.failed", q, ok=False)
    run(q, "sent", "sent")
    q = add("jana")                                                       # the target changed at approval: a new version waits
    clock.tick()
    revised = st.revise(st.claim(q.id, "executing"), raw_of("t.echo", "jana", "Hello again"))
    log("approval.revised", q, actor="server", new_sha256=revised.sha256, reason="target_changed", version=2)
    clock.tick()
    st.claim(q.id, "discarded")
    log("approval.discarded", revised)
    q = add("kai")                                                        # edited by the owner, then sent
    q = st.replace(q.id, q.sha256, raw_of("t.echo", "kai", "Owner words"), edited_from="Hello kai")
    log("approval.edited", q)
    run(q, "sent", "sent")
    q = add("lea")                                                        # edited, brought back, sent
    q = st.replace(q.id, q.sha256, raw_of("t.echo", "lea", "Owner words"), edited_from="Hello lea")
    log("approval.edited", q)
    q = st.replace(q.id, q.sha256, raw_of("t.echo", "lea", "Hello lea"), edited_from=None)
    log("approval.edit_undone", q)
    run(q, "sent", "sent")
    run(add("mo", "mail.send"), "sent", "sent")                           # kind mail (D6-8)
    q = add("nia")                                                        # edited, then it expired
    q = st.replace(q.id, q.sha256, raw_of("t.echo", "nia", "Owner words"), edited_from="Hello nia")
    log("approval.edited", q)
    st._db.execute("UPDATE request SET expires_at = ? WHERE id = ?", (clock.now - 30, q.id))
    clock.tick()
    st.pending()
    both = [add("ole"), add("pia")]                                       # the old /outbox page discards several in one entry
    clock.tick()
    for q in both:
        st.claim(q.id, "discarded")
    audit.record("approval.discarded", actor="owner", via="password", summary="x", kind="external", outbox_ids=[q.id for q in both])
    q = add("rex")                                                        # being carried out when the server stopped
    st.claim(q.id, "executing")
    st._db.close()
    os.close(S._locks.pop(str(Path(d).resolve())))
    clock.tick()
    st = S.Store(d, "external", 3600, 50)                                 # the start finds it executing
    ledger = ApprovalLedger({"external": st}, audit, kinds=kinds)        # and logs it as an unconfirmed send
    clock.tick()
    out = {"ids": ids, "status": {name: ledger.status(i) for name, i in ids.items()}, "never": ledger.status("never-queued")}
    assert out["status"]["ada"]["status"] == "waiting" and waiting.id == ids["ada"]
    if log_on:
        out["since_start"] = ledger.since(GENESIS)
        out["since_mid"] = ledger.since(mid)
        monkeypatch.setattr(status_mod, "_PAGE", 3)
        pages, cursor = [], GENESIS
        for _ in range(10):
            pages.append(ledger.since(cursor))
            cursor = pages[-1]["cursor"]
            if not pages[-1]["more"]:
                break
        out["pages"] = pages
        monkeypatch.setattr(status_mod, "_PAGE", 50)
    os.close(S._locks.pop(str(Path(d).resolve())))
    return out, st, ledger


def test_every_answer_is_what_it_was_before_the_ledger_while_the_log_is_on(tmp_path, monkeypatch):
    """protects=9a's rule that agents get the same answers as before where the log is on: each status, kind, action type, reason,
    note, edited and time, and every list by a log cursor with its paging; fails_when=any answer differs, as JSON, from what commit
    1f42796 gave for the same scenario, beyond the one difference below; no_claim=the answers of since='now' (its cursor is the
    ledger's now, tested below).
    The one difference: the old /outbox page's "discard all" writes one log line for several ids and names no action type, so
    before 9a their answers had none. The ledger keeps each request's type, so they now carry it, like every other ending."""
    got, *_ = scenario(tmp_path, monkeypatch)
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
        return
    got, want = json.loads(json.dumps(got)), json.loads(GOLDEN.read_text())
    several = {got["ids"]["ole"], got["ids"]["pia"]}
    answers = [*got["status"].values(), *(i for k in ("since_start", "since_mid") for i in got[k]["items"]),
               *(i for page in got["pages"] for i in page["items"])]
    assert [a.pop("action_type") for a in answers if a["id"] in several] == ["t.echo"] * 8
    assert got == want


def test_the_golden_file_holds_every_ending():
    """A golden file that lost its content would pass anything: it holds one answer per way a request ends."""
    want = json.loads(GOLDEN.read_text())
    assert {r["status"] for r in want["status"].values()} == {"waiting", "sent", "unclear", "discarded", "withdrawn", "expired"}
    assert {r.get("reason") for r in want["status"].values()} >= {"target_changed", "not_allowed"}
    assert [r["edited"] for n, r in sorted(want["status"].items()) if n in ("kai", "lea", "nia")] == [True, False, True]
    assert want["status"]["mo"]["kind"] == "mail" and len(want["since_start"]["items"]) == 16 and len(want["pages"]) == 6


def test_with_the_log_off_every_answer_is_the_same_and_comes_from_the_ledger(tmp_path, monkeypatch):
    """protects=9a's point: approval_status no longer depends on the activity log; fails_when=an ended request reads unknown or
    differs from the log-on answer with the log off, or since from the ledger's start lists other items; no_claim=requests that
    ended before 9a and are no longer in the store (they read unknown, as before)."""
    got, st, ledger = scenario(tmp_path, monkeypatch, log_on=False)
    want = json.loads(GOLDEN.read_text())
    for name in ("ole", "pia"):                                           # the one difference (see the test above)
        assert got["status"][name].pop("action_type") == "t.echo"
    assert got["status"] == want["status"] and got["ids"] == want["ids"]
    assert got["never"]["status"] == "unknown" and "AUDIT_LOG=false" in got["never"]["note"]
    listed = ledger.since("e0")
    assert [{k: v for k, v in i.items() if not (k == "action_type" and i["id"] in (got["ids"]["ole"], got["ids"]["pia"]))}
            for i in listed["items"]] == want["since_start"]["items"] and listed["more"] is False
    assert listed["cursor"] == f"e{st.event_head()}" and ledger.since(listed["cursor"])["items"] == []
    old = ledger.since(want["since_mid"]["cursor"])                       # a log cursor while the log is off: start again from the end
    assert old["items"] == [] and old["cursor_unknown"] is True and old["cursor"] == listed["cursor"]


# -- since with a ledger cursor ---------------------------------------------------------------------------------------------------
def parts(tmp_path, log_on=True):
    st = S.Store(str(tmp_path), "external", 3600, 50)
    audit = AuditLog(str(tmp_path), enabled=log_on)
    return st, audit, ApprovalLedger({"external": st}, audit, kinds=kinds)


def add(st, who="jana", text="Hello"):
    return st.add(raw_of("t.echo", who, text), [f"{who}@example.org"], action_type="t.echo")


@pytest.mark.parametrize("log_on", [True, False])
def test_now_hands_out_a_ledger_cursor_and_decisions_come_after_it_in_order(tmp_path, log_on):
    """protects=the new cursor: e<seq>, read from the ledger, the same with the log on or off; fails_when=now gives a log hash,
    a decision is missed or listed twice, a queued or revised request is listed as decided, or an unknown cursor lists anything;
    no_claim=delivery to a receiver (9b)."""
    st, _, ledger = parts(tmp_path, log_on)
    first = ledger.since("now")
    assert first == {"items": [], "cursor": "e0", "more": False}
    a, b, c = add(st, "jana"), add(st, "tom"), add(st, "sam")
    st.claim(b.id, "discarded")
    st.revise(st.claim(c.id, "executing"), raw_of("t.echo", "sam", "Other"))   # waits again: no decision
    st.claim(a.id, "executing")
    st.finish(a.id, "sent")
    got = ledger.since(first["cursor"])
    assert [(i["id"], i["status"]) for i in got["items"]] == [(b.id, "discarded"), (a.id, "sent")] and got["more"] is False
    assert got["cursor"] == f"e{st.event_head()}" and ledger.since(got["cursor"])["items"] == []
    for bad in ("e99", "e", "e-1", "e1x"):
        lost = ledger.since(bad)
        assert lost["items"] == [] and lost["cursor_unknown"] is True


def test_ledger_pages_lose_nothing(tmp_path, monkeypatch):
    """protects=paging by ledger cursor, as by log cursor; fails_when=a page cut skips or repeats a request; no_claim=none."""
    st, _, ledger = parts(tmp_path)
    monkeypatch.setattr(status_mod, "_PAGE", 2)
    cursor = ledger.since("now")["cursor"]
    made = [add(st, f"guest{n}") for n in range(5)]
    for q in made:
        st.claim(q.id, "discarded")
    seen = []
    for _ in range(5):
        page = ledger.since(cursor)
        seen += [i["id"] for i in page["items"]]
        cursor = page["cursor"]
        if not page["more"]:
            break
    assert seen == [q.id for q in made]


def test_an_old_log_cursor_keeps_working_and_hands_back_log_cursors(tmp_path):
    """protects=old cursors keep working while the log is on; fails_when=a log hash is refused or answered from elsewhere;
    no_claim=none beyond protects."""
    st, audit, ledger = parts(tmp_path)
    audit.record("tool.mail", actor="Claude", summary="Something unrelated")
    old = audit.entries()[0]["h"]
    q = add(st)
    st.claim(q.id, "discarded")
    audit.record("approval.discarded", actor="owner", summary="x", kind="external", outbox_id=q.id, action_type="t.echo")
    got = ledger.since(old)
    assert [(i["id"], i["status"]) for i in got["items"]] == [(q.id, "discarded")] and got["cursor"] == audit.entries()[0]["h"]


# -- the events ---------------------------------------------------------------------------------------------------------------
def events(st, item_id=None):
    rows = st._db.execute("SELECT * FROM status_event" + (" WHERE request_id = ?" if item_id else "") + " ORDER BY seq",
                          (item_id,) if item_id else ()).fetchall()
    return [dict(r) for r in rows]


def said(st, item_id):
    return [(e["status"], e["reason"], e["edited"], e["reconciled"]) for e in events(st, item_id)]


def test_each_row_of_the_status_table_writes_its_event_and_nothing_else_does(tmp_path):
    """protects=the table in docs/SENDGATE.md, package 9: one event per change, with the reason codes; edited read before _end
    drops edited_from; fails_when=a claim, a failed release or an owner's edit writes an event, or an ending writes none or the
    wrong one; no_claim=the dashboard's own wiring (tests/test_approval_status.py covers it end to end)."""
    st = S.Store(str(tmp_path), "external", 3600, 50)
    q = add(st, "jana")
    assert said(st, q.id) == [("waiting", "queued", 0, 0)]
    run = st.claim(q.id, "executing")
    st.restore(run)                                                        # failed: it never went out, it waits again
    q = st.replace(q.id, q.sha256, raw_of("t.echo", "jana", "Owner words"), edited_from="Hello")
    assert said(st, q.id) == [("waiting", "queued", 0, 0)]
    st.revise(st.claim(q.id, "executing"), raw_of("t.echo", "jana", "Service words"))
    st.claim(q.id, "executing")
    st.finish(q.id, "sent")
    assert said(st, q.id) == [("waiting", "queued", 0, 0), ("waiting", "target_changed", 1, 0), ("sent", None, 1, 0)]
    endings = {}
    for how in ("unclear", "discarded/target_changed", "discarded/not_allowed"):
        r = add(st, how.replace("/", "-"))
        st.claim(r.id, "executing")
        st.finish(r.id, *how.split("/"))
        endings[how] = said(st, r.id)[-1]
    for how in ("discarded", "withdrawn"):
        r = add(st, how)
        st.claim(r.id, how)
        endings[how] = said(st, r.id)[-1]
    assert endings == {"unclear": ("unclear", None, 0, 0), "discarded/target_changed": ("discarded", "target_changed", 0, 0),
                       "discarded/not_allowed": ("discarded", "not_allowed", 0, 0), "discarded": ("discarded", None, 0, 0),
                       "withdrawn": ("withdrawn", None, 0, 0)}
    r = add(st, "rex")
    st.claim(r.id, "executing")
    st._db.close()
    os.close(S._locks.pop(str(Path(tmp_path).resolve())))
    st = S.Store(str(tmp_path), "external", 3600, 50)                     # a crash left it executing
    assert said(st, r.id) == [("waiting", "queued", 0, 0), ("unclear", "interrupted", 0, 0)]
    assert {e["kind"] for e in events(st)} == {"external"} and {e["action_type"] for e in events(st)} == {"t.echo"}


def test_an_expiry_gets_its_event_when_the_store_notices_it_with_the_time_it_ran_out(tmp_path):
    """protects=the decision for lazy expiry: the event is written by the read that notices it, once, carrying expires_at;
    fails_when=no event, two events, or the time it was noticed instead of the time it ran out; no_claim=a clock that notices it
    without traffic (9b's worker)."""
    st = S.Store(str(tmp_path), "external", 3600, 50)
    q = add(st)
    ran_out = q.created_at - 1
    st._db.execute("UPDATE request SET expires_at = ? WHERE id = ?", (ran_out, q.id))
    assert said(st, q.id) == [("waiting", "queued", 0, 0)]
    st.pending(), st.pending()
    ended = events(st, q.id)[-1]
    assert (ended["status"], ended["at"]) == ("expired", ran_out) and len(events(st, q.id)) == 2


def test_a_change_that_is_rolled_back_writes_no_event_and_no_event_lands_without_its_change(tmp_path, monkeypatch):
    """protects=criterion 5 of package 9: the event is in the change's transaction; fails_when=a failed claim leaves an event, or
    a failed event write leaves the change (a request queued, or ended); no_claim=a crash of the process mid-commit (SQLite's own
    guarantee)."""
    st = S.Store(str(tmp_path), "external", 3600, 50)
    q = add(st)
    before = events(st)

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(st, "_gc", broken)                                # fails after the event was inserted
    with pytest.raises(sqlite3.OperationalError):
        st.claim(q.id, "discarded")
    monkeypatch.undo()
    assert st.state_of(q.id) == ("waiting", None) and events(st) == before

    def no_event(*a, **k):
        raise RuntimeError("the event could not be written")
    st.claim(q.id, "executing")
    monkeypatch.setattr(st, "_event", no_event)
    with pytest.raises(RuntimeError):
        st.finish(q.id, "sent")
    with pytest.raises(RuntimeError):
        add(st, "tom")
    monkeypatch.undo()
    assert st.state_of(q.id) == ("executing", None) and events(st) == before
    assert st._db.execute("SELECT COUNT(*) FROM request").fetchone()[0] == 1


def test_an_event_never_changes(tmp_path):
    """protects=immutable events; fails_when=an UPDATE of an event succeeds; no_claim=someone with the file (it is not signed)."""
    st = S.Store(str(tmp_path), "external", 3600, 50)
    add(st)
    with pytest.raises(sqlite3.DatabaseError, match="never changes"):
        st._db.execute("UPDATE status_event SET status = 'sent'")


def test_no_parameter_text_recipient_or_label_is_in_any_event(tmp_path):
    """protects=the event body rule (metadata only; D9-6: no requested_by); fails_when=a value of a request's parameters, its key,
    its recipients or the agent's label appears in an event row, or the row has a field beyond the list; no_claim=the webhook body
    (9b builds it from these fields by name)."""
    st = S.Store(str(tmp_path), "external", 3600, 50)
    secrets_ = ["Jana Example", "jana@example.org", "tom@example.net", "Quarterly picnic at the lake", "planner-sam", "draft-key-77",
                "Owner wrote this", "sam@example.org"]

    def ask(n, key=None):
        raw = json.dumps({"action_type": "market.reply", "params": {"to": ["jana@example.org", "tom@example.net"], "name": "Jana Example",
                                                                    "title": "Quarterly picnic at the lake", "n": n}}, sort_keys=True).encode()
        return st.add(raw, ["jana@example.org", "tom@example.net"], {"subject": "Quarterly picnic at the lake"}, key=key,
                      requested_by="planner-sam", action_type="market.reply")
    a, b, c, d, e = ask(1, "draft-key-77"), ask(2), ask(3), ask(4), ask(5)
    a = st.replace(a.id, a.sha256, a.raw + b" ", edited_from="Owner wrote this")
    st.revise(st.claim(a.id, "executing"), a.raw + b"sam@example.org")
    st.claim(a.id, "executing")
    st.finish(a.id, "sent")
    st.claim(b.id, "discarded")
    st.claim(c.id, "withdrawn")
    st.claim(d.id, "executing")
    st.finish(d.id, "discarded", "not_allowed")
    st._db.execute("UPDATE request SET expires_at = 1 WHERE id = ?", (e.id,))
    st.pending()
    rows = events(st)
    assert len(rows) == 11 and all(set(r) == EVENT_FIELDS for r in rows)
    text = json.dumps(rows) + repr(st._db.execute("SELECT * FROM status_event").fetchall())
    for value in secrets_:
        assert value not in text


def test_events_go_with_their_request_and_a_seq_is_never_used_again(tmp_path):
    """protects=the retention: events leave with their request after keep_days, and a cursor never names two events;
    fails_when=events outlive their request, or the head goes back; no_claim=none beyond protects."""
    st = S.Store(str(tmp_path), "external", 3600, 50, keep_days=1)
    old, kept = add(st, "jana"), add(st, "tom")
    st.claim(old.id, "discarded")
    head = st.event_head()
    st._db.execute("UPDATE request SET decided_at = decided_at - 2 * 86400 WHERE id = ?", (old.id,))
    st._purged_at = 0
    st.pending()
    assert events(st, old.id) == [] and len(events(st, kept.id)) == 1 and st.event_head() == head
    assert int(add(st, "sam") and st.event_head()) == head + 1


# -- a version without the ledger ------------------------------------------------------------------------------------------------
def test_changes_made_without_the_ledger_are_reconciled_at_the_next_start(tmp_path):
    """protects=the review's rollback point: a state an older version set without an event gets one, marked reconciled, and
    while the log has the entry the answer stays the log's; fails_when=a missing event stays missing, an event is written twice,
    or a reconciled event overrides the log; no_claim=the older version itself (the next test runs it)."""
    st, audit, ledger = parts(tmp_path)
    q = add(st)
    st._db.execute("UPDATE request SET state = 'sent', decided_at = ?, raw = NULL WHERE id = ?", (q.created_at + 5, q.id))
    audit.record("approval.sent", actor="owner", summary="x", kind="external", outbox_id=q.id, action_type="t.echo", delivery="unconfirmed")
    st._db.close()
    os.close(S._locks.pop(str(Path(tmp_path).resolve())))
    st = S.Store(str(tmp_path), "external", 3600, 50)
    assert said(st, q.id) == [("waiting", "queued", 0, 0), ("sent", None, 0, 1)]
    assert ApprovalLedger({"external": st}, audit, kinds=kinds).status(q.id)["status"] == "unclear"      # the log's, as before
    (tmp_path / "audit.jsonl").unlink()                                   # without the entry the reconciled event answers
    assert ApprovalLedger({"external": st}, AuditLog(str(tmp_path), enabled=False)).status(q.id)["status"] == "sent"
    st._db.close()
    os.close(S._locks.pop(str(Path(tmp_path).resolve())))
    st = S.Store(str(tmp_path), "external", 3600, 50)
    assert len(events(st, q.id)) == 2                                     # nothing missing: nothing written


SCRIPT = """
import json, sqlite3, sys, time
from icloud_mcp.gate import store as S
d, phase = sys.argv[1], sys.argv[2]
st = S.Store(d, "external", 3600, 50)
def add(n):
    raw = json.dumps({"action_type": "t.echo", "params": {"to": n + "@example.org", "text": "Hello " + n}}, sort_keys=True).encode()
    return st.add(raw, [n + "@example.org"], action_type="t.echo").id
ids = {} if phase == "first" else json.load(open(d + "/ids.json"))
if phase == "first":
    for n in ("jana", "tom", "sam", "ben", "lea"):
        ids[n] = add(n)
    st.claim(ids["jana"], "executing")
    st.finish(ids["jana"], "sent")
elif phase == "old":
    st.claim(ids["tom"], "discarded")
    st.claim(ids["sam"], "withdrawn")
    st._db.execute("UPDATE request SET expires_at = ? WHERE id = ?", (time.time() - 1, ids["lea"]))
    st.pending()
    ids["mia"] = add("mia")
    st.claim(ids["ben"], "executing")
json.dump(ids, open(d + "/ids.json", "w"))
print(json.dumps({"ledger": hasattr(st, "last_event"), "user_version": st._db.execute("PRAGMA user_version").fetchone()[0]}))
"""


def test_upgrade_downgrade_decide_and_upgrade_again(tmp_path):
    """protects=the review's rollback test and D9-8: the previous version (commit 1f42796) starts on a DATA_DIR with the ledger,
    decides requests without events, and the next start of this version writes what is missing; fails_when=the previous version
    does not start, the store version changes, an event is missing or written twice, or approval_status (log off) reads unknown;
    no_claim=other sources than external (one table holds them all)."""
    if subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", f"{BEFORE}^{{commit}}"], capture_output=True).returncode:
        pytest.skip(f"commit {BEFORE} is not in this checkout")
    old = tmp_path / "previous"
    old.mkdir()
    with tarfile.open(fileobj=io.BytesIO(subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", BEFORE, "src"],
                                                         capture_output=True, check=True).stdout)) as tar:
        tar.extractall(old, filter="data")
    data = tmp_path / "data"
    data.mkdir()

    def run(phase, src):
        got = subprocess.run([sys.executable, "-c", SCRIPT, str(data), phase], capture_output=True, text=True, timeout=60,
                             env={**os.environ, "PYTHONPATH": str(src)}, cwd=str(tmp_path))
        assert got.returncode == 0, got.stderr
        return json.loads(got.stdout.strip().splitlines()[-1])
    assert run("first", ROOT / "src") == {"ledger": True, "user_version": 2}
    assert run("old", old / "src") == {"ledger": False, "user_version": 2}          # the previous version starts and decides
    assert run("again", ROOT / "src") == {"ledger": True, "user_version": 2}
    ids = json.loads((data / "ids.json").read_text())
    st = S.Store(str(data), "external", 3600, 50)
    try:
        assert {n: said(st, i) for n, i in ids.items()} == {
            "jana": [("waiting", "queued", 0, 0), ("sent", None, 0, 0)],
            "tom": [("waiting", "queued", 0, 0), ("discarded", None, 0, 1)],
            "sam": [("waiting", "queued", 0, 0), ("withdrawn", None, 0, 1)],
            "lea": [("waiting", "queued", 0, 0), ("expired", None, 0, 1)],
            "ben": [("waiting", "queued", 0, 0), ("unclear", "interrupted", 0, 0)],      # left executing: this start recovers it
            "mia": [("waiting", None, 0, 1)]}
        lea = st._db.execute("SELECT expires_at FROM request WHERE id = ?", (ids["lea"],)).fetchone()[0]
        assert events(st, ids["lea"])[-1]["at"] == lea
        ledger = ApprovalLedger({"external": st}, AuditLog(str(data), enabled=False))
        assert {n: ledger.status(i)["status"] for n, i in ids.items()} == {
            "jana": "sent", "tom": "discarded", "sam": "withdrawn", "lea": "expired", "ben": "unclear", "mia": "waiting"}
    finally:
        st._db.close()
        os.close(S._locks.pop(str(data.resolve())))


def test_a_decision_between_the_look_at_the_queue_and_the_ledger_is_listed_once_it_is_known(tmp_path):
    """protects=since from one consistent view (Codex review of 9a, finding 1); fails_when=a request approved while since runs is
    listed as waiting with a cursor past its ending, so no later call lists it; no_claim=the log-cursor path, which reads the log."""
    st, _, ledger = parts(tmp_path)
    cursor = ledger.since("now")["cursor"]
    q = add(st)
    look = ledger._waiting

    def approved_meanwhile():
        seen = look()
        if st.state_of(q.id)[0] == "waiting":
            st.claim(q.id, "executing")
            st.finish(q.id, "sent")
        return seen
    ledger._waiting = approved_meanwhile
    seen = []
    for _ in range(3):
        page = ledger.since(cursor)
        seen += [(i["id"], i["status"]) for i in page["items"]]
        cursor = page["cursor"]
    assert seen == [(q.id, "sent")]


def reopen(st, tmp_path):
    st._db.close()
    os.close(S._locks.pop(str(Path(tmp_path).resolve())))
    return S.Store(str(tmp_path), "external", 3600, 50)


def test_an_owner_discard_after_an_export_and_a_rollback_is_reconciled(tmp_path):
    """protects=reconciling compares the whole ending (Codex review of 9a, finding 2); fails_when=a request exported under 9a,
    read in again by the previous version and discarded there by the owner keeps the export as its last event and reads unknown
    with the log off; no_claim=the previous version's read-in itself (the rollback test runs that version)."""
    st = S.Store(str(tmp_path), "external", 3600, 50)
    q = add(st)
    st.export_waiting("external-outbox.json")
    assert said(st, q.id)[-1] == ("discarded", "exported", 0, 0)
    # the previous version reads the file in again (waiting) and the owner discards it there, without events
    st._db.execute("UPDATE request SET state = 'discarded', outcome_reason = NULL, decided_at = ? WHERE id = ?", (q.created_at + 9, q.id))
    st = reopen(st, tmp_path)
    assert said(st, q.id)[-1] == ("discarded", None, 0, 1)
    assert ApprovalLedger({"external": st}, AuditLog(str(tmp_path), enabled=False)).status(q.id)["status"] == "discarded"

