"""The approval store: requests in one SQLite file, DATA_DIR/approvals.db, with an explicit state for each (docs/SENDGATE.md,
package 3). It has the methods of the JSON queue (outbox.py): add, pending, claim, replace, restore, on_new, on_expiry, so a source
uses either. For now it holds the external source only; mail, calendar and iMessage move to it with packages 4 to 6.

States, and who moves a request between them:

    waiting   -> executing      the owner approved (claim(id, "executing"); attempts goes up by one)
    executing -> sent | unclear the executor answered (finish)
    executing -> discarded      checked again before sending, the target had changed (finish, reason target_changed)
    executing -> waiting        the executor answered failed: it provably never went out (restore); past its expiry it ends expired
    waiting   -> discarded | withdrawn | expired    the owner, the agent that asked, or the clock
    waiting   -> discarded      written back to the JSON file for a rollback (reason exported; its key is freed)

A request that ends keeps its id, source, type, state, times, hash, key, fingerprint and reason code, and loses its content (raw,
recipients, followup, edited_from become NULL). Ended rows are deleted after keep_days, the span the activity log keeps.

What else holds:

- One process per DATA_DIR: an exclusive lock on DATA_DIR/approvals.lock is taken when the first store opens and held for the
  life of the process. A second process refuses to start and names the lock. Only that first open recovers rows left in
  `executing` by a crash: they become `unclear` (reason interrupted; it may have gone out; never repeated). A second store on
  the same DATA_DIR in the same process never touches rows the first is carrying out.
- Expiry is lazy: every read and write first ends what has timed out. Each expired or recovered request is reported exactly
  once through on_expiry / on_recovered; the `reported` column remembers it, so a report that could not be made (nothing was
  listening yet, or the process stopped) is made when something listens, also after a restart.
- A key (the agent's request_id) of the external source is unique among its rows and stays bound while its request waits, runs,
  was sent or is unclear, and after the owner discarded it or the agent withdrew it: asking again answers with that request
  (AlreadyDecided). It is freed after `expired`, after a discard with reason `target_changed` and by an export, so the agent can
  ask again. The same key with other parameters (by fingerprint) is a conflict.
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
VERSION = 1                                  # PRAGMA user_version this code writes and reads
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

    def __init__(self, data_dir: str, source: str, ttl: int, max_items: int, keep_days: int = 90, legacy: str | None = None):
        """source: which kind of request this store holds (external). legacy: the JSON queue file to read in once."""
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
        first = _hold_lock(self.dir)
        self._db = self._open()
        if first:
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
        db.execute(f"PRAGMA user_version = {VERSION}")
        for suffix in ("-wal", "-shm"):
            side = Path(f"{self.path}{suffix}")
            if side.exists():
                os.chmod(side, 0o600)
        return db

    def _tx(self) -> "_Tx":
        return _Tx(self._db)

    # -- rows ------------------------------------------------------------------------------------------------------------
    @staticmethod
    def _q(r: sqlite3.Row) -> QueuedMessage:
        raw = r["raw"] if r["raw"] is not None else b""
        return QueuedMessage(r["id"], r["created_at"], r["expires_at"], bytes(raw), json.loads(r["recipients"] or "[]"),
                             json.loads(r["followup"]) if r["followup"] else None, r["sha256"], r["key"], r["edited_from"],
                             r["requested_by"], r["action_type"], attempts=r["attempts"])

    def _row(self, item_id: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM request WHERE id = ? AND source = ?", (item_id, self.source)).fetchone()

    def state_of(self, item_id: str) -> tuple[str, str | None] | None:
        """(state, outcome_reason) of a request, or None when there is no such row."""
        with self._lock:
            r = self._row(item_id)
            return (r["state"], r["outcome_reason"]) if r is not None else None

    def _end(self, item_id: str, state: str, reason: str | None = None) -> None:
        """Inside a transaction: end a request, keep what says what it was, drop its content."""
        self._db.execute("UPDATE request SET state = ?, decided_at = ?, outcome_reason = ?, raw = NULL, recipients = NULL, "
                         "followup = NULL, edited_from = NULL WHERE id = ?", (state, time.time(), reason, item_id))

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
            rows = [r["id"] for r in self._db.execute("SELECT id FROM request WHERE source = ? AND state = 'executing'", (self.source,))]
            for item_id in rows:
                self._end(item_id, "unclear", "interrupted")
        if rows:
            log.warning("%d %s request(s) were being carried out when the server stopped; they are unclear and never repeated", len(rows), self.source)

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
                        self._db.execute("DELETE FROM request WHERE source = ? AND state NOT IN ('waiting', 'executing') AND decided_at < ?",
                                         (self.source, now - self.keep_days * 86400))
        except sqlite3.OperationalError as e:
            log.warning("approval store: expiry skipped this time (%s)", type(e).__name__)
            return
        if gone:
            self._report("expired")

    # -- the queue's operations ---------------------------------------------------------------------------------------------
    def add(self, raw: bytes, recipients: list[str], followup: dict[str, Any] | None = None, key: str | None = None,
            approval_page: str = "the dashboard's Approvals page", requested_by: str | None = None,
            action_type: str | None = None, key_fingerprint: str | None = None) -> QueuedMessage:
        """Queue a request, or return the one already waiting for the same bytes or the same key. Raises AlreadyDecided for a key
        whose request is being carried out or ended and is still bound, KeyConflict for a bound key with other parameters,
        OutboxFull, StoreUnavailable."""
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
                waiting = self._db.execute("SELECT COUNT(*) FROM request WHERE source = ? AND state = 'waiting'", (self.source,)).fetchone()[0]
                if waiting >= self.max_items:
                    raise OutboxFull(f"{waiting} messages are already waiting for owner approval (OUTBOX_MAX={self.max_items}). "
                                     "Nothing was queued. Do not retry: tell the owner to review the queue on "
                                     f"{approval_page}, where they can send or discard what is waiting.")
                now = time.time()
                label = (requested_by if requested_by is not None else callctx.client()) or None
                q = QueuedMessage(secrets.token_urlsafe(12), now, now + self.ttl, raw, recipients, followup, sha, key, requested_by=label,
                                  action_type=action_type)
                self._db.execute("INSERT INTO request (id, source, action_type, state, created_at, expires_at, sha256, key, key_fingerprint, "
                                 "requested_by, raw, recipients, followup) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (q.id, self.source, action_type, "waiting", q.created_at, q.expires_at, sha, key, key_fingerprint,
                                  label, raw, json.dumps(recipients), json.dumps(followup) if followup is not None else None))
        except sqlite3.OperationalError as e:
            log.warning("the approval store could not take a request (%s)", type(e).__name__)
            raise StoreUnavailable("The approval store cannot take a request right now. Nothing was queued.") from None
        if self.on_new is not None:
            try:
                self.on_new(q)
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

    def replace(self, item_id: str, sha256: str, raw: bytes, **fields: Any) -> QueuedMessage | None:
        """Swap a waiting request's content for a new version (the owner's edit), only when it is still the version read."""
        new_sha = hashlib.sha256(raw).hexdigest()
        with self._lock, self._tx():
            r = self._db.execute("SELECT * FROM request WHERE id = ? AND source = ? AND state = 'waiting' AND sha256 = ?",
                                 (item_id, self.source, sha256)).fetchone()
            if r is None:
                return None
            q = dc_replace(self._q(r), raw=raw, sha256=new_sha, **fields)
            self._db.execute("UPDATE request SET raw = ?, sha256 = ?, followup = ?, edited_from = ? WHERE id = ?",
                             (raw, new_sha, json.dumps(q.followup) if q.followup is not None else None, q.edited_from, item_id))
            return q

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
                values = (q.action_type, q.created_at, q.expires_at, q.sha256, key, q.requested_by, q.raw, json.dumps(q.recipients),
                          json.dumps(q.followup) if q.followup is not None else None, q.edited_from)
                if have is not None:                              # exported: the file's copy is the one the old version kept
                    self._db.execute("UPDATE request SET action_type = ?, created_at = ?, expires_at = ?, sha256 = ?, key = ?, requested_by = ?, "
                                     "raw = ?, recipients = ?, followup = ?, edited_from = ?, state = 'waiting', decided_at = NULL, "
                                     "outcome_reason = NULL WHERE id = ?", (*values, q.id))
                else:
                    self._db.execute("INSERT INTO request (action_type, created_at, expires_at, sha256, key, requested_by, raw, recipients, "
                                     "followup, edited_from, id, source, state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'waiting')",
                                     (*values, q.id, self.source))
                read_in += 1
        os.replace(legacy, legacy.with_name(f"{legacy.name}.migrated"))
        log.info("approval store: %d %s request(s) read in from %s", read_in, self.source, legacy.name)
        if dropped_keys:
            self._note(f"{len(dropped_keys)} request(s) from {legacy.name} lost their request_id, which another waiting request holds: "
                       + ", ".join(dropped_keys))

    def _note(self, problem: str) -> None:
        self.problem = f"{self.problem} {problem}".strip()
        log.warning("approval store: %s", problem)

    def export_waiting(self, legacy: str) -> dict[str, int]:
        """Write the waiting requests back to the JSON queue file, for a rollback to a version without the store, and end them
        here in the same step (discarded, reason exported, key freed), so the file is their only copy: a later upgrade reads the
        file in again and never revives a request the old version already sent. Refused while a request is being carried out.
        Returns how many were written and how many ended requests (and unclear ones) a rollback loses."""
        path = self.dir / legacy
        with self._lock:
            busy = self._db.execute("SELECT COUNT(*) FROM request WHERE source = ? AND state = 'executing'", (self.source,)).fetchone()[0]
            if busy:
                raise RuntimeError(f"{busy} request(s) are being carried out right now. Wait until they end, then export again.")
            if path.exists():
                raise RuntimeError(f"{path.name} exists already. Move it away first, so nothing in it is overwritten.")
            waiting = [self._q(r) for r in self._db.execute(
                "SELECT * FROM request WHERE source = ? AND state = 'waiting' ORDER BY created_at", (self.source,))]
            ended = self._db.execute("SELECT COUNT(*) FROM request WHERE source = ? AND state != 'waiting'", (self.source,)).fetchone()[0]
            unclear = self._db.execute("SELECT COUNT(*) FROM request WHERE source = ? AND state = 'unclear'", (self.source,)).fetchone()[0]
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"items": [q.to_json() for q in waiting]}, f)
            with self._tx():
                for q in waiting:
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
