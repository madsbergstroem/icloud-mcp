"""The approval store (gate/store.py, docs/SENDGATE.md package 3): states, keys, crashes, migration and rollback. Every request
here is made up. Each test says what it protects, the wrong result it rejects and what a green run does not prove."""
import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from icloud_mcp import actions as A
from icloud_mcp.audit import AuditLog
from icloud_mcp.gate import store as S
from icloud_mcp.gate.external import ExternalApprovals
from icloud_mcp.gate.status import ApprovalLedger
from icloud_mcp.outbox import Outbox

ROOT = Path(__file__).resolve().parents[1]


def make(tmp_path, ttl=3600, max_items=20, **kw):
    return S.Store(str(tmp_path), "external", ttl, max_items, **kw)


def add(store, text="Hello", key=None, fp=None):
    raw = json.dumps({"action_type": "t.echo", "params": {"to": "anna@example.org", "text": text}}, sort_keys=True).encode()
    return store.add(raw, ["anna@example.org"], key=key, action_type="t.echo", key_fingerprint=fp)


def row(store, item_id):
    return store._db.execute("SELECT * FROM request WHERE id = ?", (item_id,)).fetchone()


# -- states -----------------------------------------------------------------------------------------------------------------------
def test_each_transition_and_the_ones_that_must_be_refused(tmp_path):
    """protects=a request moves only along the table in docs/SENDGATE.md; fails_when=a request is claimed twice, finished without
    being carried out, or ends with its content kept; no_claim=other processes: one holds the lock."""
    st = make(tmp_path)
    a, b, c = add(st, "a"), add(st, "b"), add(st, "c")
    run = st.claim(a.id, "executing")
    assert run is not None and run.attempts == 1 and row(st, a.id)["state"] == "executing"
    assert st.claim(a.id, "executing") is None                                            # at most once
    st.finish(a.id, "sent")
    assert row(st, a.id)["state"] == "sent"
    st.finish(a.id, "unclear")                                                            # not executing any more: nothing happens
    assert row(st, a.id)["state"] == "sent"
    assert st.claim(b.id, "discarded").id == b.id and row(st, b.id)["state"] == "discarded"
    assert st.claim(c.id, "withdrawn").id == c.id and row(st, c.id)["state"] == "withdrawn"
    with pytest.raises(ValueError):
        st.claim(add(st, "d").id, "sent")                                                 # only executing, discarded, withdrawn
    with pytest.raises(ValueError):
        st.finish(a.id, "waiting")
    for item in (a, b, c):
        r = row(st, item.id)
        assert (r["raw"], r["recipients"], r["followup"], r["edited_from"]) == (None, None, None, None)
        assert r["sha256"] == item.sha256                                                 # what it was stays known
    assert [q.id for q in st.pending()] == [row_d["id"] for row_d in st._db.execute("SELECT id FROM request WHERE state='waiting'")]


def test_failed_puts_it_back_and_past_its_expiry_it_ends_expired_once(tmp_path):
    """protects=a failed send waits again, but not past its time (review obligation E-12); fails_when=an expired request comes back
    or is reported twice; no_claim=none beyond protects."""
    st = make(tmp_path)
    seen = []
    st.on_expiry(seen.append)
    q = add(st)
    st.restore(st.claim(q.id, "executing"))
    assert [x.id for x in st.pending()] == [q.id] and row(st, q.id)["attempts"] == 1
    run = st.claim(q.id, "executing")
    st._db.execute("UPDATE request SET expires_at = ? WHERE id = ?", (time.time() - 1, q.id))
    st.restore(run)
    assert row(st, q.id)["state"] == "expired" and st.pending() == [] and [x.id for x in seen] == [q.id]
    st.pending(), st.pending()
    assert len(seen) == 1


def test_waiting_requests_expire_lazily_and_are_reported_exactly_once(tmp_path):
    """protects=approval.expired is written once per request; fails_when=a second read reports it again;
    no_claim=a clock that goes backwards."""
    st = make(tmp_path)
    q = add(st)
    st._db.execute("UPDATE request SET expires_at = ? WHERE id = ?", (time.time() - 1, q.id))
    seen = []
    st.pending()                                                                           # expires before anything listens
    st.on_expiry(seen.append)                                                             # reported now, once
    st.pending()
    assert [x.id for x in seen] == [q.id] and row(st, q.id)["state"] == "expired"


def test_ended_rows_go_after_the_log_span_and_their_key_with_them(tmp_path):
    """protects=nothing is kept longer than the activity log keeps (review obligation K1); fails_when=an old ended row stays, or a
    waiting one is deleted; no_claim=none beyond protects."""
    st = make(tmp_path, keep_days=1)
    old, young = add(st, "old", key="k-old"), add(st, "young")
    st.claim(old.id, "discarded")
    st._db.execute("UPDATE request SET decided_at = ? WHERE id = ?", (time.time() - 2 * 86400, old.id))
    st._purged_at = 0
    st.pending()
    assert row(st, old.id) is None and row(st, young.id) is not None
    assert add(st, "old again", key="k-old").id != old.id                                 # the key is free again


# -- keys -------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("how,bound", [("sent", True), ("unclear", True), ("discarded", True), ("withdrawn", True),
                                       ("target_changed", False), ("expired", False)])
