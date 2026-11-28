"""Package 8 (docs/SENDGATE.md): signed checkpoints of the activity log, the export and dev/verify_log.py.

Made-up entries only (Jana, Tom, Sam at example.org)."""
from __future__ import annotations

import base64
import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

from icloud_mcp.gate import logproof as L
from icloud_mcp.gate.audit import AuditLog

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_log as V  # noqa: E402


def make(tmp_path, n=0, keep_days=90, **kw):
    a = AuditLog(str(tmp_path), keep_days, server_name="approvals.example.org", **kw)
    for k in range(n):
        a.record("tool.mail", actor="NORA", summary=f"Sent a mail to jana{k}@example.org")
    return a


def lines(tmp_path):
    return (tmp_path / "audit.jsonl").read_bytes().split(b"\n")[:-1]


def write_lines(tmp_path, ls):
    (tmp_path / "audit.jsonl").write_bytes(b"".join(x + b"\n" for x in ls))


# -- criterion 1: the signed-note vector and the checkpoint format --------------------------------------------------------------
VECTOR_KEY = "example.com/foo+530d903a+AekyeRrm56hApGFkyQR4ZCbV54Id2LKaANYcrnKv3U2k"
VECTOR_NOTE = ("This is an example message.\n\n— example.com/foo Uw2QOkn8srV1yJGh2VYRlL1Tnagv1YEq6TfXppzi2ONncAlTgK7Ztg1ERYNZXsYjOBH3"
               "mFXmRKuwHjG1Yu72IneyaQM=\n")


def test_signed_note_vector_from_the_spec_verifies():
    assert L.open_note(VECTOR_NOTE, VECTOR_KEY) == "This is an example message.\n"
    bad = VECTOR_NOTE.replace("example message", "example massage")
    with pytest.raises(ValueError):
        L.open_note(bad, VECTOR_KEY)


def test_checkpoint_parses_strictly_and_verifies(tmp_path):
    a = make(tmp_path, 3)
    cp = a.proof.latest()
    text = L.open_note(cp["note"], a.proof.vkey)
    origin, size, r = L.parse_checkpoint(text)
    assert origin == a.proof.origin and origin.startswith("approvals.example.org/icloud-mcp/log/")
    assert text.count("\n") == 3 and size == cp["size"] and len(r) == 32
    for wrong in ("x\n01\n" + text.split("\n")[2] + "\n", "x\n1\nAAAA\n", "x\n\n" + text.split("\n")[2] + "\n"):
        with pytest.raises(ValueError):
            L.parse_checkpoint(wrong)
    name, kid, pub = L.parse_verifier_key(a.proof.vkey)
    assert name == origin and kid == hashlib.sha256(origin.encode() + b"\n\x01" + pub).digest()[:4]


# -- criterion 2: roots by the definition --------------------------------------------------------------------------------------
def test_roots_for_sizes_0_to_9_match_rfc_9162():
    leaves = [f'{{"t":{k},"action":"x"}}'.encode() for k in range(10)]
    hashes = [L.leaf_hash(x) for x in leaves]
    assert L.mth([]) == hashlib.sha256(b"").digest()
    for n in range(10):
        assert L.root(L.leaves_range(0, leaves[:n])) == L.mth(hashes[:n]), n
    # the definition itself, spelled out for 3 leaves: k = 2
    assert L.mth(hashes[:3]) == L.node_hash(L.node_hash(hashes[0], hashes[1]), hashes[2])


def test_ranges_merge_into_the_compact_range():
    leaves = [str(k).encode() for k in range(23)]
    for m in range(24):
        merged = L.extend(L.leaves_range(0, leaves[:m]), L.leaves_range(m, leaves[m:]))
        assert merged == L.leaves_range(0, leaves)


