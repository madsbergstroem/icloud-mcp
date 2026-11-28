#!/usr/bin/env python3
"""Check a downloaded activity log (Settings › Activity log › Download log) on any computer (docs/SENDGATE.md, package 8).

    python3 verify_log.py icloud-mcp-log.json --key '<verifier key>' [--checkpoint kept.txt]

--key is the verifier key you copied from Settings and kept elsewhere; without it the key inside the file is used, which proves
only that the file agrees with itself. --checkpoint is a checkpoint you kept from before (a notice, a copy): the script says
whether the download extends it.

It checks that every checkpoint in the file is signed by the key, that the entries give the tree each checkpoint signed (RFC 9162
Merkle tree, C2SP tlog-checkpoint), that each entry carries its index and the hash of the one before it, and that checkpoints
older than the first kept entry reach the kept entries through their prune proofs. Exit 0 when all of that holds, 1 with the first
index that does not.

Self-contained on purpose: it needs Python 3.9 or newer and the `cryptography` package, nothing from the server.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys

GENESIS = "0" * 64


def leaf(line: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + line).digest()


def node(a: bytes, b: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + a + b).digest()


def push(nodes: list, n: tuple) -> list:
    out = list(nodes) + [n]
    while len(out) >= 2:
        (ls, lh, lx), (rs, rh, rx) = out[-2], out[-1]
        if lh != rh or ls % (1 << (lh + 1)) or ls + (1 << lh) != rs:
            break
        out[-2:] = [(ls, lh + 1, node(lx, rx))]
    return out


def root(nodes: list) -> bytes:
    if not nodes:
        return hashlib.sha256(b"").digest()
    h = nodes[-1][2]
    for _, _, x in reversed(nodes[:-1]):
        h = node(x, h)
    return h


def load_nodes(data) -> list:
    return [(int(s), int(h), base64.b64decode(x)) for s, h, x in (data or [])]


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def parse_vkey(vkey: str):
    name, kid, key = vkey.strip().split("+", 2)
    raw = base64.b64decode(key, validate=True)
    if b64(raw) != key or len(raw) != 33 or raw[0] != 1:
        raise ValueError("not an Ed25519 verifier key")
    if hashlib.sha256(name.encode() + b"\n\x01" + raw[1:]).digest()[:4].hex() != kid:
        raise ValueError("the key id does not match the key")
    return name, bytes.fromhex(kid), raw[1:]


def open_checkpoint(note: str, vkey: str):
    """(origin, size, root) of a checkpoint signed by vkey; ValueError otherwise."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    name, kid, public = parse_vkey(vkey)
    cut = note.rfind("\n\n")
    if cut < 0 or not note.endswith("\n"):
        raise ValueError("not a signed note")
    text = note[:cut + 1]
    good = False
    for line in note[cut + 2:].splitlines():
        if not line.startswith("— "):
            raise ValueError("bad signature line")
        who, _, sig = line[2:].rpartition(" ")
        raw = base64.b64decode(sig, validate=True)
        if who == name and raw[:4] == kid:
            try:
                Ed25519PublicKey.from_public_bytes(public).verify(raw[4:], text.encode())
            except InvalidSignature:
                raise ValueError("the signature does not verify") from None
            good = True
    if not good:
        raise ValueError("not signed by this key")
    lines = text.split("\n")
    if len(lines) < 4 or any(not x for x in lines[:-1]) or lines[0] != name:
        raise ValueError("not a checkpoint of this log")
    size, r = lines[1], base64.b64decode(lines[2], validate=True)
    if not size.isdigit() or (len(size) > 1 and size[0] == "0") or b64(r) != lines[2] or len(r) != 32:
        raise ValueError("a malformed checkpoint")
    return lines[0], int(size), r