def test_a_key_stays_bound_or_is_freed_by_how_its_request_ended(tmp_path, how, bound):
    """protects=a repeated request_id never queues a second copy of something sent or turned down, and an agent can ask again after
    an expiry or a changed target (review obligation D7); fails_when=either rule flips; no_claim=mail, calendar and iMessage, which
    keep their own key rule until packages 4 to 6."""
    st = make(tmp_path)
    q = add(st, key="k-1", fp="f-1")
    if how in ("sent", "unclear"):
        st.claim(q.id, "executing")
        st.finish(q.id, how)
    elif how == "target_changed":
        st.claim(q.id, "executing")
        st.finish(q.id, "discarded", "target_changed")
    elif how == "expired":
        st._db.execute("UPDATE request SET expires_at = ? WHERE id = ?", (time.time() - 1, q.id))
        st.pending()
    else:
        st.claim(q.id, how)
    if bound:
        with pytest.raises(S.AlreadyDecided) as got:
            add(st, "Hello again", key="k-1", fp="f-1")
        assert got.value.item_id == q.id and got.value.state == ("discarded" if how == "discarded" else how)
        with pytest.raises(S.KeyConflict):
            add(st, "Hello again", key="k-1", fp="f-other")                              # the same key for something else
    else:
        assert add(st, "Hello again", key="k-1", fp="f-1").id != q.id
    assert st._db.execute("SELECT COUNT(*) FROM request WHERE key = 'k-1'").fetchone()[0] == 1


def test_a_waiting_key_returns_its_request_and_two_threads_with_one_key_make_one_row(tmp_path):
    """protects=one row per external key (review obligation E-11); fails_when=two threads both insert; no_claim=two processes:
    the lock keeps a second one out."""
    st = make(tmp_path)
    got = []
    threads = [threading.Thread(target=lambda t=t: got.append(add(st, t, key="k-2", fp="f").id)) for t in ("first", "second")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(set(got)) == 1 and st._db.execute("SELECT COUNT(*) FROM request").fetchone()[0] == 1


# -- crashes and approvals ----------------------------------------------------------------------------------------------------------
ECHO = A.ActionType(id="t.echo", title="Echo", badge="Echo", approve_label="Send",
                    params_schema={"type": "object", "properties": {"to": {"type": "string"}, "text": {"type": "string"}}},
                    text_field="text", recipients=lambda p: [p["to"]])


class Counting:
    def __init__(self, wait=0.0):
        self.calls, self.wait = [], wait

    def execute(self, request):
        time.sleep(self.wait)
        self.calls.append(request.id)
        return A.Result("sent", "Echoed.")


def source(tmp_path, ex):
    reg = A.ActionRegistry()
    reg.register(ECHO, ex, ["anna@example.org"])
    return ExternalApprovals(make(tmp_path), reg)


def test_a_crash_while_carrying_out_reads_unclear_after_the_restart_and_is_logged_once(tmp_path):
    """protects=a request half carried out is never repeated and never looks waiting (criterion 1); fails_when=the restart leaves
    it executing or waiting, or approval_status says anything but unclear; no_claim=a real process kill: the restart is simulated
    by a second process that exits without finishing."""
    code = ("import json, os, sys; sys.path.insert(0, %r)\n" % str(ROOT / "src")
            + "from icloud_mcp.gate.store import Store\n"
            + "st = Store(%r, 'external', 3600, 20)\n" % str(tmp_path)
            + "q = st.add(b'{\"action_type\":\"t.echo\",\"params\":{}}', ['anna@example.org'], action_type='t.echo')\n"
            + "st.claim(q.id, 'executing')\nprint(q.id, q.sha256, flush=True)\nos._exit(0)\n")     # ... and the process dies here
    qid, sha = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True).stdout.split()
    q = type("Q", (), {"id": qid, "sha256": sha})
    again = make(tmp_path)
    audit = AuditLog(str(tmp_path))
    ledger = ApprovalLedger({"external": again}, audit)
    assert row(again, q.id)["state"] == "unclear" and row(again, q.id)["outcome_reason"] == "interrupted"
    assert ledger.status(q.id)["status"] == "unclear"
    lines = [e for e in audit.entries(10) if e["action"] == "approval.sent"]
    assert len(lines) == 1 and lines[0]["details"]["delivery"] == "unconfirmed" and lines[0]["details"]["sha256"] == q.sha256