# -- the log writes indexes and signs at most hourly --------------------------------------------------------------------------
def test_entries_carry_their_index_and_a_checkpoint_comes_hourly(tmp_path, monkeypatch):
    a = make(tmp_path, 3)
    got = [json.loads(x)["i"] for x in lines(tmp_path)]
    assert got == [0, 1, 2]
    assert a.proof.latest()["size"] == 1                   # the first entry signs at once, then not within the hour
    assert a.verify()["ok"]
    later = time.time() + L.EVERY + 1
    monkeypatch.setattr(a.proof, "clock", lambda: later)
    a.record("tool.mail", actor="NORA", summary="Sent a mail to tom@example.org")
    assert a.proof.latest()["size"] == 4


def test_a_new_checkpoint_goes_to_on_checkpoint_outside_the_lock(tmp_path):
    seen = []
    a = AuditLog(str(tmp_path), 90, server_name="approvals.example.org")
    a.on_checkpoint = lambda note, t: seen.append((note, a._lock.locked()))
    a.record("tool.mail", actor="NORA", summary="x")
    assert len(seen) == 1 and seen[0][1] is False and seen[0][0].startswith(a.proof.origin + "\n1\n")


# -- criterion 3: export and verify_log ----------------------------------------------------------------------------------------
def test_export_verifies_and_every_tamper_fails_with_its_index(tmp_path):
    a = make(tmp_path, 8)
    ex = a.export()
    r = V.verify(ex, a.proof.vkey)                              # the export's own checkpoint covers every entry
    assert r["size"] == len(ex["entries"]) and r["checkpoints"] >= 1

    def broken(change):
        e = json.loads(json.dumps(ex))
        change(e["entries"])
        with pytest.raises(V.Bad) as got:
            V.verify(e, a.proof.vkey)
        return got.value.at

    assert broken(lambda es: es.__setitem__(3, es[3].replace("jana3", "jana9"))) == 3          # one changed byte
    assert broken(lambda es: es.pop(4)) == 4                                                   # a removed line
    assert broken(lambda es: es.insert(2, es[5])) == 2                                         # an inserted line
    assert broken(lambda es: es.__setitem__(slice(5, 7), [es[6], es[5]])) == 5                 # two reordered
    with pytest.raises(V.Bad):
        V.verify(ex, "example.com/foo+530d903a+AekyeRrm56hApGFkyQR4ZCbV54Id2LKaANYcrnKv3U2k")  # another key


def test_a_rewritten_chain_is_still_caught_by_the_checkpoint(tmp_path):
    """Someone who rebuilds the hash chain after a change still cannot match the signed root."""
    from icloud_mcp.gate.audit import _GENESIS, _digest
    a = make(tmp_path, 5)
    ex = a.export()
    es = [json.loads(x) for x in ex["entries"]]
    es[2]["summary"] = "Sent a mail to sam@example.org"
    prev = _GENESIS
    out = []
    for e in es:
        e["prev"] = prev
        e["h"] = _digest(prev, {k: v for k, v in e.items() if k != "h"})
        prev = e["h"]
        out.append(json.dumps(e, ensure_ascii=False, separators=(",", ":")))
    ex["entries"] = out
    with pytest.raises(V.Bad) as got:
        V.verify(ex, a.proof.vkey)
    assert "checkpoint" in got.value.why


def test_verify_log_script_exit_codes(tmp_path, capsys):
    a = make(tmp_path, 4)
    f = tmp_path / "export.json"
    f.write_text(json.dumps(a.export()))
    assert V.main([str(f), "--key", a.proof.vkey]) == 0
    assert "OK: 4 entries" in capsys.readouterr().out
    ex = json.loads(f.read_text())
    ex["entries"][1] = ex["entries"][1].replace("jana1", "jana7")
    f.write_text(json.dumps(ex))
    assert V.main([str(f), "--key", a.proof.vkey]) == 1
    assert "FAILED at index 1" in capsys.readouterr().out


