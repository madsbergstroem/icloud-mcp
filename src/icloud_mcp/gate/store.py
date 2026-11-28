"""The approval store: requests in one SQLite file, DATA_DIR/approvals.db, with an explicit state for each (docs/SENDGATE.md,
package 3). It has the methods of the JSON queue (outbox.py): add, pending, claim, replace, restore, on_new, on_expiry, so a source
uses either. It holds the external source, iMessage (package 4a), calendar changes (package 5a) and mail (package 6a).

States, and who moves a request between them:

    waiting   -> executing      the owner approved (claim(id, "executing"); attempts goes up by one)
    executing -> sent | unclear the executor answered (finish)
    executing -> waiting        checked again before sending, the target had changed: a new version with the service's values
                                waits instead (revise, package 2c); nothing was sent
    executing -> discarded      a recheck that answers only a code (finish, reason target_changed)
    executing -> waiting        the executor answered failed: it provably never went out (restore); past its expiry it ends expired
    waiting   -> discarded | withdrawn | expired    the owner, the agent that asked, or the clock
    waiting   -> discarded      written back to the JSON file for a rollback (reason exported; its key is freed)

A request that ends keeps its id, source, type, state, times, hash, key, fingerprint and reason code, and loses its content (raw,
recipients, followup, edited_from become NULL). Ended rows are deleted after keep_days, the span the activity log keeps.

Versions (package 2c): every content a request has had is a row of `version` (request, n from 1, time, author, sha256, raw).
The agent's request is version 1 (author assistant), the owner's edit a new one (owner), a recheck that found other values at the
service one more (service), and an agent's revision with `replaces` another (assistant). The owner's edits in a row are one
version (it follows the latest edit), and going back to the text before them removes it; every other version never changes.
The request row holds the newest, and only its sha256 can be approved. When a request ends its versions keep author, time and hash and lose raw.
Store version 2 adds the table; a version 1 file gets one version per request from what its row holds.

What else holds:

- One process per DATA_DIR: an exclusive lock on DATA_DIR/approvals.lock is taken when the first store opens and held for the
  life of the process. A second process refuses to start and names the lock. Only that first open recovers rows left in
  `executing` by a crash, of every source: they become `unclear` (reason interrupted; it may have gone out; never repeated). A second store on
  the same DATA_DIR in the same process never touches rows the first is carrying out.
- Expiry is lazy: every read and write first ends what has timed out. Each expired or recovered request is reported exactly
  once through on_expiry / on_recovered; the `reported` column remembers it, so a report that could not be made (nothing was
  listening yet, or the process stopped) is made when something listens, also after a restart.
- A key (the agent's request_id) of the external source is unique among its rows and stays bound while its request waits, runs,
  was sent or is unclear, and after the owner discarded it or the agent withdrew it: asking again answers with that request
  (AlreadyDecided). It is freed after `expired`, after a discard with reason `target_changed` and by an export, so the agent can
  ask again. The same key with other parameters (by fingerprint) is a conflict.
- A store opened with tombstones=True (mail, package 6a: a key names one saved draft) keeps a bound key beyond keep_days: when an
  ended row is dropped, its key, fingerprint, ending and time stay in `key_tombstone`, and asking again with that key answers as
  if the row were still there (AlreadyDecided). Tombstones are not dropped with the activity retention.
- Blobs (package 6b): what does not fit a request's canonical JSON (a mail's attachments, a large body) is kept in `blob`, by its
  sha256. A type stages a blob before it names it (stage), and every version that names one is a row of `blob_ref`, which the
  store works out from the version's bytes (refs_of, set by the external source). A blob goes, in the same transaction, once
  nothing names it any more: its last version ended, was dropped or was written over. A staged blob nothing named within an hour
  goes too.
- The status ledger (package 9a): every change of a request's status is a row of `status_event` (seq, request, kind, action
  type, status, reason code, edited, time, reconciled), written in the transaction that makes the change, so a change that is
  rolled back leaves no event and no event exists without its change. The statuses are approval_status's words: waiting (reason
  queued, or target_changed after a recheck, package 2c), sent, unclear (reason interrupted after a crash), discarded (reason
  target_changed, not_allowed or exported), withdrawn, expired. Claiming a request for execution, a release that failed and the
  owner's edits change no status and write none. An event holds metadata only, never content or the agent's label, and never
  changes. An expiry's event is written when the store notices it (the lazy expiry above) and carries the time it ran out. A
  version without the ledger changes states without events (a rollback): the first open compares each request's state with
  its last event and writes the missing ones, marked reconciled. The table is new beside the others; the store version stays 2.
- The database and its WAL files are mode 600; a database file others can read stops the start, and so does one written by a
  newer version (PRAGMA user_version).
- On first start the source's JSON queue file is read into the table and renamed `<name>.migrated`, row by row: an id already
  in the table is not imported again, unless it was exported, when the file's copy is authoritative; a key bound to another row
  is freed when that row has ended and dropped (reported as `problem`) when it still waits. A file that cannot be read is renamed
  `<name>.corrupt-<time>`, never sent, and reported as `problem`. `python -m icloud_mcp.gate.export_queue DATA_DIR` writes the
  waiting rows back for a rollback.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import replace as dc_replace
from pathlib import Path
from typing import Any

from .. import callctx
from .outbox import OutboxFull, QueuedMessage

log = logging.getLogger(__name__)

DB_FILE, LOCK_FILE = "approvals.db", "approvals.lock"
VERSION = 2                                  # PRAGMA user_version this code writes and reads (2: the version table)
AUTHORS = ("assistant", "owner", "service")
STATES = ("waiting", "executing", "sent", "unclear", "discarded", "withdrawn", "expired")

SCHEMA = """
CREATE TABLE IF NOT EXISTS request (
  id              TEXT PRIMARY KEY,
  source          TEXT NOT NULL,
  action_type     TEXT,
  state           TEXT NOT NULL CHECK (state IN ('waiting','executing','sent','unclear','discarded','withdrawn','expired')),
  created_at      REAL NOT NULL,
  expires_at      REAL NOT NULL,
  decided_at      REAL,
  sha256          TEXT NOT NULL,
  key             TEXT,
  key_fingerprint TEXT,
  requested_by    TEXT,
  attempts        INTEGER NOT NULL DEFAULT 0,
  outcome_reason  TEXT,
  reported        INTEGER NOT NULL DEFAULT 0,
  raw             BLOB,
  recipients      TEXT,
  followup        TEXT,
  edited_from     TEXT
);
CREATE INDEX IF NOT EXISTS request_waiting ON request (source, state, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS request_key_external ON request (key) WHERE source = 'external' AND key IS NOT NULL;
CREATE TABLE IF NOT EXISTS version (
  request_id  TEXT NOT NULL,
  n           INTEGER NOT NULL,
  created_at  REAL NOT NULL,
  author      TEXT NOT NULL CHECK (author IN ('assistant','owner','service')),
  sha256      TEXT NOT NULL,
  raw         BLOB,
  PRIMARY KEY (request_id, n)
);
CREATE TABLE IF NOT EXISTS blob (
  sha256     TEXT PRIMARY KEY,
  data       BLOB NOT NULL,
  staged_at  REAL
);
CREATE TABLE IF NOT EXISTS blob_ref (
  request_id  TEXT NOT NULL,
  n           INTEGER NOT NULL,
  sha256      TEXT NOT NULL,
  PRIMARY KEY (request_id, n, sha256)
);
CREATE TABLE IF NOT EXISTS key_tombstone (
  source          TEXT NOT NULL,
  key             TEXT NOT NULL,
  request_id      TEXT NOT NULL,
  key_fingerprint TEXT,
  state           TEXT NOT NULL,
  decided_at      REAL,
  PRIMARY KEY (source, key)
);
CREATE TABLE IF NOT EXISTS status_event (
  seq         INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id  TEXT NOT NULL,
  kind        TEXT NOT NULL,
  action_type TEXT,
  status      TEXT NOT NULL CHECK (status IN ('waiting','sent','unclear','discarded','withdrawn','expired')),
  reason      TEXT,
  edited      INTEGER NOT NULL DEFAULT 0,
  at          REAL NOT NULL,
  reconciled  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS status_event_request ON status_event (request_id, seq);
CREATE TRIGGER IF NOT EXISTS status_event_unchanged BEFORE UPDATE ON status_event
BEGIN SELECT RAISE(ABORT, 'a status event never changes'); END;
"""
# what keeps a key bound after its request ended; anything else frees it
_FREEING = "state = 'expired' OR (state = 'discarded' AND outcome_reason IN ('target_changed', 'exported'))"

_locks: dict[str, int] = {}                  # DATA_DIR -> the open lock file this process holds
_locks_guard = threading.Lock()


class StoreUnavailable(Exception):
    """The store cannot take a request right now (a full disk, a database busy for too long). Nothing was queued."""


class AlreadyDecided(Exception):
    """A key whose request has ended (or is being carried out right now) was used again: nothing is queued."""

    def __init__(self, item_id: str, state: str):
        super().__init__(f"This request_id belongs to a request that already ended ({state}). Nothing was queued.")
        self.item_id, self.state = item_id, state


class KeyConflict(Exception):
    """The key is bound to a request with other parameters."""


class MissingBlob(StoreUnavailable):
    """A version names a blob the store does not hold (it was never staged, or was dropped before it was named)."""


def _hold_lock(data_dir: Path) -> bool:
    """Take DATA_DIR's lock for this process. True the first time (this open recovers crashed rows), False when this process
    already holds it. Another process holding it stops the start with the lock's path."""
    key = str(data_dir.resolve())
    with _locks_guard:
        if key in _locks:
            return False
        path = data_dir / LOCK_FILE
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise RuntimeError(f"Another server process is using {data_dir} (it holds {path}). Stop it first; two servers on one "
                               "DATA_DIR could send a request twice.") from None
        _locks[key] = fd
        return True


def fingerprint(raw_params: Any) -> str:
    return hashlib.sha256(json.dumps(raw_params, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class Store:
    keeps_keys = True                        # a key stays bound after its request ended (external.py asks for the fingerprint)

    def __init__(self, data_dir: str, source: str, ttl: int, max_items: int, keep_days: int = 90, legacy: str | None = None,
                 type_of: Any = None, tombstones: Any = False, report_type: bool = True):
        """source: which kind of request this store holds (external, imessage, calendar, mail). legacy: the JSON queue file to read
        in once. type_of(q): the action type of a request read in from that file or queued without one (package 5a: the
        calendar's operation; 6a: mail's kind), so it outlasts the request's content. tombstones: keep bound keys after their rows
        are dropped (package 6a); a collection of action types keeps only the keys of rows of those types (the external source
        since 6b: mail's keys, not those of the actions agents ask for). report_type=False keeps the type in the row only: the
        requests read back carry none, so what the log, approval_status and the pages say stays as before (mail in 6a; 6b
        reports it). refs_of(raw), set later by the external source: the blobs a version's bytes name."""
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.source, self.ttl, self.max_items, self.keep_days = source, ttl, max_items, max(1, keep_days)
        self.path = self.dir / DB_FILE
        self.problem = ""                    # what health reports: a queue file that could not be read, an entry that was dropped
        self.on_new: Any = None
        self._on_expired: Any = None
        self._on_recovered: Any = None
        self._lock = threading.RLock()
        self._purged_at = 0.0
        self.type_of = type_of
        self.tombstones = tombstones if isinstance(tombstones, bool) else tuple(tombstones)
        self.report_type = report_type
        self.refs_of: Any = None
        first = _hold_lock(self.dir)
        self._db = self._open()
        if first:
            self._reconcile()                # before the recovery, which writes its own events
            self._recover()
        if legacy:
            self._migrate(self.dir / legacy)

    # -- opening ---------------------------------------------------------------------------------------------------------
    def _open(self) -> sqlite3.Connection:
        if self.path.exists():
            if self.path.stat().st_mode & 0o077:
                raise RuntimeError(f"{self.path} can be read by other users of this machine. Run chmod 600 on it, then start again.")
        else:
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=5.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        have = db.execute("PRAGMA user_version").fetchone()[0]
        if have > VERSION:
            raise RuntimeError(f"{self.path} was written by a newer version of this server (store version {have}). Start that version, "
                               "or run its export before going back.")
        db.executescript(SCHEMA)
        if have == 1:                                 # store version 1: one version per request, from what its row holds
            db.execute("INSERT OR IGNORE INTO version (request_id, n, created_at, author, sha256, raw) "
                       "SELECT id, 1, created_at, CASE WHEN edited_from IS NULL THEN 'assistant' ELSE 'owner' END, sha256, raw FROM request")
        db.execute(f"PRAGMA user_version = {VERSION}")
        for suffix in ("-wal", "-shm"):
            side = Path(f"{self.path}{suffix}")
            if side.exists():
                os.chmod(side, 0o600)
        return db

    def _tx(self) -> "_Tx":
        return _Tx(self._db)

    # -- rows ------------------------------------------------------------------------------------------------------------
    def _q(self, r: sqlite3.Row) -> QueuedMessage:
        raw = r["raw"] if r["raw"] is not None else b""
        return QueuedMessage(r["id"], r["created_at"], r["expires_at"], bytes(raw), json.loads(r["recipients"] or "[]"),
                             json.loads(r["followup"]) if r["followup"] else None, r["sha256"], r["key"], r["edited_from"],
                             r["requested_by"], r["action_type"] if self.report_type else None, attempts=r["attempts"])

    def _row(self, item_id: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM request WHERE id = ? AND source = ?", (item_id, self.source)).fetchone()

    def state_of(self, item_id: str) -> tuple[str, str | None] | None:
        """(state, outcome_reason) of a request, or None when there is no such row."""
        with self._lock:
            r = self._row(item_id)
            return (r["state"], r["outcome_reason"]) if r is not None else None

    def _end(self, item_id: str, state: str, reason: str | None = None) -> None:
        """Inside a transaction: end a request, keep what says what it was, drop its content, and write its status event (edited
        is read before edited_from goes; an expiry carries the time it ran out)."""
        r = self._db.execute("SELECT source, action_type, expires_at, edited_from FROM request WHERE id = ?", (item_id,)).fetchone()
        now = time.time()
        self._db.execute("UPDATE request SET state = ?, decided_at = ?, outcome_reason = ?, raw = NULL, recipients = NULL, "
                         "followup = NULL, edited_from = NULL WHERE id = ?", (state, now, reason, item_id))
        if r is not None:
            self._event(item_id, state, reason, edited=r["edited_from"] is not None, at=r["expires_at"] if state == "expired" else now, r=r)
        self._db.execute("UPDATE version SET raw = NULL WHERE request_id = ?", (item_id,))
        self._unref(item_id)

    def _version(self, item_id: str, sha: str, raw: bytes, author: str) -> int:
        """Inside a transaction: add the next version of a request. Returns its number."""
        n = self._db.execute("SELECT COALESCE(MAX(n), 0) + 1 FROM version WHERE request_id = ?", (item_id,)).fetchone()[0]
        self._db.execute("INSERT INTO version (request_id, n, created_at, author, sha256, raw) VALUES (?,?,?,?,?,?)",
                         (item_id, n, time.time(), author, sha, raw))
        self._ref(item_id, n, raw)
        return n

    # -- the status ledger (package 9a) ------------------------------------------------------------------------------------
    def _event(self, item_id: str, status: str, reason: str | None = None, *, edited: bool = False, at: float | None = None,
               reconciled: bool = False, r: Any = None) -> None:
        """Inside a transaction: add a status event for a request. r: its row (source and action_type), when the caller has it."""
        if r is None:
            r = self._db.execute("SELECT source, action_type FROM request WHERE id = ?", (item_id,)).fetchone()
        self._db.execute("INSERT INTO status_event (request_id, kind, action_type, status, reason, edited, at, reconciled) "
                         "VALUES (?,?,?,?,?,?,?,?)", (item_id, r["source"], r["action_type"] if self.report_type else None, status, reason,
                                                      int(bool(edited)), time.time() if at is None else at, int(reconciled)))

    def _reconcile(self) -> None:
        """A version without the ledger (before 9a, or one a rollback went back to) changed states and wrote no events: give each
        request whose last event does not say its state the event it lacks, marked reconciled. An ending is compared with its
        reason too: an export's discard (reason exported) is not the owner's discard of the same request after a rollback read it in
        again. An ending's edited is what the ledger last recorded for the request (edited_from, as _end reads it, is gone from the
        row by then); an edit made while the previous version ran is not known here, and the log still answers it."""
        with self._lock, self._tx():
            rows = self._db.execute("SELECT r.*, e.status AS last, e.reason AS last_reason, e.edited AS last_edited FROM request r LEFT JOIN status_event e ON "
                                    "e.seq = (SELECT MAX(seq) FROM status_event WHERE request_id = r.id)").fetchall()
            n = 0
            for r in rows:
                status = "waiting" if r["state"] in ("waiting", "executing") else r["state"]
                if r["last"] == status and (status == "waiting" or r["last_reason"] == r["outcome_reason"]):
                    continue
                if status == "waiting":
                    self._event(r["id"], status, edited=r["edited_from"] is not None, at=r["created_at"], reconciled=True, r=r)
                else:
                    self._event(r["id"], status, r["outcome_reason"], edited=r["edited_from"] is not None or bool(r["last_edited"]),
                                at=r["expires_at"] if status == "expired" else (r["decided_at"] or time.time()), reconciled=True, r=r)
                n += 1
        if n:
            log.info("approval store: %d status event(s) written for changes made without the ledger", n)

    def last_event(self, item_id: str) -> dict[str, Any] | None:
        """The newest status event of a request, or None when it has none."""
        with self._lock:
            r = self._db.execute("SELECT * FROM status_event WHERE request_id = ? ORDER BY seq DESC LIMIT 1", (item_id,)).fetchone()
        return dict(r) if r is not None else None

    def events_after(self, seq: int, upto: int, decided: bool = True) -> list[dict[str, Any]]:
        """The status events after seq up to upto, oldest first; decided: only those that end a request, which an export's discard
        does not (the request goes on in the file, and a read-in makes it wait again)."""
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT * FROM status_event WHERE seq > ? AND seq <= ?"
                                                      + (" AND status != 'waiting' AND COALESCE(reason, '') != 'exported'" if decided else "")
                                                      + " ORDER BY seq", (seq, upto))]

    def event_head(self) -> int:
        """The newest event's seq, 0 before the first (a seq is never used twice, also after its event was dropped)."""
        with self._lock:
            r = self._db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'status_event'").fetchone()
        return int(r[0]) if r is not None else 0

    # -- blobs (package 6b) -----------------------------------------------------------------------------------------------
    def stage(self, data: bytes) -> str:
        """Keep `data` as a blob a version is about to name; returns its sha256. Nothing names it yet: unnamed for an hour, it
        goes. Inside the store's own transaction (a conversion while adopt runs) it joins that transaction."""
        sha = hashlib.sha256(data).hexdigest()
        with self._lock:
            if self._db.in_transaction:
                self._put_blob(sha, data)
            else:
                with self._tx():
                    self._put_blob(sha, data)
        return sha

    def _put_blob(self, sha: str, data: bytes) -> None:
        self._db.execute("INSERT OR IGNORE INTO blob (sha256, data, staged_at) VALUES (?, ?, ?)", (sha, data, time.time()))
        self._db.execute("UPDATE blob SET staged_at = ? WHERE sha256 = ? AND staged_at IS NOT NULL", (time.time(), sha))

    def blob(self, sha: str) -> bytes | None:
        """A blob's bytes, or None when the store does not hold it."""
        with self._lock:
            r = self._db.execute("SELECT data FROM blob WHERE sha256 = ?", (sha,)).fetchone()
        return bytes(r["data"]) if r is not None else None

    def _ref(self, item_id: str, n: int, raw: bytes | None) -> None:
        """Inside a transaction: the blobs version n names, from its bytes; each must be held (MissingBlob otherwise). The blobs it
        named before and names no more go when nothing else names them."""
        self._db.execute("DELETE FROM blob_ref WHERE request_id = ? AND n = ?", (item_id, n))
        shas: set[str] = set()
        if raw is not None and self.refs_of is not None:
            try:
                shas = {str(x) for x in self.refs_of(bytes(raw))}
            except Exception:  # noqa: BLE001  -- bytes no type can read name nothing
                shas = set()
        for sha in sorted(shas):
            if not self._db.execute("SELECT 1 FROM blob WHERE sha256 = ?", (sha,)).fetchone():
                raise MissingBlob(sha)
            self._db.execute("INSERT OR IGNORE INTO blob_ref (request_id, n, sha256) VALUES (?, ?, ?)", (item_id, n, sha))
            self._db.execute("UPDATE blob SET staged_at = NULL WHERE sha256 = ?", (sha,))
        self._gc()

    def _unref(self, item_id: str, n: int | None = None) -> None:
        """Inside a transaction: the versions of a request (all, or version n) name no blob any more."""
        if n is None:
            self._db.execute("DELETE FROM blob_ref WHERE request_id = ?", (item_id,))
        else:
            self._db.execute("DELETE FROM blob_ref WHERE request_id = ? AND n = ?", (item_id, n))
        self._gc()

    def _gc(self) -> None:
        """Inside a transaction: drop every blob nothing names, and staged ones nothing named within an hour."""
        self._db.execute("DELETE FROM blob WHERE sha256 NOT IN (SELECT sha256 FROM blob_ref) AND "
                         "(staged_at IS NULL OR staged_at < ?)", (time.time() - 3600,))

    def versions(self, item_id: str) -> list[dict[str, Any]]:
        """Every version of a request, oldest first: n, created_at, author, sha256, raw (None once the request ended)."""
        with self._lock:
            return [{"n": r["n"], "created_at": r["created_at"], "author": r["author"], "sha256": r["sha256"],
                     "raw": bytes(r["raw"]) if r["raw"] is not None else None}
                    for r in self._db.execute("SELECT * FROM version WHERE request_id = ? ORDER BY n", (item_id,))]

    # -- reports that must happen once ------------------------------------------------------------------------------------
    _KINDS = {"expired": "state = 'expired'", "recovered": "state = 'unclear' AND outcome_reason = 'interrupted'"}

    def _report(self, kind: str) -> None:
        """Hand every unreported expired (or recovered) request to its callback once, and remember that it was."""
        callback = self._on_expired if kind == "expired" else self._on_recovered
        if callback is None:
            return
        with self._lock:
            rows = [self._q(r) for r in self._db.execute(f"SELECT * FROM request WHERE source = ? AND reported = 0 AND {self._KINDS[kind]}",
                                                         (self.source,))]
        for q in rows:
            try:
                callback(q)
            except Exception:  # noqa: BLE001  -- a failed log line must never break the queue; it is tried again next time
                continue
            with self._lock, self._tx():
                self._db.execute("UPDATE request SET reported = 1 WHERE id = ?", (q.id,))

    def on_expiry(self, callback: Any) -> None:
        """Call back once with each request that timed out, also those that timed out before this was set or before a restart."""
        self._on_expired = callback
        self._report("expired")

    def on_recovered(self, callback: Any) -> None:
        """Call back once with each request found `executing` at start: it may have gone out, and it is never repeated."""
        self._on_recovered = callback
        self._report("recovered")

    def _recover(self) -> None:
        with self._lock, self._tx():
            # every source: only the first store of the process gets here, and the others open later on the same file
            rows = [r["id"] for r in self._db.execute("SELECT id FROM request WHERE state = 'executing'")]
            for item_id in rows:
                self._end(item_id, "unclear", "interrupted")
        if rows:
            log.warning("%d request(s) were being carried out when the server stopped; they are unclear and never repeated", len(rows))

    # -- the clock -------------------------------------------------------------------------------------------------------
    def _expire(self) -> None:
        """End what has timed out, and now and then drop ended rows older than keep_days. A store that cannot write right now
        tries again on the next call; nothing waiting is sent because of it."""
        now = time.time()
        try:
            with self._lock:
                with self._tx():
                    gone = [r["id"] for r in self._db.execute(
                        "SELECT id FROM request WHERE source = ? AND state = 'waiting' AND expires_at <= ?", (self.source, now))]
                    for item_id in gone:
                        self._end(item_id, "expired")
                if now - self._purged_at > 3600:
                    self._purged_at = now
                    with self._tx():
                        if self.tombstones:              # a key still bound outlives its row (D6-3)
                            kinds = "" if self.tombstones is True else f" AND action_type IN ({','.join('?' * len(self.tombstones))})"
                            self._db.execute("INSERT OR REPLACE INTO key_tombstone (source, key, request_id, key_fingerprint, state, decided_at) "
                                             "SELECT source, key, id, key_fingerprint, state, decided_at FROM request WHERE source = ? AND "
                                             f"state NOT IN ('waiting', 'executing') AND decided_at < ? AND key IS NOT NULL AND NOT COALESCE(({_FREEING}), 0)"
                                             + kinds, (self.source, now - self.keep_days * 86400,
                                                       *(() if self.tombstones is True else self.tombstones)))
                        self._db.execute("DELETE FROM request WHERE source = ? AND state NOT IN ('waiting', 'executing') AND decided_at < ?",
                                         (self.source, now - self.keep_days * 86400))
                        self._db.execute("DELETE FROM version WHERE request_id NOT IN (SELECT id FROM request)")
                        self._db.execute("DELETE FROM blob_ref WHERE request_id NOT IN (SELECT id FROM request)")
                        self._db.execute("DELETE FROM status_event WHERE request_id NOT IN (SELECT id FROM request)")
                        self._gc()
        except sqlite3.OperationalError as e:
            log.warning("approval store: expiry skipped this time (%s)", type(e).__name__)
            return
        if gone:
            self._report("expired")

    # -- the queue's operations ---------------------------------------------------------------------------------------------
    def add(self, raw: bytes, recipients: list[str], followup: dict[str, Any] | None = None, key: str | None = None,
            approval_page: str = "the dashboard's Approvals page", requested_by: str | None = None,
            action_type: str | None = None, key_fingerprint: str | None = None, notify: Any = True) -> QueuedMessage:
        """Queue a request, or return the one already waiting for the same bytes or the same key. Raises AlreadyDecided for a key
        whose request is being carried out or ended and is still bound, KeyConflict for a bound key with other parameters,
        OutboxFull, StoreUnavailable. notify: True calls on_new for a new request (the owner's push), a function is called in its
        place (gate.submit, package 10: the push waits until it is known that the request waits), False calls nothing."""
        sha = hashlib.sha256(raw).hexdigest()
        try:
            self._expire()
            with self._lock, self._tx():
                same = self._db.execute("SELECT * FROM request WHERE source = ? AND state = 'waiting' AND sha256 = ?", (self.source, sha)).fetchone()
                if same is not None:
                    return self._q(same)
                if key is not None:
                    old = self._db.execute("SELECT * FROM request WHERE source = ? AND key = ?", (self.source, key)).fetchone()
                    if old is not None:
                        freed = self._db.execute(f"SELECT 1 FROM request WHERE id = ? AND ({_FREEING})", (old["id"],)).fetchone()
                        if not freed:
                            if key_fingerprint and old["key_fingerprint"] and key_fingerprint != old["key_fingerprint"]:
                                raise KeyConflict()
                            if old["state"] == "waiting":
                                return self._q(old)
                            raise AlreadyDecided(old["id"], old["state"])
                        self._db.execute("UPDATE request SET key = NULL WHERE id = ?", (old["id"],))
                    elif (gone := self._db.execute("SELECT * FROM key_tombstone WHERE source = ? AND key = ?", (self.source, key)).fetchone()):
                        if key_fingerprint and gone["key_fingerprint"] and key_fingerprint != gone["key_fingerprint"]:
                            raise KeyConflict()
                        raise AlreadyDecided(gone["request_id"], gone["state"])
                waiting = self._db.execute("SELECT COUNT(*) FROM request WHERE source = ? AND state = 'waiting'", (self.source,)).fetchone()[0]
                if waiting >= self.max_items:
                    raise OutboxFull(f"{waiting} messages are already waiting for owner approval (OUTBOX_MAX={self.max_items}). "
                                     "Nothing was queued. Do not retry: tell the owner to review the queue on "
                                     f"{approval_page}, where they can send or discard what is waiting.")
                now = time.time()
                label = (requested_by if requested_by is not None else callctx.client()) or None
                q = QueuedMessage(secrets.token_urlsafe(12), now, now + self.ttl, raw, recipients, followup, sha, key, requested_by=label,
                                  action_type=action_type)
                kind = action_type or self._type_of(q)
                if self.report_type:
                    q = dc_replace(q, action_type=kind)
                self._db.execute("INSERT INTO request (id, source, action_type, state, created_at, expires_at, sha256, key, key_fingerprint, "
                                 "requested_by, raw, recipients, followup) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (q.id, self.source, kind, "waiting", q.created_at, q.expires_at, sha, key, key_fingerprint,
                                  label, raw, json.dumps(recipients), json.dumps(followup) if followup is not None else None))
                self._version(q.id, sha, raw, "assistant")
                self._event(q.id, "waiting", "queued", at=q.created_at, r={"source": self.source, "action_type": kind})
        except sqlite3.OperationalError as e:
            log.warning("the approval store could not take a request (%s)", type(e).__name__)
            raise StoreUnavailable("The approval store cannot take a request right now. Nothing was queued.") from None
        note = self.on_new if notify is True else notify if callable(notify) else None
        if note is not None:
            try:
                note(q)
            except Exception:  # noqa: BLE001  -- a failed note must never fail the queueing
                pass
        return q

    def pending(self) -> list[QueuedMessage]:
        self._expire()
        with self._lock:
            return [self._q(r) for r in self._db.execute(
                "SELECT * FROM request WHERE source = ? AND state = 'waiting' ORDER BY created_at", (self.source,))]

    def claim(self, item_id: str, state: str = "discarded") -> QueuedMessage | None:
        """Take a waiting request, at most once. state 'executing' (the owner approved: it keeps its content and attempts goes up),
        or how it ended: 'discarded' (the owner) or 'withdrawn' (the agent). None when it is not waiting any more."""
        if state not in ("executing", "discarded", "withdrawn"):
            raise ValueError(f"claim to {state!r}")
        self._expire()
        with self._lock, self._tx():
            r = self._db.execute("SELECT * FROM request WHERE id = ? AND source = ? AND state = 'waiting'", (item_id, self.source)).fetchone()
            if r is None:
                return None
            q = self._q(r)
            if state == "executing":
                self._db.execute("UPDATE request SET state = 'executing', attempts = attempts + 1 WHERE id = ?", (item_id,))
                return dc_replace(q, attempts=q.attempts + 1)
            self._end(item_id, state)
            return q

    def finish(self, item_id: str, state: str, reason: str | None = None) -> None:
        """End a request that was being carried out: sent, unclear, or discarded (reason target_changed)."""
        if state not in ("sent", "unclear", "discarded"):
            raise ValueError(f"finish to {state!r}")
        with self._lock, self._tx():
            if self._db.execute("SELECT 1 FROM request WHERE id = ? AND state = 'executing'", (item_id,)).fetchone():
                self._end(item_id, state, reason)

    def restore(self, q: QueuedMessage) -> None:
        """The executor answered failed (or the claim was for a version the owner did not look at): it waits again. Past its
        expiry it ends expired instead, reported once like any other expiry."""
        lapsed = False
        with self._lock, self._tx():
            r = self._db.execute("SELECT * FROM request WHERE id = ? AND state = 'executing'", (q.id,)).fetchone()
            if r is None:
                return
            if r["expires_at"] <= time.time():
                self._end(q.id, "expired")
                lapsed = True
            else:
                self._db.execute("UPDATE request SET state = 'waiting' WHERE id = ?", (q.id,))
        if lapsed:
            self._report("expired")

    def replace(self, item_id: str, sha256: str, raw: bytes, author: str = "owner", key_fingerprint: str | None = None,
                **fields: Any) -> QueuedMessage | None:
        """Add a new version of a waiting request (the owner's edit, or the agent's revision with author assistant), only when
        it is still the version read. The older versions stay."""
        if author not in AUTHORS:
            raise ValueError(f"author {author!r}")
        new_sha = hashlib.sha256(raw).hexdigest()
        with self._lock, self._tx():
            r = self._db.execute("SELECT * FROM request WHERE id = ? AND source = ? AND state = 'waiting' AND sha256 = ?",
                                 (item_id, self.source, sha256)).fetchone()
            if r is None:
                return None
            q = dc_replace(self._q(r), raw=raw, sha256=new_sha, **fields)
            self._db.execute("UPDATE request SET raw = ?, sha256 = ?, followup = ?, edited_from = ?, recipients = ?, "
                             "key_fingerprint = COALESCE(?, key_fingerprint) WHERE id = ?",
                             (raw, new_sha, json.dumps(q.followup) if q.followup is not None else None, q.edited_from,
                              json.dumps(q.recipients), key_fingerprint, item_id))
            last = self._db.execute("SELECT * FROM version WHERE request_id = ? ORDER BY n DESC LIMIT 1", (item_id,)).fetchone()
            if author == "owner" and last is not None and last["author"] == "owner":
                before = self._db.execute("SELECT sha256 FROM version WHERE request_id = ? AND n = ?", (item_id, last["n"] - 1)).fetchone()
                if before is not None and before["sha256"] == new_sha:      # back to what was there before the owner's edits
                    self._db.execute("DELETE FROM version WHERE request_id = ? AND n = ?", (item_id, last["n"]))
                    self._unref(item_id, last["n"])
                else:                                                       # edits in a row are one version
                    self._db.execute("UPDATE version SET raw = ?, sha256 = ?, created_at = ? WHERE request_id = ? AND n = ?",
                                     (raw, new_sha, time.time(), item_id, last["n"]))
                    self._ref(item_id, last["n"], raw)
            else:
                self._version(item_id, new_sha, raw, author)
            return q

    def revise(self, q: QueuedMessage, raw: bytes, key_fingerprint: str | None = None) -> QueuedMessage | None:
        """A recheck found other values at the service while the request was being carried out (nothing was sent): it waits
        again as a new version by the service. Past its expiry it ends expired instead. None when it was not executing."""
        new_sha = hashlib.sha256(raw).hexdigest()
        lapsed = False
        with self._lock, self._tx():
            r = self._db.execute("SELECT * FROM request WHERE id = ? AND state = 'executing'", (q.id,)).fetchone()
            if r is None:
                return None
            if r["expires_at"] <= time.time():
                self._end(q.id, "expired")
                lapsed = True
                out = None
            else:
                self._db.execute("UPDATE request SET state = 'waiting', raw = ?, sha256 = ?, key_fingerprint = COALESCE(?, key_fingerprint) "
                                 "WHERE id = ?", (raw, new_sha, key_fingerprint, q.id))
                self._version(q.id, new_sha, raw, "service")
                self._event(q.id, "waiting", "target_changed", edited=r["edited_from"] is not None, r=r)
                out = dc_replace(self._q(r), raw=raw, sha256=new_sha)
        if lapsed:
            self._report("expired")
        return out

    # -- the JSON file, in and out ------------------------------------------------------------------------------------------
    def _migrate(self, legacy: Path) -> None:
        """Read the source's JSON queue file into the table, row by row (see the module docstring for the rules)."""
        if not legacy.exists():
            return
        try:
            entries = [QueuedMessage.from_json(d) for d in json.loads(legacy.read_text())["items"]]
        except Exception as e:  # noqa: BLE001  -- an unreadable or tampered file must never turn into a send
            dest = legacy.with_name(f"{legacy.name}.corrupt-{int(time.time())}")
            os.replace(legacy, dest)
            self._note(f"{legacy.name} could not be read ({type(e).__name__}) and was set aside as {dest.name}; nothing in it was sent.")
            return
        read_in, dropped_keys = 0, []
        with self._lock, self._tx():
            for q in entries:
                have = self._row(q.id)
                if have is not None and have["outcome_reason"] != "exported":
                    continue                                      # read in before (a crash between commit and rename)
                key = q.key
                if key is not None:
                    holder = self._db.execute("SELECT * FROM request WHERE source = ? AND key = ? AND id != ?", (self.source, key, q.id)).fetchone()
                    if holder is not None:
                        if holder["state"] in ("waiting", "executing"):
                            dropped_keys.append(q.id)
                            key = None                            # both stay; the one in the table keeps the key
                        else:
                            self._db.execute("UPDATE request SET key = NULL WHERE id = ?", (holder["id"],))
                values = (q.action_type or self._type_of(q), q.created_at, q.expires_at, q.sha256, key, q.requested_by, q.raw, json.dumps(q.recipients),
                          json.dumps(q.followup) if q.followup is not None else None, q.edited_from)
                if have is not None:                              # exported: the file's copy is the one the old version kept
                    self._db.execute("UPDATE request SET action_type = ?, created_at = ?, expires_at = ?, sha256 = ?, key = ?, requested_by = ?, "
                                     "raw = ?, recipients = ?, followup = ?, edited_from = ?, state = 'waiting', decided_at = NULL, "
                                     "outcome_reason = NULL WHERE id = ?", (*values, q.id))
                else:
                    self._db.execute("INSERT INTO request (action_type, created_at, expires_at, sha256, key, requested_by, raw, recipients, "
                                     "followup, edited_from, id, source, state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'waiting')",
                                     (*values, q.id, self.source))
                if not self._db.execute("SELECT 1 FROM version WHERE request_id = ? AND sha256 = ?", (q.id, q.sha256)).fetchone():
                    self._version(q.id, q.sha256, q.raw, "assistant")
                self._event(q.id, "waiting", "queued", edited=q.edited_from is not None, at=q.created_at)
                read_in += 1
        os.replace(legacy, legacy.with_name(f"{legacy.name}.migrated"))
        log.info("approval store: %d %s request(s) read in from %s", read_in, self.source, legacy.name)
        if dropped_keys:
            self._note(f"{len(dropped_keys)} request(s) from {legacy.name} lost their request_id, which another waiting request holds: "
                       + ", ".join(dropped_keys))

    def _type_of(self, q: QueuedMessage) -> str | None:
        """The type `type_of` gives a request read in from the file or queued without one; none when it cannot tell (the request
        still reads in or queues)."""
        if self.type_of is None:
            return None
        try:
            return self.type_of(q) or None
        except Exception:  # noqa: BLE001  -- an entry the function cannot read keeps no type rather than stopping the start
            return None

    def has_source(self, source: str) -> bool:
        """Whether any row of `source` is in the database (package 4b looks before it moves iMessage rows)."""
        with self._lock:
            return self._db.execute("SELECT 1 FROM request WHERE source = ? LIMIT 1", (source,)).fetchone() is not None

    def adopt(self, other: str, convert: Any, action_type: str | None = None) -> list[tuple[str, str, str]]:
        """Take every row of source `other` into this store, once and in one transaction (package 4b: iMessage becomes an action
        type). Ids, states, times, keys and versions stay; `convert(raw) -> raw` gives each stored version its new bytes, so a
        waiting request gets a new hash (D4-1). A version whose bytes cannot be converted keeps its old ones, and its request
        stops being approvable rather than vanishing. Ended requests have no bytes and only change source and type. Returns
        (id, old hash, new hash) for every request that was waiting, for the log.
        Without `action_type` (package 5b: a calendar change is its object plus its metadata, of one of five types) every row keeps
        the type its action_type column holds, `convert(raw, row) -> raw` sees the whole row (followup, recipients, action_type),
        and the metadata, now part of the new bytes, is dropped from the row."""
        moved: list[tuple[str, str, str]] = []
        with self._lock, self._tx():
            rows = self._db.execute("SELECT * FROM request WHERE source = ?", (other,)).fetchall()
            for r in rows:
                whole = {"followup": json.loads(r["followup"]) if r["followup"] else None, "recipients": json.loads(r["recipients"] or "[]"),
                         "action_type": r["action_type"]}
                conv = convert if action_type is not None else (lambda raw, whole=whole: convert(raw, whole))
                old_sha, new_sha, new_raw = r["sha256"], r["sha256"], r["raw"]
                if r["raw"] is not None:
                    try:
                        new_raw = conv(bytes(r["raw"]))
                        new_sha = hashlib.sha256(new_raw).hexdigest()
                    except Exception:  # noqa: BLE001
                        log.warning("approval store: request %s of %s could not be converted; it keeps its old form", r["id"], other)
                        new_raw = r["raw"]
                for v in self._db.execute("SELECT * FROM version WHERE request_id = ? AND raw IS NOT NULL", (r["id"],)).fetchall():
                    try:
                        vraw = new_raw if v["sha256"] == old_sha else conv(bytes(v["raw"]))
                    except Exception:  # noqa: BLE001
                        continue
                    self._db.execute("UPDATE version SET raw = ?, sha256 = ? WHERE request_id = ? AND n = ?",
                                     (vraw, hashlib.sha256(vraw).hexdigest(), r["id"], v["n"]))
                    self._ref(r["id"], v["n"], vraw)
                self._db.execute("UPDATE request SET source = ?, action_type = ?, raw = ?, sha256 = ?, followup = ? WHERE id = ?",
                                 (self.source, action_type or r["action_type"], new_raw, new_sha,
                                  r["followup"] if action_type is not None or new_raw == r["raw"] else None, r["id"]))
                if r["state"] == "waiting":
                    moved.append((r["id"], old_sha, new_sha))
            # the keys a source kept beyond its rows (mail's tombstones, package 6a) move with them (package 6b)
            self._db.execute("UPDATE OR IGNORE key_tombstone SET source = ? WHERE source = ?", (self.source, other))
        if rows:
            log.info("approval store: %d %s request(s) moved to %s", len(rows), other, self.source)
        return moved

    def _note(self, problem: str) -> None:
        self.problem = f"{self.problem} {problem}".strip()
        log.warning("approval store: %s", problem)

    def check_export(self, legacy: str) -> None:
        """Raise RuntimeError when an export to `legacy` would be refused: a request is being carried out, or the file exists."""
        with self._lock:
            busy = self._db.execute("SELECT COUNT(*) FROM request WHERE source = ? AND state = 'executing'", (self.source,)).fetchone()[0]
            if busy:
                raise RuntimeError(f"{busy} request(s) are being carried out right now. Wait until they end, then export again.")
            if (self.dir / legacy).exists():
                raise RuntimeError(f"{legacy} exists already. Move it away first, so nothing in it is overwritten.")

    def waiting_of(self, only: Any = None, skip: Any = ()) -> list[QueuedMessage]:
        """The waiting requests, oldest first; `only`: just these action types, `skip`: not these (an export's split, 4b)."""
        with self._lock:
            rows = [self._q(r) for r in self._db.execute(
                "SELECT * FROM request WHERE source = ? AND state = 'waiting' ORDER BY created_at", (self.source,))]
        return [q for q in rows if (only is None or q.action_type in only) and q.action_type not in skip]

    def export_waiting(self, legacy: str, only: Any = None, skip: Any = (), as_params: bool = False, as_object: Any = None) -> dict[str, int]:
        """Write the waiting requests back to the JSON queue file, for a rollback to a version without the store, and end them
        here in the same step (discarded, reason exported, key freed), so the file is their only copy: a later upgrade reads the
        file in again and never revives a request the old version already sent. Refused while a request is being carried out.
        only/skip pick action types; as_params writes an action's parameters as its bytes, the form a source had before it became
        an action type (package 4b: iMessage back to 4a), with the hash of those bytes; as_object(type, params, blob) -> (bytes,
        metadata) writes the bytes and metadata a source kept before (package 5b: a calendar change back to 5a; 6b: a mail, whose
        attachments it reads with blob(sha256)). A request
        written in another source's form is removed here instead of ended: the previous version reads it in under its own source
        with the same id, which a row left here would hold.
        Returns how many were written and how many ended requests (and unclear ones) a rollback loses."""
        path = self.dir / legacy
        with self._lock:
            self.check_export(legacy)
            waiting = self.waiting_of(only, skip)
            if as_params:
                waiting = [dc_replace(q, raw=(b := json.dumps(json.loads(q.raw.decode())["params"], ensure_ascii=False).encode()),
                                      sha256=hashlib.sha256(b).hexdigest(), action_type=None) for q in waiting]
            elif as_object is not None:
                waiting = [dc_replace(q, raw=raw, followup=meta, sha256=hashlib.sha256(raw).hexdigest())
                           for q in waiting for raw, meta in [as_object(q.action_type, json.loads(q.raw.decode())["params"], self.blob)]]
            ended = self._db.execute("SELECT COUNT(*) FROM request WHERE source = ? AND state != 'waiting'", (self.source,)).fetchone()[0]
            unclear = self._db.execute("SELECT COUNT(*) FROM request WHERE source = ? AND state = 'unclear'", (self.source,)).fetchone()[0]
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"items": [q.to_json() for q in waiting]}, f)
            with self._tx():
                for q in waiting:
                    if as_params or as_object is not None:                 # read in again under the source it came from
                        self._db.execute("DELETE FROM request WHERE id = ?", (q.id,))
                        self._db.execute("DELETE FROM version WHERE request_id = ?", (q.id,))
                        self._unref(q.id)
                        continue
                    self._end(q.id, "discarded", "exported")
                    self._db.execute("UPDATE request SET key = NULL WHERE id = ?", (q.id,))
            os.replace(tmp, path)
        return {"written": len(waiting), "ended_lost": ended, "unclear_lost": unclear}


class _Tx:
    """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK on an error: one writer at a time, also across threads of this process. A COMMIT
    that fails is rolled back too, so the shared connection is never left inside a transaction."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def __enter__(self) -> None:
        self.db.execute("BEGIN IMMEDIATE")

    def __exit__(self, kind: Any, *_: Any) -> None:
        if kind is None:
            try:
                self.db.execute("COMMIT")
                return
            except Exception:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise
        if self.db.in_transaction:
            self.db.execute("ROLLBACK")