def test_two_approvals_at_the_same_moment_carry_it_out_once(tmp_path):
    """protects=one executor call per approval (criterion 2); fails_when=both threads reach the executor; no_claim=two processes."""
    ex = Counting(wait=0.2)
    src = source(tmp_path, ex)
    q = src.queue_external("t.echo", {"to": "anna@example.org", "text": "Hi"}, None)
    out = []

    def approve():
        try:
            out.append(str(src.release(q)))
        except A.ExternalError as e:
            out.append(f"refused: {e}")
    threads = [threading.Thread(target=approve) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert ex.calls == [q.id] and sorted(o.startswith("refused") for o in out) == [False, True]
    assert row(src.box, q.id)["state"] == "sent" and row(src.box, q.id)["raw"] is None


# -- files --------------------------------------------------------------------------------------------------------------------------
def test_the_json_queue_is_read_in_once_with_its_ids_and_hashes(tmp_path):
    """protects=nothing waiting is lost or doubled when the store takes over (criterion 5, review obligation X15);
    fails_when=an id or hash changes, the file stays, or a second start imports again; no_claim=mail, calendar and iMessage files,
    which stay JSON until packages 4 to 6."""
    old = Outbox(str(tmp_path), 3600, 20, filename="external-outbox.json")
    a = old.add(b'{"action_type":"t.echo","params":{"text":"one"}}', ["anna@example.org"], action_type="t.echo")
    b = old.add(b'{"action_type":"t.echo","params":{"text":"two"}}', ["anna@example.org"], key="k-3", action_type="t.echo")
    st = make(tmp_path, legacy="external-outbox.json")
    assert [(q.id, q.sha256, q.key) for q in st.pending()] == [(a.id, a.sha256, None), (b.id, b.sha256, "k-3")]
    assert not (tmp_path / "external-outbox.json").exists() and (tmp_path / "external-outbox.json.migrated").exists()
    (tmp_path / "external-outbox.json").write_text((tmp_path / "external-outbox.json.migrated").read_text())   # left behind by a crash
    again = make(tmp_path, legacy="external-outbox.json")
    assert len(again.pending()) == 2


def test_a_queue_file_that_cannot_be_read_is_set_aside_never_sent_and_reported(tmp_path):
    """protects=a broken file never turns into a send and the server still starts (review obligation E-1); fails_when=the start
    fails, something waits, or nothing says so; no_claim=a file that is valid JSON but holds wrong values: tampered entries are
    skipped by their hash, as before."""
    (tmp_path / "external-outbox.json").write_text("{not json")
    st = make(tmp_path, legacy="external-outbox.json")
    assert st.pending() == [] and "could not be read" in st.problem
    assert [p.name.startswith("external-outbox.json.corrupt-") for p in tmp_path.iterdir() if "corrupt" in p.name] == [True]


def test_export_writes_the_waiting_requests_back_and_refuses_while_one_runs(tmp_path):
    """protects=a rollback finds what was waiting, with the same ids and hashes (criterion 6, review obligation E-13);
    fails_when=the old JSON queue reads anything else, or the export runs while a request is being carried out;
    no_claim=what the rollback loses: the command prints it."""
    st = make(tmp_path)
    keep, run, done = add(st, "keep"), add(st, "run"), add(st, "done")
    st.claim(done.id, "discarded")
    st.claim(run.id, "executing")
    with pytest.raises(RuntimeError, match="being carried out"):
        st.export_waiting("external-outbox.json")
    st.finish(run.id, "unclear")
    got = st.export_waiting("external-outbox.json")
    assert got == {"written": 1, "ended_lost": 2, "unclear_lost": 1}
    old = Outbox(str(tmp_path), 3600, 20, filename="external-outbox.json")
    assert [(q.id, q.sha256) for q in old.pending()] == [(keep.id, keep.sha256)]


def test_the_database_and_its_side_files_are_private_and_a_shared_one_stops_the_start(tmp_path):
    """protects=other users of the machine cannot read waiting requests (review obligations E-15, X15); fails_when=a file is
    readable by others, or a store opens on such a file; no_claim=a directory others can list."""
    st = make(tmp_path)
    add(st)
    for name in ("approvals.db", "approvals.db-wal", "approvals.db-shm"):
        p = tmp_path / name
        if p.exists():
            assert stat.S_IMODE(p.stat().st_mode) == 0o600, name
    other = tmp_path / "shared"
    other.mkdir()
    (other / "approvals.db").touch(mode=0o644)
    os.chmod(other / "approvals.db", 0o644)
    with pytest.raises(RuntimeError, match="chmod 600"):
        S.Store(str(other), "external", 3600, 20)


def test_a_second_process_on_the_same_data_dir_refuses_to_start(tmp_path):
    """protects=two servers never carry out one request twice (review obligation E-5); fails_when=a second process opens the store;
    no_claim=a network file system without flock."""
    make(tmp_path)
    code = ("import sys; sys.path.insert(0, %r)\n" % str(ROOT / "src")
            + "from icloud_mcp.gate.store import Store\n"
            + "try:\n    Store(%r, 'external', 60, 5)\nexcept RuntimeError as e:\n    print('REFUSED', e)\n" % str(tmp_path))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60).stdout
    assert out.startswith("REFUSED") and "approvals.lock" in out


def test_a_full_disk_refuses_as_store_unavailable(tmp_path, monkeypatch):
    """protects=a store that cannot write queues nothing and says it may pass later (review obligation X15); fails_when=the error
    escapes as something else; no_claim=a real full disk."""
    st = make(tmp_path)

    class Full:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, *a):
            if sql.startswith("INSERT"):
                raise sqlite3.OperationalError("database or disk is full")
            return self.db.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self.db, name)
    monkeypatch.setattr(st, "_db", Full(st._db))
    with pytest.raises(S.StoreUnavailable):
        add(st)


def test_the_reason_code_survives_the_content_being_dropped(tmp_path):
    """protects=a request ended by the server says why, never with a parameter (review obligation E-15); fails_when=the code is
    lost when the content goes; no_claim=none beyond protects."""
    st = make(tmp_path)
    q = add(st)
    st.claim(q.id, "executing")
    st.finish(q.id, "discarded", "target_changed")
    r = row(st, q.id)
    assert (r["state"], r["outcome_reason"], r["raw"]) == ("discarded", "target_changed", None)


def test_a_duplicate_external_key_is_refused_by_the_index_itself(tmp_path):
    """protects=the unique index holds even if the code above it is wrong (review obligation E-11); fails_when=a second row with the
    key can be written; no_claim=other sources, which keep a non-unique key."""
    st = make(tmp_path)
    add(st, "one", key="k-u")
    with pytest.raises(sqlite3.IntegrityError):
        st._db.execute("INSERT INTO request (id, source, state, created_at, expires_at, sha256, key) VALUES ('x','external','waiting',0,0,'h','k-u')")