# -- criterion 4: checkpoints from before a prune -------------------------------------------------------------------------------
def test_a_checkpoint_from_before_a_prune_still_verifies(tmp_path, monkeypatch):
    t0 = time.time() - 10 * 86400
    clock = {"t": t0}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    a = AuditLog(str(tmp_path), keep_days=3, server_name="approvals.example.org")
    a.proof.clock = lambda: clock["t"]
    kept_notes = []
    for day in range(10):
        for k in range(3):
            clock["t"] = t0 + day * 86400 + k * 4000
            a._pruned_at = clock["t"]                           # prune only when the test says so
            a.record("tool.mail", actor="NORA", summary=f"day {day} mail to jana{k}@example.org")
        kept_notes.append(a.proof.latest()["note"])
    first_cp = kept_notes[7]                                    # day 7: its entries are still there after the prune below
    old_cp = kept_notes[1]                                      # day 1: its entries go; a receiver kept this one
    gone = a.proof.checkpoints()[4]                             # a checkpoint the server no longer has (taken out by hand)
    kept = [c for c in a.proof.checkpoints() if c["size"] != gone["size"]]
    (tmp_path / L.CHECKPOINTS_FILE).write_text("".join(json.dumps(c) + "\n" for c in kept))
    clock["t"] = t0 + 10 * 86400
    a._pruned_at = 0
    a.record("tool.mail", actor="NORA", summary="after the prune")
    first_i = json.loads(lines(tmp_path)[0])["i"]
    assert first_i > 20                                         # most of the log went
    ex = a.export()
    assert ex["base"]["index"] == first_i
    r = V.verify(ex, a.proof.vkey, first_cp)
    assert r["kept"] == L.parse_checkpoint(L.open_note(first_cp, a.proof.vkey))[1]
    # a kept checkpoint older than the first entry: checked through its prune proof
    older = [c for c in ex["checkpoints"] if c["size"] < first_i]
    assert older and all(c["proof"]["to"] == first_i for c in older)
    assert V.verify(ex, a.proof.vkey)["older"] == len(older)
    assert V.verify(ex, a.proof.vkey, old_cp)["kept"] < first_i  # the receiver's copy from before the prune still checks
    with pytest.raises(V.Bad) as got:                           # not on the server's list and older than the entries
        V.verify(ex, a.proof.vkey, gone["note"])
    assert "older than the first kept entry" in got.value.why


def test_a_stop_between_rewriting_and_the_tree_is_resolved_at_the_start(tmp_path, monkeypatch):
    a = make(tmp_path, 6)
    ls = lines(tmp_path)
    a.proof.before_prune(ls, 2)
    write_lines(tmp_path, ls[2:])                               # the file is rewritten, then the process stops
    b = AuditLog(str(tmp_path), 90, server_name="approvals.example.org")
    assert not b.proof.broken and b.proof.state["base"]["index"] == 2
    b.record("tool.mail", actor="NORA", summary="next")
    assert V.verify(b.export(), b.proof.vkey)["first"] == 2


# -- criterion 5: a changed log stops signing; a new tree needs a fresh sign-in ---------------------------------------------------
def test_a_changed_line_stops_signing_until_a_new_tree(tmp_path, monkeypatch):
    make(tmp_path, 4)
    ls = lines(tmp_path)
    ls[1] = ls[1].replace(b"jana1", b"jana8")
    write_lines(tmp_path, ls)
    b = AuditLog(str(tmp_path), 90, server_name="approvals.example.org")
    st = b.proof_status()
    assert st["broken"] and st["broken"]["at"] == 1
    later = time.time() + 2 * L.EVERY
    monkeypatch.setattr(b.proof, "clock", lambda: later)
    n = len(b.proof.checkpoints())
    b.record("tool.mail", actor="NORA", summary="more")
    assert len(b.proof.checkpoints()) == n                    # nothing more is signed
    assert any(json.loads(x)["action"] == "log.broken" for x in lines(tmp_path))
    old = b.proof.origin
    b.new_tree()
    assert b.proof.origin != old and not b.proof.broken
    b.record("tool.mail", actor="NORA", summary="after the new tree")
    assert V.verify(b.export(), b.proof.vkey)["entries"] == len(lines(tmp_path))
    assert (tmp_path / f"checkpoints-{old.rsplit('/', 1)[-1]}.jsonl").exists()


