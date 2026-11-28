"""Signed checkpoints of the activity log (docs/SENDGATE.md, package 8).

The log's lines are the leaves of an RFC 9162 Merkle tree (leaf hash SHA-256(0x00 || line), node SHA-256(0x01 || left || right),
where `line` is the entry's JSON line as written, without its newline). The server keeps only the compact range of the tree, at
most one hash per set bit of its size, in DATA_DIR/log-tree.json, and at most once an hour while the log grew it signs a
checkpoint: a C2SP signed note (c2sp.org/tlog-checkpoint v1.1.0) with the origin, the tree size and the root hash, signed with an
Ed25519 log key (c2sp.org/signed-note v1.1.0, type 0x01). The key is DATA_DIR/log-key (mode 600) and never leaves this module: only
its verifier key is shown.

Checkpoints are kept in DATA_DIR/checkpoints.jsonl: the latest of each day for keep_days, and the latest of all. Each carries the
compact range of the tree at its size, so it stays checkable after pruning: before pruning drops entries, every kept checkpoint
older than the new first entry gets the compact range from its size to that entry (a prune proof), and the tree keeps the compact
range before the first kept entry (its base). A checkpoint's range merged with its prune proof gives the base, and the base with
the kept entries gives the tree of the latest checkpoint. That is a consistency proof in compact-range form, not the RFC 9162
§2.1.4 list of hashes; dev/verify_log.py checks it.

The tree is append-only. Before each checkpoint the file is read again and must give the tree the server kept; a line changed,
removed or inserted stops signing (D8-7) until the owner starts a new log tree, which makes a new origin. A line whose tree update
failed (a full disk) is still in the file and is caught up at the next start.

One writer: the AuditLog that owns this object calls it under its own lock, and one process holds a DATA_DIR. Nothing here raises
into the caller's action; errors are logged and the line stays written.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

TREE_FILE = "log-tree.json"
CHECKPOINTS_FILE = "checkpoints.jsonl"
KEY_FILE = "log-key"
EVERY = 3600                   # at most one checkpoint an hour while the log grew (D8-3)
KEEP_ALL_EXTRA_DAYS = 30       # every checkpoint stays this long after its entries could be pruned, then the latest of each day
KEEP_DAILY_DAYS = 365          # ... for a year: a checkpoint kept elsewhere stays checkable after its entries are gone
SIG_ED25519 = 0x01
EMPTY_ROOT = hashlib.sha256(b"").digest()


# -- RFC 9162 §2.1 ---------------------------------------------------------------------------------------------------------------
def leaf_hash(line: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + line).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def mth(leaves: list[bytes]) -> bytes:
    """The Merkle Tree Hash of the leaf hashes, by the definition (for tests and for small trees)."""
    n = len(leaves)
    if n == 0:
        return EMPTY_ROOT
    if n == 1:
        return leaves[0]
    k = 1
    while k * 2 < n:
        k *= 2
    return node_hash(mth(leaves[:k]), mth(leaves[k:]))


# A range is a list of nodes (start, height, hash): perfect subtrees covering consecutive leaves, each aligned to its size.
Node = tuple[int, int, bytes]


def push(nodes: list[Node], node: Node) -> list[Node]:
    """Append a node right after the last one, merging siblings: the compact range grows like a binary counter."""
    out = list(nodes)
    out.append(node)
    while len(out) >= 2:
        (ls, lh, lx), (rs, rh, rx) = out[-2], out[-1]
        if lh != rh or ls % (1 << (lh + 1)) != 0 or ls + (1 << lh) != rs:
            break
        out[-2:] = [(ls, lh + 1, node_hash(lx, rx))]
    return out


def extend(nodes: list[Node], more: list[Node]) -> list[Node]:
    for n in more:
        nodes = push(nodes, n)
    return nodes


def leaves_range(start: int, lines: list[bytes]) -> list[Node]:
    out: list[Node] = []
    for k, line in enumerate(lines):
        out = push(out, (start + k, 0, leaf_hash(line)))
    return out


def end_of(nodes: list[Node], default: int = 0) -> int:
    return nodes[-1][0] + (1 << nodes[-1][1]) if nodes else default


def root(nodes: list[Node]) -> bytes:
    """The root of the tree over [0, n) from its compact range (nodes from 0, largest first)."""
    if not nodes:
        return EMPTY_ROOT
    h = nodes[-1][2]
    for _, _, x in reversed(nodes[:-1]):
        h = node_hash(x, h)
    return h


def nodes_json(nodes: list[Node]) -> list[list[Any]]:
    return [[s, h, base64.b64encode(x).decode()] for s, h, x in nodes]


def nodes_from(data: Any) -> list[Node]:
    return [(int(s), int(h), base64.b64decode(x)) for s, h, x in (data or [])]


# -- C2SP signed notes and checkpoints ---------------------------------------------------------------------------------------------
def key_id(name: str, sig_type: int, public: bytes) -> bytes:
    return hashlib.sha256(name.encode() + b"\n" + bytes([sig_type]) + public).digest()[:4]


def verifier_key(name: str, public: bytes) -> str:
    return f"{name}+{key_id(name, SIG_ED25519, public).hex()}+{base64.b64encode(bytes([SIG_ED25519]) + public).decode()}"


def parse_verifier_key(vkey: str) -> tuple[str, bytes, bytes]:
    """(name, key id, Ed25519 public key). ValueError for anything else."""
    name, kid_hex, b64 = vkey.strip().split("+", 2)       # the key name has no plus; base64 may
    raw = base64.b64decode(b64, validate=True)
    if base64.b64encode(raw).decode() != b64 or len(raw) != 33 or raw[0] != SIG_ED25519 or len(kid_hex) != 8:
        raise ValueError("not an Ed25519 verifier key")
    kid = bytes.fromhex(kid_hex)
    if kid != key_id(name, SIG_ED25519, raw[1:]):
        raise ValueError("the key id does not match the key")
    return name, kid, raw[1:]


def checkpoint_text(origin: str, size: int, root_hash: bytes) -> str:
    return f"{origin}\n{size}\n{base64.b64encode(root_hash).decode()}\n"


def open_note(note: str, vkey: str) -> str:
    """The note text of a signed note whose signature by vkey verifies; ValueError otherwise."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    name, kid, public = parse_verifier_key(vkey)
    cut = note.rfind("\n\n")
    if cut < 0 or not note.endswith("\n"):
        raise ValueError("not a signed note")
    text, sigs = note[:cut + 1], note[cut + 2:].splitlines()
    for line in sigs:
        if not line.startswith("— "):
            raise ValueError("a signature line does not start with an em dash")
        who, _, b64 = line[2:].rpartition(" ")
        raw = base64.b64decode(b64, validate=True)
        if base64.b64encode(raw).decode() != b64:
            raise ValueError("signature is not canonical base64")
        if who != name or raw[:4] != kid:
            continue
        try:
            Ed25519PublicKey.from_public_bytes(public).verify(raw[4:], text.encode())
        except InvalidSignature:
            raise ValueError("the signature does not verify") from None
        return text
    raise ValueError("no signature by this key")