def test_a_busy_or_locked_database_refuses_as_store_unavailable(tmp_path, monkeypatch):
    """protects=a database that cannot be written right now answers as the retryable store_unavailable (review obligation X15);
    fails_when=a raw sqlite error escapes; no_claim=a real second writer: the lock keeps it out."""
    st = make(tmp_path)

    class Busy:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, *a):
            if sql.startswith("BEGIN"):
                raise sqlite3.OperationalError("database is locked")
            return self.db.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self.db, name)
    monkeypatch.setattr(st, "_db", Busy(st._db))
    with pytest.raises(S.StoreUnavailable):
        add(st)


def test_a_full_queue_names_the_approval_page(tmp_path):
    """protects=the agent is told where the owner decides (review obligation F-CEO-4); fails_when=the page is missing;
    no_claim=none beyond protects."""
    st = make(tmp_path, max_items=1)
    add(st, "one")
    raw = b'{"action_type":"t.echo","params":{"text":"two"}}'
    with pytest.raises(Exception, match="the owner's own page") as got:
        st.add(raw, ["anna@example.org"], approval_page="the owner's own page")
    assert type(got.value).__name__ == "OutboxFull"


def test_after_export_and_a_rollback_the_upgrade_never_revives_what_the_old_version_sent(tmp_path):
    """protects=a rollback and a second upgrade never offer a sent request for approval again (review finding F1);
    fails_when=a request the old version sent or discarded comes back waiting; no_claim=none beyond protects."""
    st = make(tmp_path)
    sent_by_old, kept = add(st, "old sends this", key="k-r"), add(st, "still waiting")
    assert st.export_waiting("external-outbox.json")["written"] == 2
    assert st.pending() == [] and row(st, sent_by_old.id)["outcome_reason"] == "exported"
    old = Outbox(str(tmp_path), 3600, 20, filename="external-outbox.json")             # the previous version runs ...
    old.claim(sent_by_old.id)                                                             # ... and sends one
    again = make(tmp_path, legacy="external-outbox.json")                                # the upgrade, once more
    assert [q.id for q in again.pending()] == [kept.id]
    assert add(again, "a new one", key="k-r").id != sent_by_old.id                       # the export freed the key


def test_reading_in_frees_an_ended_key_and_keeps_a_waiting_one(tmp_path):
    """protects=nothing the old version queued is dropped by a key the store still holds (review finding F3); fails_when=an entry
    is lost, or a waiting request loses its key; no_claim=none beyond protects."""
    st = make(tmp_path)
    ended, live = add(st, "ended", key="k-e"), add(st, "live", key="k-w")
    st.claim(ended.id, "discarded")
    old = Outbox(str(tmp_path), 3600, 20, filename="external-outbox.json")
    y = old.add(b'{"action_type":"t.echo","params":{"text":"y"}}', ["anna@example.org"], key="k-e", action_type="t.echo")
    z = old.add(b'{"action_type":"t.echo","params":{"text":"z"}}', ["anna@example.org"], key="k-w", action_type="t.echo")
    again = make(tmp_path, legacy="external-outbox.json")
    got = {q.id: q.key for q in again.pending()}
    assert got == {live.id: "k-w", y.id: "k-e", z.id: None} and z.id in again.problem


def test_a_second_store_in_the_same_process_leaves_a_running_request_alone(tmp_path):
    """protects=only the process that took the lock recovers rows (review finding F6); fails_when=opening another store turns a
    request being carried out into unclear; no_claim=none beyond protects."""
    st = make(tmp_path)
    q = add(st)
    st.claim(q.id, "executing")
    make(tmp_path)
    assert row(st, q.id)["state"] == "executing"
    st.finish(q.id, "sent")
    assert row(st, q.id)["state"] == "sent"


def test_an_expiry_nobody_heard_is_reported_once_when_someone_listens(tmp_path):
    """protects=no approval.expired line is lost because nothing listened yet, and none is written twice (review finding F5);
    fails_when=the report never comes or comes again; no_claim=none beyond protects."""
    st = make(tmp_path)
    q = add(st)
    st._db.execute("UPDATE request SET expires_at = ? WHERE id = ?", (time.time() - 1, q.id))
    st.pending()
    seen = []
    other = make(tmp_path)
    other.on_expiry(seen.append)
    other.on_expiry(seen.append)
    st.pending()
    assert [x.id for x in seen] == [q.id]


def test_a_database_from_a_newer_version_stops_the_start(tmp_path):
    """protects=an older server never works on a table it does not know; fails_when=it opens it; no_claim=none beyond protects."""
    db = make(tmp_path)._db
    db.execute("PRAGMA user_version = 99")
    db.execute("PRAGMA wal_checkpoint(FULL)")                                             # into the main file before it is copied
    other = tmp_path / "copy"
    other.mkdir()
    import shutil
    shutil.copy(tmp_path / "approvals.db", other / "approvals.db")
    os.chmod(other / "approvals.db", 0o600)
    with pytest.raises(RuntimeError, match="newer version"):
        S.Store(str(other), "external", 3600, 20)