def digest(prev: str, entry: dict) -> str:
    body = json.dumps({k: v for k, v in entry.items() if k != "h"}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((prev + body).encode()).hexdigest()


class Bad(Exception):
    def __init__(self, at, why):
        super().__init__(why)
        self.at, self.why = at, why


def verify(export: dict, vkey: str | None = None, kept: str | None = None) -> dict:
    """Raise Bad(index, why) at the first problem; return what was checked."""
    vkey = vkey or export.get("verifier_key") or ""
    base = export.get("base") or {}
    f = int(base.get("index", 0))
    legacy = int(export.get("legacy_until", 0))
    lines = [x.encode("utf-8") for x in export.get("entries", [])]
    n = f + len(lines)
    cps = []
    for c in export.get("checkpoints", []):
        try:
            cps.append((open_checkpoint(c["note"], vkey), c))
        except ValueError as e:
            raise Bad(None, f"checkpoint of size {c.get('size')}: {e}") from None
    # each entry: valid JSON, its index, the hash of the one before it
    prev = None
    for k, line in enumerate(lines):
        idx = f + k
        try:
            e = json.loads(line)
        except ValueError:
            raise Bad(idx, "not a JSON line") from None
        if idx < legacy:
            continue                  # in the file when its tree began (before package 8, or a new tree the owner started): the tree covers it
        if e.get("i") != idx:
            raise Bad(idx, f"carries index {e.get('i')!r}: an entry is missing, inserted or out of order here")
        if prev is None:
            prev = e.get("prev", GENESIS)
        if e.get("prev") != prev or digest(prev, e) != e.get("h"):
            raise Bad(idx, "does not match the hash chain: changed after it was written")
        prev = e["h"]
    # the tree at every checkpoint inside the kept entries
    nodes = load_nodes(base.get("nodes"))
    want = {size: r for (_, size, r), _ in cps}
    if f in want and root(nodes) != want[f]:
        raise Bad(f, f"the entries before index {f} do not give the checkpoint of size {f}")
    for k, line in enumerate(lines):
        nodes = push(nodes, (f + k, 0, leaf(line)))
        size = f + k + 1
        if size in want and root(nodes) != want[size]:
            raise Bad(None, f"the entries up to index {size - 1} do not give the signed checkpoint of size {size}")
    # older checkpoints: their range, merged with the prune proof, is the base
    base_nodes = load_nodes(base.get("nodes"))
    older = 0
    for (_, size, r), c in cps:
        if size >= f:
            continue
        rng = load_nodes(c.get("nodes"))
        if root(rng) != r:
            raise Bad(None, f"the checkpoint of size {size} does not match its own range")
        proof = c.get("proof") or {}
        if int(proof.get("to", -1)) != f:
            raise Bad(None, f"the checkpoint of size {size} has no proof to the first kept entry")
        merged = rng
        for x in load_nodes(proof.get("nodes")):
            merged = push(merged, x)
        if merged != base_nodes:
            raise Bad(None, f"the checkpoint of size {size} is not followed by the kept entries")
        older += 1
    last = max((size for (_, size, _), _ in cps), default=0)
    if lines and last < n:
        raise Bad(last, f"entries from index {last} on are after the last checkpoint and not signed")
    out = {"origin": vkey.split("+")[0], "entries": len(lines), "first": f, "size": n, "checkpoints": len(cps), "older": older}
    if kept:
        _, size, r = open_checkpoint(kept, vkey)
        if size >= f:
            c = load_nodes(base.get("nodes"))
            for k, line in enumerate(lines[: size - f]):
                c = push(c, (f + k, 0, leaf(line)))
            if size > n or root(c) != r:
                raise Bad(None, f"your checkpoint of size {size} is not what these entries give: the log was rewritten")
        elif not any(s == size and rr == r for (_, s, rr), _ in cps):
            raise Bad(None, f"your checkpoint of size {size} is older than the first kept entry and not among the file's checkpoints")
        out["kept"] = size
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Check a downloaded activity log against its signed checkpoints.")
    ap.add_argument("export")
    ap.add_argument("--key", help="the verifier key from Settings, kept elsewhere")
    ap.add_argument("--checkpoint", help="a file with a checkpoint you kept from before")
    a = ap.parse_args(argv)
    with open(a.export, encoding="utf-8") as fh:
        export = json.load(fh)
    kept = open(a.checkpoint, encoding="utf-8").read() if a.checkpoint else None
    if not a.key:
        print("No --key given: using the key inside the file, which proves only that the file agrees with itself.")
    try:
        r = verify(export, a.key, kept)
    except Bad as e:
        print(f"FAILED{'' if e.at is None else f' at index {e.at}'}: {e.why}")
        return 1
    print(f"OK: {r['entries']} entries (index {r['first']} to {r['size'] - 1}), {r['checkpoints']} "
          f"checkpoint{'' if r['checkpoints'] == 1 else 's'} of {r['origin']}"
          + (f", {r['older']} from before the oldest kept entry" if r["older"] else "")
          + (f"; your checkpoint of size {r['kept']} is extended by this log" if "kept" in r else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