def test_a_removed_or_inserted_line_breaks_the_tree(tmp_path):
    for change in (lambda ls: ls.pop(2), lambda ls: ls.insert(1, ls[3])):
        d = tmp_path / str(id(change))
        d.mkdir()
        make(d, 5)
        ls = lines(d)
        change(ls)
        write_lines(d, ls)
        b = AuditLog(str(d), 90, server_name="approvals.example.org")
        assert b.proof_status()["broken"]


# -- criterion 6: catching up -----------------------------------------------------------------------------------------------------
def test_the_tree_catches_up_after_a_failed_tree_write(tmp_path, monkeypatch):
    a = make(tmp_path, 3)
    real = L._write

    def full(path, data):
        if Path(path).name == L.TREE_FILE:
            raise OSError(28, "No space left on device")
        return real(path, data)
    monkeypatch.setattr(L, "_write", full)
    a.record("tool.mail", actor="NORA", summary="written while the tree could not be saved")
    a.record("tool.mail", actor="NORA", summary="and another")
    monkeypatch.setattr(L, "_write", real)
    assert json.loads((tmp_path / L.TREE_FILE).read_text())["size"] == 3
    b = AuditLog(str(tmp_path), 90, server_name="approvals.example.org")
    assert not b.proof.broken and b.proof.size >= 5
    assert any(json.loads(x)["action"] == "log.caught_up" for x in lines(tmp_path))
    assert V.verify(b.export(), b.proof.vkey)["size"] == len(lines(tmp_path))


def test_an_existing_log_becomes_the_start_of_the_tree(tmp_path):
    a = AuditLog(str(tmp_path), 90, proof=False)
    for k in range(3):
        a.record("tool.mail", actor="NORA", summary=f"before package 8, {k}")
    assert all("i" not in json.loads(x) for x in lines(tmp_path))
    b = AuditLog(str(tmp_path), 90, server_name="approvals.example.org")
    assert b.proof.size == 3 and b.proof.state["legacy_until"] == 3
    b.record("tool.mail", actor="NORA", summary="the first with an index")
    assert json.loads(lines(tmp_path)[-1])["i"] == 3
    assert V.verify(b.export(), b.proof.vkey)["entries"] == 4


# -- criterion 7: the key never leaves -----------------------------------------------------------------------------------------
def test_the_log_key_appears_in_no_output(tmp_path, caplog, monkeypatch):
    a = make(tmp_path, 3)
    seed = json.loads((tmp_path / L.KEY_FILE).read_text())["seed"]
    assert oct((tmp_path / L.KEY_FILE).stat().st_mode & 0o777) == "0o600"
    raw = base64.b64decode(seed)
    outputs = [json.dumps(a.proof_status()), json.dumps(a.export()), (tmp_path / "audit.jsonl").read_text(),
               (tmp_path / L.CHECKPOINTS_FILE).read_text(), (tmp_path / L.TREE_FILE).read_text(), caplog.text]
    monkeypatch.setattr(L, "_write", lambda *a_, **k: (_ for _ in ()).throw(OSError("disk")))
    a.record("tool.mail", actor="NORA", summary="an error path")
    outputs.append(caplog.text)
    for out in outputs:
        assert seed not in out and raw.hex() not in out and "seed" not in out


# -- criterion 9: rollback ------------------------------------------------------------------------------------------------------
def test_the_previous_version_keeps_logging_on_the_same_data_dir(tmp_path):
    make(tmp_path, 3)
    old = AuditLog(str(tmp_path), 90, proof=False)             # the reader of before: by key, ignores i and the new files
    old.record("tool.mail", actor="NORA", summary="written by the previous version")
    assert old.verify()["ok"] and len(old.entries()) == 4
    back = AuditLog(str(tmp_path), 90, server_name="approvals.example.org")
    assert back.proof_status()["broken"]                       # a line without its index: the owner starts a new tree (D8-7)