def parse_checkpoint(text: str) -> tuple[str, int, bytes]:
    """(origin, size, root hash) of a checkpoint's note text, strictly as tlog-checkpoint says."""
    lines = text.split("\n")
    if len(lines) < 4 or lines[-1] != "" or any(not x for x in lines[:-1]):
        raise ValueError("a checkpoint has at least three non-empty lines")
    origin, size_s, root_b64 = lines[0], lines[1], lines[2]
    if not size_s.isdigit() or (len(size_s) > 1 and size_s[0] == "0"):
        raise ValueError("the size is not a decimal without leading zeros")
    raw = base64.b64decode(root_b64, validate=True)
    if base64.b64encode(raw).decode() != root_b64 or len(raw) != 32:
        raise ValueError("the root hash is not canonical base64 of 32 bytes")
    return origin, int(size_s), raw


# -- files -------------------------------------------------------------------------------------------------------------------------
def _write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _day(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


class LogProof:
    """The tree, the key and the checkpoints of one AuditLog. Every method is called under that log's lock."""

    def __init__(self, data_dir: str, server_name: str = "", keep_days: int = 90,
                 clock: Callable[[], float] = time.time):
        self.dir = Path(data_dir)
        self.tree_path = self.dir / TREE_FILE
        self.cp_path = self.dir / CHECKPOINTS_FILE
        self.key_path = self.dir / KEY_FILE
        self.server_name = "".join(c for c in (server_name or "localhost") if not c.isspace() and c != "+") or "localhost"
        self.keep_days = max(1, keep_days)
        self.clock = clock
        self._key = None
        self.public = b""
        self.state: dict[str, Any] = {}
        self.nodes: list[Node] = []           # compact range of [0, size)
        self.dirty = False                    # the state in memory is ahead of the file (a write failed)
        self.pending: list[tuple[str, Any]] = []   # ("caught_up", n), ("broken", index), ("signed", checkpoint): the caller acts
                                                   # on them after it let go of its lock (a log line, a notice)

    # -- key ---------------------------------------------------------------------------------------------------------------
    def _load_key(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        try:
            seed = base64.b64decode(json.loads(self.key_path.read_text())["seed"])
            key = Ed25519PrivateKey.from_private_bytes(seed)
        except FileNotFoundError:
            key = Ed25519PrivateKey.generate()
            seed = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
            _write(self.key_path, json.dumps({"seed": base64.b64encode(seed).decode()}) + "\n")
        self._key = key
        self.public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    # -- state -------------------------------------------------------------------------------------------------------------
    @property
    def origin(self) -> str:
        return str(self.state.get("origin", ""))

    @property
    def size(self) -> int:
        return int(self.state.get("size", 0))

    @property
    def vkey(self) -> str:
        return verifier_key(self.origin, self.public) if self.origin and self.public else ""

    @property
    def broken(self) -> dict[str, Any] | None:
        return self.state.get("broken")

    def _new_origin(self) -> str:
        return f"{self.server_name}/icloud-mcp/log/{secrets.token_hex(4)}"

    def _save(self) -> None:
        self.state["nodes"] = nodes_json(self.nodes)
        try:
            _write(self.tree_path, json.dumps(self.state, separators=(",", ":")) + "\n")
            self.dirty = False
        except OSError as e:
            self.dirty = True
            log.warning("Could not write the log tree: %s", type(e).__name__)

    def open(self, lines: list[bytes], chain_break: Callable[[], int | None] | None = None) -> None:
        """At the start: load or make the key and the tree, check the file against the tree, catch up lines written after the tree
        was last saved."""
        self._load_key()
        try:
            self.state = json.loads(self.tree_path.read_text())
            self.nodes = nodes_from(self.state.get("nodes"))
        except FileNotFoundError:
            self._start_tree(lines)
            return
        except (OSError, ValueError, KeyError, TypeError):
            self.state = {"origin": self._new_origin(), "size": 0, "base": {"index": 0, "nodes": []}, "legacy_until": 0}
            self.nodes = []
            self._break(None, "the log tree file could not be read")
            return
        if self.broken:
            return
        self.check(lines, catch_up=True, chain_break=chain_break)

    def _start_tree(self, lines: list[bytes], origin: str | None = None) -> None:
        """A tree over the lines in the file now, starting at index 0; they keep whatever `i` they carry (legacy_until)."""
        self.state = {"v": 1, "origin": origin or self._new_origin(), "created_at": self.clock(), "size": len(lines),
                      "legacy_until": len(lines), "base": {"index": 0, "nodes": [], "first": self._first(lines)},
                      "last_checkpoint_at": 0, "broken": None}
        self.nodes = leaves_range(0, lines)
        self._save()

    @staticmethod
    def _first(lines: list[bytes]) -> str | None:
        return base64.b64encode(leaf_hash(lines[0])).decode() if lines else None

    def _break(self, at: int | None, why: str) -> None:
        self.state["broken"] = {"at": at, "found_at": self.clock(), "why": why}
        self._save()
        self.pending.append(("broken", at))
        log.warning("The activity log no longer matches its signed tree (%s); no checkpoint is signed until a new tree is started", why)

    def first_index(self, lines: list[bytes]) -> int | None:
        """The index of the first line in the file: the tree's base, or the base a prune was about to move to (next_base), told
        apart by the hash of the first line each of them starts with. None when the file starts with neither."""
        base, nxt = self.state.get("base") or {}, self.state.get("next_base")
        if not lines:
            return int((nxt or base).get("index", 0))
        first = base64.b64encode(leaf_hash(lines[0])).decode()
        if base.get("first") == first or (not base.get("first") and int(base.get("index", 0)) == self.size):
            return int(base.get("index", 0))
        if nxt and (nxt.get("first") == first or (not nxt.get("first") and int(nxt["index"]) == self.size)):
            return int(nxt["index"])
        return None

    def check(self, lines: list[bytes], catch_up: bool = False, chain_break: Callable[[], int | None] | None = None) -> bool:
        """Whether the file gives the tree this server kept. With catch_up, lines after the tree's size are added (each must carry
        its index). A mismatch breaks the tree: nothing more is signed."""
        base = self.state.get("base") or {"index": 0, "nodes": []}
        f = self.first_index(lines)
        nxt = self.state.get("next_base")
        if f is None:
            self._break(chain_break() if chain_break else None, "the first line is not the one the tree starts at")
            return False
        if nxt and f == int(nxt["index"]) and f != int(base["index"]):   # a prune that stopped after it rewrote the file
            self.state["base"], base = nxt, nxt
        self.state.pop("next_base", None)
        legacy = int(self.state.get("legacy_until", 0))
        nodes = nodes_from(base["nodes"])
        n = self.size
        if f + len(lines) < n:
            self._break(chain_break() if chain_break else None, "lines were removed")
            return False
        for k, line in enumerate(lines):
            idx = f + k
            try:
                i = json.loads(line).get("i")
            except ValueError:
                i = None
            if idx >= legacy and i != idx:
                self._break(idx, "a line is missing, inserted or out of order")
                return False
            if idx == n:
                if root(nodes) != root(self.nodes):
                    self._break(chain_break() if chain_break else None, "a line was changed")
                    return False
                if not catch_up:
                    self._break(idx, "lines were added without the tree")
                    return False
            nodes = push(nodes, (idx, 0, leaf_hash(line)))
        if f + len(lines) == n and root(nodes) != root(self.nodes):
            self._break(chain_break() if chain_break else None, "a line was changed")
            return False
        if f + len(lines) > n:                       # caught up: the file was ahead of the tree
            self.pending.append(("caught_up", f + len(lines) - n))
            self.nodes = nodes
            self.state["size"] = f + len(lines)
            self._save()
        elif self.dirty:
            self._save()
        return True

    # -- writing -----------------------------------------------------------------------------------------------------------
    def next_index(self) -> int:
        return self.size

    def appended(self, line: bytes) -> None:
        """The line at index `size` was written to the file."""
        base = self.state.setdefault("base", {"index": 0, "nodes": []})
        if self.size == int(base.get("index", 0)) and not base.get("first"):
            base["first"] = base64.b64encode(leaf_hash(line)).decode()
        self.nodes = push(self.nodes, (self.size, 0, leaf_hash(line)))
        self.state["size"] = self.size + 1
        self._save()

    def due(self) -> bool:
        return (not self.broken and self._key is not None and self.size > int(self.state.get("last_checkpoint_size", 0))
                and self.clock() - float(self.state.get("last_checkpoint_at", 0)) >= EVERY)

    def sign(self, lines: list[bytes], chain_break: Callable[[], int | None] | None = None) -> dict[str, Any] | None:
        """Check the file against the tree and sign a checkpoint of it. None when the tree is broken (or breaks now). The latest
        checkpoint again when nothing was added since."""
        if self.broken or self._key is None:
            return None
        if not self.check(lines, catch_up=False, chain_break=chain_break):
            return None
        latest = self.latest()
        if latest and int(latest["size"]) == self.size and latest.get("origin") == self.origin:
            return latest
        now = self.clock()
        text = checkpoint_text(self.origin, self.size, root(self.nodes))
        sig = self._key.sign(text.encode())
        note = text + "\n" + f"— {self.origin} {base64.b64encode(key_id(self.origin, SIG_ED25519, self.public) + sig).decode()}\n"
        cp = {"t": round(now, 3), "origin": self.origin, "size": self.size, "note": note, "nodes": nodes_json(self.nodes)}
        kept = self.checkpoints()
        kept.append(cp)
        self._keep(kept)
        self.state["last_checkpoint_at"], self.state["last_checkpoint_size"] = now, self.size
        self._save()
        self.pending.append(("signed", cp))
        return cp

    def checkpoints(self) -> list[dict[str, Any]]:
        try:
            raw = self.cp_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in raw:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return [c for c in out if c.get("origin") == self.origin]

    def latest(self) -> dict[str, Any] | None:
        kept = self.checkpoints()
        return kept[-1] if kept else None

    def _keep(self, kept: list[dict[str, Any]]) -> None:
        """Every checkpoint for keep_days plus 30 days, then the latest of each day up to a year, and the latest of all. Longer than
        the entries on purpose: a checkpoint a receiver or the owner kept must still find itself here once its entries are pruned,
        and it then reaches the kept entries through its prune proof."""
        now = self.clock()
        all_after = now - (self.keep_days + KEEP_ALL_EXTRA_DAYS) * 86400
        daily_after = now - max(KEEP_DAILY_DAYS, self.keep_days + KEEP_ALL_EXTRA_DAYS) * 86400
        by_day: dict[str, dict[str, Any]] = {}
        out = []
        for c in kept:
            if c["t"] >= all_after:
                out.append(c)
            elif c["t"] >= daily_after:
                by_day[_day(c["t"])] = c
        out = sorted(list(by_day.values()) + out, key=lambda c: c["size"])
        if kept and (not out or out[-1] is not kept[-1]):
            out.append(kept[-1])
        _write(self.cp_path, "".join(json.dumps(c, separators=(",", ":"), ensure_ascii=False) + "\n" for c in out))

    # -- pruning -----------------------------------------------------------------------------------------------------------
    def before_prune(self, lines: list[bytes], keep_from: int) -> None:
        """lines[:keep_from] are about to be dropped. Write the new base and the prune proofs first; the base follows once the
        file is rewritten (after_prune), and a stop in between is resolved at the next start (next_base)."""
        if self.broken:
            return
        base = self.state.get("base") or {"index": 0, "nodes": []}
        f_old = int(base["index"])
        f_new = f_old + keep_from
        dropped = lines[:keep_from]
        new_base = extend(nodes_from(base["nodes"]), leaves_range(f_old, dropped))
        kept = self.checkpoints()
        for c in kept:
            m = int(c["size"])
            if m >= f_new:
                continue
            if m >= f_old:                            # its entries are still in the file: the proof is the dropped part after it
                rng = leaves_range(m, dropped[m - f_old:])
            else:                                     # pruned before: its proof reaches f_old, and grows by what goes now
                proof = c.get("proof") or {}
                if int(proof.get("to", -1)) != f_old:
                    continue                          # nothing to build on: left as is, and the export says it cannot be checked
                rng = extend(nodes_from(proof.get("nodes")), leaves_range(f_old, dropped))
            c["proof"] = {"to": f_new, "nodes": nodes_json(rng)}
        if kept:
            _write(self.cp_path, "".join(json.dumps(c, separators=(",", ":"), ensure_ascii=False) + "\n" for c in kept))
        self.state["next_base"] = {"index": f_new, "nodes": nodes_json(new_base), "first": self._first(lines[keep_from:])}
        self._save()

    def after_prune(self) -> None:
        nxt = self.state.pop("next_base", None)
        if nxt:
            self.state["base"] = nxt
            self._save()

    # -- the owner ---------------------------------------------------------------------------------------------------------
    def new_tree(self, lines: list[bytes]) -> str:
        """A new origin over the file as it is now (D8-7). The old checkpoints move aside, kept for the owner."""
        old = self.origin
        if self.cp_path.exists():
            try:
                os.replace(self.cp_path, self.dir / f"checkpoints-{old.rsplit('/', 1)[-1] or 'old'}.jsonl")
            except OSError:
                pass
        self._start_tree(lines)
        return old

    def status(self) -> dict[str, Any]:
        latest = self.latest()
        return {"verifier_key": self.vkey, "origin": self.origin, "size": self.size,
                "last_checkpoint_at": latest["t"] if latest else None, "last_checkpoint_size": latest["size"] if latest else 0,
                "checkpoints": len(self.checkpoints()), "broken": dict(self.broken) if self.broken else None}

    def export(self, lines: list[str]) -> dict[str, Any]:
        """What Download log carries next to the entries: the base, the checkpoints with their proofs, the verifier key."""
        base = self.state.get("base") or {"index": 0, "nodes": []}
        return {"format": "icloud-mcp-log-export/1", "origin": self.origin, "verifier_key": self.vkey,
                "legacy_until": int(self.state.get("legacy_until", 0)), "base": base,
                "checkpoints": [{k: c[k] for k in ("t", "size", "note", "nodes", "proof") if k in c} for c in self.checkpoints()],
                "entries": lines}