def test_the_export_command_runs_from_the_package_and_reports_what_it_loses(tmp_path):
    """protects=the rollback runs in the Docker image and says what it costs (review obligations E-13, F3); fails_when=it is not a
    module of the package, writes nothing, or prints no counts; no_claim=the Docker image itself."""
    code = ("import sys; sys.path.insert(0, %r)\n" % str(ROOT / "src")
            + "from icloud_mcp.gate.store import Store\n"
            + "st = Store(%r, 'external', 3600, 20)\n" % str(tmp_path)
            + "a = st.add(b'{\"action_type\":\"t.echo\",\"params\":{\"n\":1}}', ['x'], action_type='t.echo')\n"
            + "b = st.add(b'{\"action_type\":\"t.echo\",\"params\":{\"n\":2}}', ['x'], action_type='t.echo')\n"
            + "st.claim(b.id, 'discarded')\n")
    subprocess.run([sys.executable, "-c", code], check=True, timeout=60)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    run = subprocess.run([sys.executable, "-m", "icloud_mcp.gate.export_queue", str(tmp_path)], capture_output=True, text=True, env=env, timeout=60)
    assert run.returncode == 0 and "Wrote 1 waiting request(s)" in run.stdout and "loses 1 ended request(s), 0 of them unclear" in run.stdout
    again = subprocess.run([sys.executable, "-m", "icloud_mcp.gate.export_queue", str(tmp_path)], capture_output=True, text=True, env=env, timeout=60)
    assert again.returncode == 1 and "exists already" in again.stderr                    # never overwrites an export
    assert subprocess.run([sys.executable, "-m", "icloud_mcp.gate.export_queue"], capture_output=True, env=env, timeout=60).returncode == 2


def test_versions_record_each_content_and_lose_it_when_the_request_ends(tmp_path):
    """protects=package 2c: add is version 1 (assistant), replace adds one (owner by default), revise one (service) and puts the
    request back to waiting; ending keeps author and hash and drops the content; fails_when=a version is overwritten, revise sends
    the request anywhere but waiting, or content outlives the request; no_claim=none beyond protects."""
    st = make(tmp_path)
    q = add(st)
    st.replace(q.id, q.sha256, b"edited", edited_from=None)
    claimed = st.claim(q.id, "executing")
    back = st.revise(claimed, b"from the service")
    assert back is not None and st.state_of(q.id) == ("waiting", None)
    vs = st.versions(q.id)
    assert [(v["n"], v["author"], v["raw"]) for v in vs] == [(1, "assistant", vs[0]["raw"]), (2, "owner", b"edited"), (3, "service", b"from the service")]
    assert st.pending()[0].sha256 == vs[-1]["sha256"]
    st.claim(q.id, "discarded")
    assert [v["raw"] for v in st.versions(q.id)] == [None, None, None] and len({v["sha256"] for v in st.versions(q.id)}) == 3


def test_a_version_1_file_gets_one_version_per_request(tmp_path):
    """protects=the store migration 1 -> 2; fails_when=a request of a version 1 file has no version, or the file stays at 1;
    no_claim=a file written by a version 2 server opened by a version 1 server (refused by user_version, tested elsewhere)."""
    st = make(tmp_path)
    q = add(st)
    db = sqlite3.connect(tmp_path / S.DB_FILE)
    db.execute("DROP TABLE version")
    db.execute("PRAGMA user_version = 1")
    db.commit()
    db.close()
    again = make(tmp_path)
    assert [(v["n"], v["author"], v["sha256"]) for v in again.versions(q.id)] == [(1, "assistant", q.sha256)]
    assert sqlite3.connect(tmp_path / S.DB_FILE).execute("PRAGMA user_version").fetchone()[0] == S.VERSION == 2


def test_the_owners_edits_in_a_row_are_one_version_and_going_back_removes_it(tmp_path):
    """protects=package 2c, owner decision 2026-11-16 (variant A): edits in a row fold into one version; bringing back the text
    before them leaves no version behind; fails_when=each edit adds a version, or going back adds one; no_claim=none beyond protects."""
    st = make(tmp_path)
    q = add(st)
    a = st.replace(q.id, q.sha256, b"first edit", edited_from="x")
    b = st.replace(q.id, a.sha256, b"second edit", edited_from="x")
    assert [(v["author"], v["raw"]) for v in st.versions(q.id)][1:] == [("owner", b"second edit")]
    st.replace(q.id, b.sha256, q.raw, edited_from=None)
    assert [v["author"] for v in st.versions(q.id)] == ["assistant"] and st.pending()[0].sha256 == q.sha256


def test_versions_go_with_their_request_and_every_way_of_ending_drops_their_content(tmp_path):
    """protects=package 2c review: withdraw and expiry drop version content like a discard; the purge removes versions of deleted
    requests and keeps the others; fails_when=content survives an ending, or the purge touches a waiting request's versions;
    no_claim=export, which ends requests through the same _end."""
    st = make(tmp_path, ttl=1)
    gone, kept = add(st, "one"), add(st, "two")
    st.claim(gone.id, "withdrawn")
    assert [v["raw"] for v in st.versions(gone.id)] == [None]
    time.sleep(1.1)
    st.pending()                                                # expiry runs on read
    assert [v["raw"] for v in st.versions(kept.id)] == [None]
    st2 = make(tmp_path / "b")
    w = add(st2, "waits")
    old = add(st2, "old")
    st2.claim(old.id, "discarded")
    st2._db.execute("UPDATE request SET decided_at = 0 WHERE id = ?", (old.id,))
    st2._purged_at = 0
    st2.pending()
    assert st2.versions(old.id) == [] and len(st2.versions(w.id)) == 1


