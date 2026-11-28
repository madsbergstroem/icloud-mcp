"""The activity log: who changed what, and when, on this server.

Every write an agent makes through a tool, every approval and discard on the dashboard, every owner sign-in (and failed
attempt), new and removed passkeys, signed-out apps and changed settings land here as one JSON line. It records names and
addresses, never message bodies, passwords or tokens. The log lives in DATA_DIR/audit.jsonl (mode 600) and keeps
AUDIT_KEEP_DAYS days (90 by default).

Each entry carries the hash of the entry before it, so a line changed or removed after it was written breaks the chain, and
the dashboard says so. That alone is tamper evidence for the file, not a defence against someone who can rewrite the whole file.
Since package 8 each entry also carries `i`, its index counted from the first entry of its tree, and the lines are the leaves of a
Merkle tree whose head the server signs at most once an hour (gate/logproof.py). A checkpoint kept outside the server shows a
rewrite later, even one that rebuilt the chain. Pruning drops only the oldest lines, never one in the middle.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

FILE = "audit.jsonl"
_GENESIS = "0" * 64
_MAX_TEXT = 200          # longest single value kept in an entry (a subject, an event title)


def _clip(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= _MAX_TEXT else value[:_MAX_TEXT - 1] + "…"
    if isinstance(value, (list, tuple)):
        return [_clip(v) for v in list(value)[:25]]
    if isinstance(value, dict):
        return {str(k): _clip(v) for k, v in list(value.items())[:25]}
    return value


def _digest(prev: str, entry: dict[str, Any]) -> str:
    body = json.dumps({k: v for k, v in entry.items() if k != "h"}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((prev + body).encode()).hexdigest()


class AuditLog:
    """Append-only, one process. `record` never raises: a full disk must not turn into a failed send or a failed sign-in."""

    def __init__(self, data_dir: str, keep_days: int = 90, enabled: bool = True, server_name: str = "", proof: bool = True):
        self.path = Path(data_dir) / FILE
        self.keep_days = max(1, keep_days)
        self.enabled = enabled
        self._lock = threading.Lock()
        self._last = self._tail_hash()
        self._pruned_at = 0.0
        self.proof = None                       # the signed tree over the lines (package 8); none while the log is off
        self.on_checkpoint: Any = None           # called with each new checkpoint's note, outside the lock (notices)
        if enabled and proof:
            try:
                from .logproof import LogProof
                lp = LogProof(data_dir, server_name, self.keep_days)
                with self._lock:
                    lines = self._lines()
                    lp.open(lines, chain_break=lambda: self._chain_break(lines))
                self.proof = lp
            except Exception as e:  # noqa: BLE001  -- the log works without its proof, and the health check says so
                log.warning("The activity log's checkpoints are off: %s", type(e).__name__)
            self._after()

    def _lines(self) -> list[bytes]:
        try:
            return [x for x in self.path.read_bytes().split(b"\n") if x.strip()]
        except OSError:
            return []

    def _chain_break(self, lines: list[bytes]) -> int | None:
        """The index (`i`) of the first line the hash chain does not accept, or None when the chain holds."""
        prev = None
        last_i = None
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                return last_i + 1 if isinstance(last_i, int) else None
            if prev is None:
                prev = e.get("prev", _GENESIS)
            if e.get("prev") != prev or _digest(prev, e) != e.get("h"):
                return e.get("i") if isinstance(e.get("i"), int) else (last_i + 1 if isinstance(last_i, int) else None)
            prev, last_i = e["h"], e.get("i", last_i)
        return None

    def _after(self) -> None:
        """What the tree asked for while the lock was held: a line for a catch-up or a broken tree, the notice of a checkpoint."""
        lp = self.proof
        if lp is None or not lp.pending:
            return
        todo, lp.pending = lp.pending, []
        for kind, value in todo:
            try:
                if kind == "caught_up":
                    self.record("log.caught_up", actor="server", summary=f"The log's tree caught up with {value} line(s) written while it could not be saved",
                                lines=value)
                elif kind == "broken":
                    self.record("log.broken", actor="server", ok=False, summary="The log no longer matches its signed checkpoints; nothing more is signed",
                                at=value)
                elif kind == "signed" and self.on_checkpoint is not None:
                    self.on_checkpoint(value["note"], value["t"])
            except Exception as e:  # noqa: BLE001
                log.warning("Could not act on the log's tree: %s", type(e).__name__)

    def _tail_hash(self) -> str:
        try:
            with open(self.path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 65536))
                lines = [x for x in f.read().splitlines() if x.strip()]
            return json.loads(lines[-1])["h"] if lines else _GENESIS
        except (OSError, ValueError, KeyError, IndexError):
            return _GENESIS

    def record(self, action: str, *, actor: str, via: str = "", summary: str = "", ok: bool = True, **details: Any) -> None:
        """actor: 'owner', an app's name, or 'server'. via: how the owner signed in (passkey name, 'password'), or the client id."""
        if not self.enabled:
            return
        try:
            with self._lock:
                entry = {"t": round(time.time(), 3), "action": action, "actor": _clip(actor), "ok": bool(ok)}
                if via:
                    entry["via"] = _clip(via)
                if summary:
                    entry["summary"] = _clip(summary)
                if details:
                    entry["details"] = _clip({k: v for k, v in details.items() if v not in (None, "", [], {})})
                lp = self.proof
                if lp is not None:
                    entry["i"] = lp.next_index()
                entry["prev"] = self._last
                entry["h"] = _digest(self._last, entry)
                line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                os.chmod(self.path, 0o600)
                self._last = entry["h"]
                if lp is not None:
                    try:
                        lp.appended(line.encode())
                        if lp.due():
                            lines = self._lines()
                            lp.sign(lines, chain_break=lambda: self._chain_break(lines))
                    except Exception as e:  # noqa: BLE001  -- the line is written; the tree catches up at the next start
                        log.warning("Could not update the log's tree: %s", type(e).__name__)
                if time.time() - self._pruned_at > 3600:
                    self._prune()
        except Exception as e:  # noqa: BLE001  -- logging must never break the action it records
            log.warning("Could not write the activity log: %s", type(e).__name__)
        self._after()

    def _prune(self) -> None:
        """Drop the oldest entries, those older than keep_days, up to the first one that is not (a clock that went back keeps
        the rest in order). The first kept entry still names the hash of the one before it; verify() starts there. With
        checkpoints, the tree keeps the range before the first kept entry and the kept checkpoints get their prune proofs first."""
        self._pruned_at = time.time()
        cutoff = time.time() - self.keep_days * 86400
        try:
            raw = self.path.read_bytes()
        except OSError:
            return
        lines = [x for x in raw.split(b"\n") if x.strip()]
        drop = 0
        for line in lines:
            try:
                if json.loads(line).get("t", 0) >= cutoff:
                    break
            except ValueError:
                break                     # an unreadable line stays, so verify() can report it
            drop += 1
        if not drop:
            return
        if self.proof is not None:
            self.proof.before_prune(lines, drop)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(b"".join(x + b"\n" for x in lines[drop:]))
        os.replace(tmp, self.path)
        if self.proof is not None:
            self.proof.after_prune()

    # -- checkpoints (package 8) -------------------------------------------------------------------------------------------
    def proof_status(self) -> dict[str, Any]:
        """What Settings and the health check show: the verifier key, the last checkpoint, a broken tree. Never the key."""
        if self.proof is None:
            return {"on": False}
        with self._lock:
            return {"on": True, **self.proof.status()}

    def export(self) -> dict[str, Any]:
        """Download log: a checkpoint of everything first, then the entries as written with the tree's base and checkpoints."""
        if self.proof is None:
            raise RuntimeError("the log has no checkpoints")
        with self._lock:
            lines = self._lines()
            if not self.proof.broken:
                self.proof.sign(lines, chain_break=lambda: self._chain_break(lines))
            out = self.proof.export([x.decode("utf-8", "replace") for x in self._lines()])
        self._after()
        return out

    def new_tree(self) -> str:
        """Start a new tree over the file as it is (the owner's step, D8-7). Returns the old origin."""
        if self.proof is None:
            raise RuntimeError("the log has no checkpoints")
        with self._lock:
            old = self.proof.new_tree(self._lines())
            self.proof.pending = [x for x in self.proof.pending if x[0] != "broken"]
        return old

    def entries(self, limit: int = 200, kinds: tuple[str, ...] = (), since: float = 0) -> list[dict[str, Any]]:
        """The newest entries first. kinds filters by action prefix ('approval.', 'signin.'); since stops at older entries."""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in reversed(lines):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if since and e.get("t", 0) < since:
                break
            if kinds and not str(e.get("action", "")).startswith(kinds):
                continue
            out.append(e)
            if len(out) >= limit:
                break
        return out

    def verify(self) -> dict[str, Any]:
        """Walk the chain. ok=False names the first line that does not match: changed, removed or inserted after writing."""
        try:
            lines = [x for x in self.path.read_text(encoding="utf-8").splitlines() if x.strip()]
        except OSError:
            return {"ok": True, "entries": 0, "since": None}
        prev, since = None, None
        for n, line in enumerate(lines, 1):
            try:
                e = json.loads(line)
            except ValueError:
                return {"ok": False, "entries": len(lines), "broken_at": n, "since": since}
            if prev is None:                 # the oldest kept entry: its predecessor may have been pruned
                prev, since = e.get("prev", _GENESIS), e.get("t")
            if e.get("prev") != prev or _digest(prev, e) != e.get("h"):
                return {"ok": False, "entries": len(lines), "broken_at": n, "since": since}
            prev = e["h"]
        return {"ok": True, "entries": len(lines), "since": since}