def test_log_off_makes_no_tree(tmp_path):
    a = AuditLog(str(tmp_path), 90, enabled=False)
    a.record("tool.mail", actor="NORA", summary="x")
    assert a.proof is None and a.proof_status() == {"on": False}
    assert not (tmp_path / L.KEY_FILE).exists()


# -- criterion 8: checkpoints as notices, only to receivers that opted in ---------------------------------------------------------
def test_a_checkpoint_notice_goes_only_to_receivers_that_opted_in_and_carries_only_the_checkpoint(tmp_path, monkeypatch):
    import test_notices as T
    clock, st, svc, rx = T.setup(tmp_path, monkeypatch, hosts=(T.HOST, "other.example.net"))
    yes, secret = svc.add(T.URL)
    svc.add("https://other.example.net/hook")
    assert svc.loosens(yes["id"], checkpoints=True)                       # turning it on lets more out: a fresh sign-in
    with pytest.raises(T.N.NeedsConfirm):
        svc.change(yes["id"], may_loosen=False, checkpoints=True)
    shown, _ = svc.change(yes["id"], checkpoints=True)
    assert shown["checkpoints"] is True and not svc.loosens(yes["id"], checkpoints=False)
    a = AuditLog(str(tmp_path), 90, server_name="approvals.example.org")
    a.on_checkpoint = svc.offer_checkpoint
    a.record("tool.mail", actor="NORA", summary="Sent a mail to jana@example.org")
    first = T.add(st, "tom")                                              # a decision still goes to both, with its cursor
    st.claim(first.id, "discarded")
    svc.run_once()
    to_yes = [json.loads(c["body"]) for c in rx.calls if c["host"] == T.HOST]
    to_no = [json.loads(c["body"]) for c in rx.calls if c["host"] != T.HOST]
    cps = [b for b in to_yes if b["type"] == "log.checkpoint"]
    assert len(cps) == 1 and set(cps[0]) == {"type", "timestamp", "data"} and set(cps[0]["data"]) == {"checkpoint"}
    assert cps[0]["data"]["checkpoint"] == a.proof.latest()["note"]
    assert "jana" not in json.dumps(cps) and "summary" not in json.dumps(cps)
    assert all(b["type"] != "log.checkpoint" for b in to_no) and any(b["data"].get("id") == first.id for b in to_no)
    call = next(c for c in rx.calls if json.loads(c["body"])["type"] == "log.checkpoint")
    assert T.verify(secret, call["headers"], call["body"], clock.now)    # signed like any notice
    dec = [b for b in to_yes if b["type"] == "approval.status"][-1]
    assert dec["data"]["cursor"] == f"e{st.event_head()}"                # a checkpoint row never holds the cursor back


def test_a_checkpoint_notice_that_fails_goes_dead_and_is_sent_again(tmp_path, monkeypatch):
    import test_notices as T
    clock, st, svc, rx = T.setup(tmp_path, monkeypatch, 500)
    r, _ = svc.add(T.URL)
    svc.change(r["id"], checkpoints=True)
    assert svc.offer_checkpoint("o\n1\n" + base64.b64encode(b"x" * 32).decode() + "\n\n— o AAAA\n", clock.now) == 1
    for wait in (0,) + T.N.BACKOFF[1:]:
        clock.tick(wait)
        svc.run_once()
    assert [x["state"] for x in T.rows(svc)] == ["dead"]
    rx.answers = [200]
    assert svc.resend(r["id"]) == (1, 0)
    svc.run_once()
    assert [x["state"] for x in T.rows(svc)] == ["delivered"]
    clock.tick(100 * 86400)
    svc._retained_at = 0
    svc.retain()
    assert svc._db.execute("SELECT COUNT(*) FROM log_notice").fetchone()[0] == 0