def test_revise_needs_an_executing_request_and_ends_one_past_its_expiry(tmp_path):
    """protects=store.revise edges; fails_when=a waiting request is revised, or an expired one waits again; no_claim=none."""
    st = make(tmp_path, ttl=1)
    q = add(st)
    assert st.revise(q, b"x") is None                          # not executing
    claimed = st.claim(q.id, "executing")
    time.sleep(1.1)
    assert st.revise(claimed, b"x") is None and st.state_of(q.id)[0] == "expired"
    with pytest.raises(ValueError):
        st.replace(q.id, q.sha256, b"y", author="someone")


def test_an_owner_edit_after_a_service_version_is_a_new_version(tmp_path):
    """protects=the fold only joins the owner's own edits in a row; fails_when=an edit rewrites the service's version;
    no_claim=none beyond protects."""
    st = make(tmp_path)
    q = add(st)
    back = st.revise(st.claim(q.id, "executing"), b"service")
    st.replace(q.id, back.sha256, b"owner", edited_from="x")
    assert [(v["author"], v["raw"]) for v in st.versions(q.id)][1:] == [("service", b"service"), ("owner", b"owner")]


def test_the_migration_covers_ended_and_edited_rows_and_runs_once(tmp_path):
    """protects=1 -> 2 backfill: an ended row gets a version without content, an edited one is the owner's, a second open adds
    nothing; fails_when=any of these; no_claim=none beyond protects."""
    st = make(tmp_path)
    ended, edited = add(st, "a"), add(st, "b")
    st.claim(ended.id, "discarded")
    st._db.execute("UPDATE request SET edited_from = 'asked' WHERE id = ?", (edited.id,))
    db = sqlite3.connect(tmp_path / S.DB_FILE)
    db.execute("DROP TABLE version")
    db.execute("PRAGMA user_version = 1")
    db.commit()
    db.close()
    for _ in range(2):
        again = make(tmp_path)
        assert [(v["author"], v["raw"]) for v in again.versions(ended.id)] == [("assistant", None)]
        assert [v["author"] for v in again.versions(edited.id)] == ["owner"]


# -- package 4a: iMessage in the same file ------------------------------------------------------------------------------------
IMSG = b'{"chat_id": null, "handle": "+31600000001", "text": "Hi", "to": {"participants": []}}'


def test_a_crashed_imessage_is_recovered_even_when_the_external_store_opens_first(tmp_path):
    """protects=package 4a criterion 4: whichever store of the process opens first ends every source's half-sent request as
    unclear, and the iMessage ledger logs it once; fails_when=the iMessage row stays executing because the external store took the
    lock; no_claim=a real process kill: the restart is a second process that exits without finishing."""
    code = ("import os, sys; sys.path.insert(0, %r)\n" % str(ROOT / "src")
            + "from icloud_mcp.gate.store import Store\n"
            + "st = Store(%r, 'imessage', 3600, 20)\n" % str(tmp_path)
            + "q = st.add(%r, ['+31600000001'])\n" % IMSG
            + "st.claim(q.id, 'executing')\nprint(q.id, flush=True)\nos._exit(0)\n")
    qid = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True).stdout.strip()
    make(tmp_path)                                                        # the external store takes the lock first
    imsg = S.Store(str(tmp_path), "imessage", 3600, 20)
    audit = AuditLog(str(tmp_path))
    ApprovalLedger({"imessage": imsg}, audit)
    assert row(imsg, qid)["state"] == "unclear" and row(imsg, qid)["outcome_reason"] == "interrupted"
    lines = [e for e in audit.entries(10) if e["action"] == "approval.sent"]
    assert len(lines) == 1 and lines[0]["details"]["kind"] == "imessage" and lines[0]["details"]["delivery"] == "unconfirmed"


def test_the_export_writes_waiting_imessages_back_with_their_ids_and_hashes(tmp_path):
    """protects=package 4a criterion 3: a rollback keeps the waiting iMessages, in the file and format the previous version reads;
    fails_when=no imessage-outbox.json, another id or hash, or a file written when no iMessage waits; no_claim=the previous version
    itself (its Outbox class reads the file here)."""
    st = S.Store(str(tmp_path), "imessage", 3600, 20)
    q = st.add(IMSG, ["+31600000001"])
    st._db.close()
    os.close(S._locks.pop(str(Path(tmp_path).resolve())))                 # as if this process stopped: the command opens it again
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    run = subprocess.run([sys.executable, "-m", "icloud_mcp.gate.export_queue", str(tmp_path)], capture_output=True, text=True, env=env, timeout=60)
    assert run.returncode == 0 and "Wrote 1 waiting request(s) to imessage-outbox.json" in run.stdout, run.stderr
    old = Outbox(str(tmp_path), 3600, 20, filename="imessage-outbox.json")
    assert [(x.id, x.sha256, x.raw) for x in old.pending()] == [(q.id, q.sha256, IMSG)]
    empty = tmp_path / "none"
    empty.mkdir()
    S.Store(str(empty), "external", 3600, 20)._db.close()
    os.close(S._locks.pop(str(empty.resolve())))
    subprocess.run([sys.executable, "-m", "icloud_mcp.gate.export_queue", str(empty)], check=True, env=env, timeout=60, capture_output=True)
    assert not (empty / "imessage-outbox.json").exists()


def test_the_json_imessage_queue_is_read_in_with_its_ids_and_hashes(tmp_path):
    """protects=package 4a criterion 3: the first start with the store keeps every waiting iMessage, byte for byte; fails_when=an
    id, a hash or the bytes change, or the file is not renamed; no_claim=none beyond protects."""
    old = Outbox(str(tmp_path), 3600, 20, filename="imessage-outbox.json")
    q = old.add(IMSG, ["+31600000001"])
    st = S.Store(str(tmp_path), "imessage", 3600, 20, legacy="imessage-outbox.json")
    assert [(x.id, x.sha256, x.raw) for x in st.pending()] == [(q.id, q.sha256, IMSG)]
    assert (tmp_path / "imessage-outbox.json.migrated").exists() and not (tmp_path / "imessage-outbox.json").exists()


ICS = b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:u1\r\nSUMMARY:Autumn fair\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
CAL_META = {"op": "create", "calendar": "Club", "uid": "u1", "summary": "Autumn fair"}


def test_a_calendar_queue_file_is_read_in_with_its_ids_hashes_and_the_type_of_each_change(tmp_path):
    """protects=package 5a criterion 3: the first start keeps every waiting calendar change byte for byte, and the row learns its
    type (calendar.invite for a create), which outlasts the content; fails_when=an id, hash or byte changes, the file stays, the
    type is missing, or a type function that raises stops the start; no_claim=the calendar's own mapping (tested with cal.py)."""
    old = Outbox(str(tmp_path), 3600, 20, filename="calendar-outbox.json")
    q = old.add(ICS, ["jana@example.org"], CAL_META)
    bad = old.add(ICS + b" ", ["ben@example.org"], {"op": "update"})
    st = S.Store(str(tmp_path), "calendar", 3600, 20, legacy="calendar-outbox.json",
                 type_of=lambda x: {"create": "calendar.invite"}[(x.followup or {})["op"]])
    assert sorted((x.id, x.sha256, x.raw) for x in st.pending()) == sorted([(q.id, q.sha256, ICS), (bad.id, bad.sha256, ICS + b" ")])
    assert (tmp_path / "calendar-outbox.json.migrated").exists() and not (tmp_path / "calendar-outbox.json").exists()
    st.claim(q.id, "discarded")                                     # an ended row keeps its type although its content is gone
    types = dict(st._db.execute("SELECT id, action_type FROM request").fetchall())
    assert types == {q.id: "calendar.invite", bad.id: None}


def test_the_export_writes_waiting_calendar_changes_back_for_the_previous_version(tmp_path):
    """protects=package 5a criterion 3: a rollback keeps the waiting calendar changes with their metadata, in the file the
    previous version reads; fails_when=no calendar-outbox.json, another id, hash, byte or op, or a file written when none waits;
    no_claim=the previous version itself (its Outbox class reads the file here)."""
    st = S.Store(str(tmp_path), "calendar", 3600, 20)
    q = st.add(ICS, ["jana@example.org"], CAL_META, action_type="calendar.invite")
    st._db.close()
    os.close(S._locks.pop(str(Path(tmp_path).resolve())))
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    run = subprocess.run([sys.executable, "-m", "icloud_mcp.gate.export_queue", str(tmp_path)], capture_output=True, text=True, env=env, timeout=60)
    assert run.returncode == 0 and "Wrote 1 waiting request(s) to calendar-outbox.json" in run.stdout, run.stderr
    old = Outbox(str(tmp_path), 3600, 20, filename="calendar-outbox.json")
    assert [(x.id, x.sha256, x.raw, x.followup["op"], x.recipients) for x in old.pending()] == [(q.id, q.sha256, ICS, "create", ["jana@example.org"])]
    assert not (tmp_path / "imessage-outbox.json").exists()


# -- mail (package 6a) ------------------------------------------------------------------------------------------------------------
MAIL = b"From: me@icloud.com\r\nTo: jana@example.org\r\nSubject: Lunch\r\nMessage-ID: <m1@icloud.com>\r\n\r\nSee you at noon.\r\n"


@pytest.mark.parametrize("how", ["sent", "unclear", "discarded"])
def test_a_mail_key_outlives_the_activity_retention_as_a_tombstone(tmp_path, how):
    """protects=D6-3 and the review's tombstone: a saved draft whose mail ended is refused again after keep_days dropped its row,
    with the ending it had; fails_when=the same draft queues a second mail once the row is gone, or the tombstone forgets the
    ending; no_claim=mail's own answer to the agent (tests/test_mail_release.py)."""
    st = S.Store(str(tmp_path), "mail", 3600, 20, keep_days=1, tombstones=True)
    q = st.add(MAIL, ["jana@example.org"], {"folder": "Drafts", "uid": 4, "action": "trash"}, key="draft:Drafts:7:4", action_type="mail.send")
    if how == "discarded":
        st.claim(q.id, "discarded")
    else:
        st.claim(q.id, "executing")
        st.finish(q.id, how)
    st._db.execute("UPDATE request SET decided_at = ? WHERE id = ?", (time.time() - 2 * 86400, q.id))
    st._purged_at = 0
    st.pending()
    assert row(st, q.id) is None                                         # the activity retention dropped the row ...
    with pytest.raises(S.AlreadyDecided) as e:                            # ... and the key still answers
        st.add(MAIL + b" ", ["jana@example.org"], key="draft:Drafts:7:4", action_type="mail.send")
    assert (e.value.item_id, e.value.state) == (q.id, how) and st.pending() == []


def test_a_freed_mail_key_leaves_no_tombstone_and_other_stores_keep_none(tmp_path):
    """protects=a tombstone only for a key that was still bound (an expired mail may be asked for again), and only where asked for
    (external keeps package 3's rule); fails_when=an expired draft is refused for good, or the external source refuses a key after
    keep_days; no_claim=none beyond protects."""
    st = S.Store(str(tmp_path), "mail", 0, 20, keep_days=1, tombstones=True)
    q = st.add(MAIL, ["jana@example.org"], key="draft:Drafts:7:5")
    st.pending()                                                         # ttl 0: it expired
    st._db.execute("UPDATE request SET decided_at = ? WHERE id = ?", (time.time() - 2 * 86400, q.id))
    st._purged_at = 0
    st.pending()
    assert st._db.execute("SELECT COUNT(*) FROM key_tombstone").fetchone()[0] == 0
    st.ttl = 3600
    assert st.add(MAIL + b" ", ["jana@example.org"], key="draft:Drafts:7:5").id != q.id


def test_a_mail_queue_file_is_read_in_with_everything_it_kept(tmp_path):
    """protects=package 6a criterion 4: the first start keeps every waiting mail byte for byte with its id, hash, key, followup,
    edited_from and requested_by, and renames the file; fails_when=one of them changes or the file stays; no_claim=the server's
    wiring (tests/test_mail_release.py)."""
    old = Outbox(str(tmp_path), 3600, 20)
    a = old.add(MAIL, ["jana@example.org"], {"folder": "INBOX", "uid": 7, "flag": "\\Answered", "uidvalidity": 3}, requested_by="agent-a")
    b = old.add(MAIL + b"x", ["tom@example.net"], {"folder": "Drafts", "uid": 4, "action": "trash"}, key="draft:Drafts:7:4")
    b = old.replace(b.id, b.sha256, MAIL + b"y", edited_from="See you at one.")
    st = S.Store(str(tmp_path), "mail", 3600, 20, legacy="outbox.json", tombstones=True)
    want = [(x.id, x.sha256, x.raw, x.key, x.followup, x.edited_from, x.requested_by, x.recipients) for x in (a, b)]
    assert sorted((x.id, x.sha256, x.raw, x.key, x.followup, x.edited_from, x.requested_by, x.recipients) for x in st.pending()) == sorted(want)
    assert (tmp_path / "outbox.json.migrated").exists() and not (tmp_path / "outbox.json").exists()


def test_the_export_writes_waiting_mail_back_for_the_previous_version(tmp_path):
    """protects=package 6a criterion 4: a rollback keeps the waiting mail with its id, hash, bytes, key and followup in the file the
    previous version reads; fails_when=no outbox.json, another id, hash or byte, or a file written when no mail waits;
    no_claim=the previous version itself (its Outbox class reads the file here)."""
    st = S.Store(str(tmp_path), "mail", 3600, 20, tombstones=True)
    q = st.add(MAIL, ["jana@example.org"], {"folder": "Drafts", "uid": 4, "action": "trash"}, key="draft:Drafts:7:4", action_type="mail.send")
    st._db.close()
    os.close(S._locks.pop(str(Path(tmp_path).resolve())))
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    run = subprocess.run([sys.executable, "-m", "icloud_mcp.gate.export_queue", str(tmp_path)], capture_output=True, text=True, env=env, timeout=60)
    assert run.returncode == 0 and "Wrote 1 waiting request(s) to outbox.json" in run.stdout, run.stderr
    old = Outbox(str(tmp_path), 3600, 20)
    assert [(x.id, x.sha256, x.raw, x.key, x.followup) for x in old.pending()] == [(q.id, q.sha256, MAIL, q.key, q.followup)]
    empty = tmp_path / "empty"
    run = subprocess.run([sys.executable, "-m", "icloud_mcp.gate.export_queue", str(empty)], capture_output=True, text=True, env=env, timeout=60)
    assert run.returncode == 0 and not (empty / "outbox.json").exists()


def test_a_type_kept_in_the_row_only_is_written_for_new_and_read_in_requests_and_never_reported(tmp_path):
    """protects=package 6a writes each mail's type into the row (6b reads it; it outlasts the content), while the log,
    approval_status and the pages answer as before; fails_when=a row has no type, a request read back carries one, or a type
    function that raises stops the queueing; no_claim=mail's own mapping (tests/test_mail_release.py)."""
    old = Outbox(str(tmp_path), 3600, 20)
    a = old.add(MAIL, ["jana@example.org"], {"folder": "INBOX", "uid": 7, "flag": "$Forwarded"})
    kinds = {"$Forwarded": "mail.forward"}
    st = S.Store(str(tmp_path), "mail", 3600, 20, legacy="outbox.json", tombstones=True, report_type=False,
                 type_of=lambda q: kinds.get((q.followup or {}).get("flag"), "mail.send") if q.followup is not None else 1 / 0)
    b = st.add(MAIL + b"b", ["tom@example.net"], {"folder": "Drafts", "uid": 4, "action": "trash"})
    c = st.add(MAIL + b"c", ["tom@example.net"])                          # the type function raises: queued without a type
    assert b.action_type is None and [x.action_type for x in st.pending()] == [None, None, None]
    st.claim(b.id, "discarded")
    types = dict(st._db.execute("SELECT id, action_type FROM request").fetchall())
    assert types == {a.id: "mail.forward", b.id: "mail.send", c.id: None}
