"""Notices of decisions by webhook (docs/SENDGATE.md, package 9b): a signed notice to each receiver the owner named, every time a
request's status changes.

Where a notice comes from: the status ledger (package 9a, `status_event` in approvals.db), never the activity log. A fan-out step
reads the events after its recorded progress and, in one transaction, gives each event an event id (`notice`), makes one delivery
row per receiver whose filter it passes (`notice_delivery`) and moves its progress (`notice_progress`). It is idempotent, so a
crash between an event and its deliveries, or in the middle of the fan-out, loses nothing and makes nothing twice.

Receivers (DATA_DIR/notices.json, mode 600, at most 5) come from the owner on the dashboard only; no tool reads or writes them. Each
has an id, an https URL whose host is on NOTICE_HOSTS, a filter (kinds, action types, statuses; empty means all), a generation and
the seq it starts after. Adding one starts at the ledger's head (no backlog); a new URL is a new generation with a new secret,
starting at the head again, and the pending deliveries of the old one are dropped; removing one drops its deliveries.

What a notice says (`body`): type `approval.status`, the event's time, and data with the request's id, kind (as approval_status
names it), action type, status, reason code, edited, the hash the request had when the notice was made, and the receiver's cursor.
Built from those fields by name, never from a log line, and never more than 2 KiB. Never a parameter, a text, a title, a
recipient, a chat id or the agent's label (D9-6). An export's discard (reason `exported`) is no decision and makes no notice.

Delivery: one worker thread, one request at a time. A notice is delivered on a 2xx answer only. Six attempts: at once, then after
5 s, 5 min, 30 min, 2 h and 5 h (7 h 35 min in all, D9-4), each delay with 10 % jitter; then the delivery is `dead`, which the owner
sees and can send again. 410 and 413 end that delivery as `refused` without retries and leave the receiver on (D9-5); 429, 502, 503
and 504 wait twice as long, or as long as a Retry-After header says, up to an hour. A redirect is a failure and is never followed.
Per request and receiver a notice waits while an earlier one is pending; across requests there is no order. An attempt is counted
before it is sent, so a process stopped while a request is on the wire sends it again after the start, with the same webhook-id.
At most 10 000 pending per receiver; older ones go `dead`. Ended rows go after keep_days (AUDIT_KEEP_DAYS).

Signing (Standard Webhooks 1.0.0): webhook-id is the event id (`evt_` and 16 random bytes, base64url), the same on every attempt
and for every receiver; webhook-timestamp is the attempt's Unix time; webhook-signature is `v1,` and base64 of HMAC-SHA256 over
`id.timestamp.body`, keyed with the base64-decoded part of the secret after `whsec_`. The server makes the secret (`whsec_` and 32
random bytes), shows it once, and never writes it to the log, an error or an answer after that. A new secret signs beside the old one
for 24 hours (two signatures, space-separated) unless the owner ends the old one sooner. X-MCP-Subscription-Id names the receiver.

The cursor (`e<seq>`, the cursor approval_status(since=...) reads) is a watermark per receiver: every notice for it up to there
was delivered or refused, or did not pass its filter. A pending or dead notice holds it back, so a later success never moves it
past a gap; the receiver fetches the gap with approval_status(since=<cursor>).

Egress (SSRF): https only; the host must be on NOTICE_HOSTS; it is resolved at every attempt and every address must be globally
routable, or the host must be on NOTICE_PRIVATE_HOSTS (a receiver on the owner's own network). A link-local, multicast,
unspecified or reserved address is refused always. The connection goes to the address that was checked, with TLS verified for the
host name, and a timeout of 10 s for the connect and then 10 s for the whole attempt, however slowly the receiver answers.

The tables are new beside the store's; PRAGMA user_version stays 2, so the previous version starts on the same DATA_DIR and ignores
them (D9-8).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import os
import random
import secrets
import socket
import sqlite3
import ssl
import tempfile
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

EVENT_TYPE = "approval.status"
FILE = "notices.json"
DB_FILE = "approvals.db"
MAX_RECEIVERS = 5
MAX_BODY = 2048
MAX_PENDING = 10_000
TIMEOUT = 10.0
BACKOFF = (0, 5, 300, 1800, 7200, 18000)      # before attempts 1..6; 7 h 35 min in all (D9-4)
SLOW_DOWN = (429, 502, 503, 504)
REFUSED = (410, 413)
RETRY_AFTER_MAX = 3600
OVERLAP = 24 * 3600                            # an old secret signs this long beside a new one
CLOCK_EVERY = 60                               # the worker reads the store this often, so an expiry is noticed without traffic
FAILING_LINE = 24 * 3600                       # a receiver failing this long gets a line on the health page
STATUSES = ("waiting", "sent", "unclear", "discarded", "withdrawn", "expired")
STATES = ("pending", "delivered", "refused", "dead")

SCHEMA = """
CREATE TABLE IF NOT EXISTS notice (
  seq         INTEGER PRIMARY KEY,
  event_id    TEXT NOT NULL UNIQUE,
  request_id  TEXT NOT NULL,
  sha256      TEXT,
  created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notice_delivery (
  seq          INTEGER NOT NULL,
  receiver     TEXT NOT NULL,
  generation   INTEGER NOT NULL,
  request_id   TEXT NOT NULL,
  state        TEXT NOT NULL CHECK (state IN ('pending','delivered','refused','dead')),
  attempts     INTEGER NOT NULL DEFAULT 0,
  next_at      REAL NOT NULL,
  first_try_at REAL,
  last_try_at  REAL,
  last_status  INTEGER,
  last_error   TEXT,
  ended_at     REAL,
  PRIMARY KEY (seq, receiver, generation)
);
CREATE INDEX IF NOT EXISTS notice_delivery_due ON notice_delivery (state, next_at);
CREATE INDEX IF NOT EXISTS notice_delivery_receiver ON notice_delivery (receiver, generation, state, seq);
CREATE TABLE IF NOT EXISTS notice_progress (
  name  TEXT PRIMARY KEY,
  seq   INTEGER NOT NULL
);
"""


# -- signing (Standard Webhooks) -------------------------------------------------------------------------------------------------
def new_secret() -> str:
    return "whsec_" + base64.b64encode(secrets.token_bytes(32)).decode()


def sign(secret: str, msg_id: str, timestamp: int, body: bytes) -> str:
    """`v1,` and base64 of HMAC-SHA256 over `msg_id.timestamp.body`, keyed with the secret's base64 after `whsec_`."""
    key = base64.b64decode(secret.removeprefix("whsec_"))
    mac = hmac.new(key, f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


def new_event_id() -> str:
    return "evt_" + base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode()


# -- the body ----------------------------------------------------------------------------------------------------------------------
def _iso_ms(t: float) -> str:
    return datetime.fromtimestamp(float(t), timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(float(t) * 1000) % 1000:03d}Z"


def body(event: dict[str, Any], *, kind: str | None, sha256: str | None, cursor: int) -> bytes:
    """The notice for one status event, built from its fields by name. Raises ValueError above MAX_BODY."""
    data = {"id": event["request_id"], "kind": kind, "action_type": event["action_type"], "status": event["status"],
            "reason": event["reason"], "edited": bool(event["edited"]), "sha256": sha256, "cursor": f"e{int(cursor)}"}
    out = json.dumps({"type": EVENT_TYPE, "timestamp": _iso_ms(event["at"]), "data": data}, separators=(",", ":")).encode()
    if len(out) > MAX_BODY:
        raise ValueError("notice body too large")
    return out


# -- addresses ---------------------------------------------------------------------------------------------------------------------
def address_ok(ip: str, private_ok: bool) -> bool:
    """Whether a notice may go to this address: a globally routable one, or (for a host on NOTICE_PRIVATE_HOSTS) any other
    except link-local (the cloud metadata address among them), multicast, unspecified and reserved."""
    try:
        a = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped is not None:
        a = a.ipv4_mapped
    if a.is_link_local or a.is_multicast or a.is_unspecified:
        return False
    if a.is_global:
        return True
    return private_ok and (a.is_loopback or not a.is_reserved)


def _resolve(host: str, port: int) -> list[str]:
    return list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))


class _Pinned(http.client.HTTPSConnection):
    """An HTTPS connection to an address that was checked, with TLS verified for the host name."""

    def __init__(self, host: str, ip: str, port: int, context: ssl.SSLContext, timeout: float):
        super().__init__(host, port, context=context, timeout=timeout)
        self._ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def https_post(host: str, ip: str, port: int, path: str, headers: dict[str, str], data: bytes, context: ssl.SSLContext,
               timeout: float) -> tuple[int, dict[str, str]]:
    """POST once to the checked address; never follows a redirect. Returns (status, lower-cased headers). `timeout` bounds the
    connect, and once connected it is one deadline for the whole attempt: a receiver that answers a byte at a time is cut off."""
    conn = _Pinned(host, ip, port, context, timeout)
    fired = threading.Event()

    def cut() -> None:
        fired.set()
        try:
            socket.socket.shutdown(conn.sock, socket.SHUT_RDWR)       # wakes a blocked read; the attempt then ends as a timeout
        except (OSError, TypeError):
            pass
    timer = None
    try:
        conn.connect()
        timer = threading.Timer(timeout, cut)
        timer.daemon = True
        timer.start()
        conn.request("POST", path, body=data, headers=headers)
        r = conn.getresponse()
        r.read(4096)
        if fired.is_set():
            raise TimeoutError("the attempt ran past its deadline")
        return r.status, {k.lower(): v for k, v in r.getheaders()}
    except Exception:
        if fired.is_set():
            raise TimeoutError("the attempt ran past its deadline") from None
        raise
    finally:
        if timer is not None:
            timer.cancel()
        conn.close()


class Refused(Exception):
    """This attempt is not made; code says why (a code, never a URL or a secret)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _retry_after(value: str | None, now: float) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(int(value.strip())))
    except ValueError:
        pass
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - now)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def _clean_list(v: Any, what: str) -> list[str]:
    if v is None or v == "":
        return []
    items = v.replace("\n", ",").split(",") if isinstance(v, str) else v
    if not isinstance(items, list) or len(items) > 20:
        raise ValueError(f"{what}: a list of at most 20 entries.")
    out = []
    for x in items:
        x = str(x).strip()
        if not x:
            continue
        if len(x) > 64:
            raise ValueError(f"{what}: an entry is longer than 64 characters.")
        out.append(x)
    return sorted(dict.fromkeys(out))


def clean_filter(f: Any) -> dict[str, list[str]]:
    """A receiver's filter: kinds, action types and statuses; an empty list means all. Raises ValueError for the owner."""
    f = f if isinstance(f, dict) else {}
    out = {"kinds": _clean_list(f.get("kinds"), "Kinds"), "action_types": _clean_list(f.get("action_types"), "Action types"),
           "statuses": _clean_list(f.get("statuses"), "Statuses")}
    if bad := [x for x in out["statuses"] if x not in STATUSES]:
        raise ValueError("Statuses: unknown " + ", ".join(bad) + ". Use " + ", ".join(STATUSES) + ".")
    return out


def filter_wider(old: dict[str, list[str]], new: dict[str, list[str]]) -> bool:
    """A filter lets more through when a list that was set becomes empty (= all) or gains an entry."""
    for k in ("kinds", "action_types", "statuses"):
        o, n = set(old.get(k) or []), set(new.get(k) or [])
        if (o and not n) or (o and not n <= o):
            return True
    return False


# a pending delivery that waits for no earlier pending notice of its request and receiver (_due and the worker's wait)
_FREE = ("d.state = 'pending' AND NOT EXISTS (SELECT 1 FROM notice_delivery e WHERE e.receiver = d.receiver AND "
         "e.generation = d.generation AND e.request_id = d.request_id AND e.state = 'pending' AND e.seq < d.seq)")


class NeedsConfirm(Exception):
    """A change lets more through than the receiver as it is now, and the owner has not signed in recently."""


def _looser(r: dict[str, Any], url: str | None, filt: Any) -> bool:
    if url is not None and str(url).strip() != r["url"]:
        return True
    return filt is not None and filter_wider(r["filter"], clean_filter(filt))


class NoticeService:
    def __init__(self, data_dir: str, boxes: list[Any] | None = None, *, hosts: Any = (), private_hosts: Any = (),
                 keep_days: int = 90, kinds: Callable[[str], str | None] | None = None,
                 resolve: Callable[[str, int], list[str]] | None = None, ssl_context: ssl.SSLContext | None = None,
                 transport: Callable[..., tuple[int, dict[str, str]]] | None = None):
        """boxes: the approval stores, read once a minute so an expiry is noticed without other traffic. hosts / private_hosts:
        NOTICE_HOSTS and NOTICE_PRIVATE_HOSTS. kinds(action_type): the kind approval_status names for a request of the external
        source (D6-8). resolve, ssl_context and transport are for tests (a local receiver with a test certificate)."""
        self.dir = Path(data_dir)
        self.path = self.dir / FILE
        self.boxes = [b for b in (boxes or []) if b is not None]
        self.hosts = {h.strip().lower() for h in hosts if h and h.strip()}
        self.private_hosts = {h.strip().lower() for h in private_hosts if h and h.strip()}
        self.keep_days = max(1, int(keep_days))
        self.kinds = kinds
        self.resolve = resolve or _resolve
        self.ssl_context = ssl_context or ssl.create_default_context()
        self.transport = transport or https_post
        self._lock = threading.RLock()
        self._file_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()
        self._busy: set[tuple[int, str, int]] = set()     # deliveries on the wire now (one process holds the DATA_DIR lock)
        self.autostart = False                 # start() turns it on: the worker then runs while a receiver exists
        self._clock_at = 0.0
        self._retained_at = 0.0
        db_path = self.dir / DB_FILE
        if not db_path.exists():               # the store makes it (mode 600); notices without a store have nothing to say
            raise RuntimeError("the approval store is not open")
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None, timeout=5.0)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(SCHEMA)
        with _Tx(self._db):                    # the first start of 9b begins at the ledger's head: no backlog
            self._db.execute("INSERT OR IGNORE INTO notice_progress (name, seq) VALUES ('fanout', ?)", (self._head(),))
        self._receivers = self._load()
        self._drop_stale()

    # -- receivers (notices.json) ----------------------------------------------------------------------------------------------
    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []
        items = data.get("receivers") if isinstance(data, dict) else None
        return [r for r in (items or []) if isinstance(r, dict) and r.get("id") and r.get("url") and r.get("secrets")][:MAX_RECEIVERS]

    def _drop_stale(self) -> None:
        """Drop the pending deliveries of receivers that are gone and of generations older than a receiver's own: a new URL or a
        removal writes notices.json first and drops them after, so a stop in between leaves them here, never sent."""
        with self._lock, _Tx(self._db):
            gens = {r["id"]: int(r["generation"]) for r in self._receivers}
            for row in self._db.execute("SELECT DISTINCT receiver, generation FROM notice_delivery WHERE state = 'pending'").fetchall():
                if row["receiver"] not in gens or row["generation"] < gens[row["receiver"]]:
                    self._db.execute("DELETE FROM notice_delivery WHERE receiver = ? AND generation = ? AND state = 'pending'",
                                     (row["receiver"], row["generation"]))

    def _save(self, receivers: list[dict[str, Any]] | None = None) -> None:
        """Write notices.json; with `receivers`, that list (the caller takes it into memory once it is written)."""
        receivers = self._receivers if receivers is None else receivers
        with self._file_lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".notices-", suffix=".tmp")
            try:
                os.chmod(tmp, 0o600)
                with os.fdopen(fd, "w") as f:
                    json.dump({"receivers": receivers}, f, indent=1)
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

    def _get(self, rid: str) -> dict[str, Any] | None:
        return next((r for r in self._receivers if r["id"] == rid), None)

    def check_url(self, url: str) -> str:
        """The URL as stored, or ValueError in the owner's words: https, no user name or password, a host on NOTICE_HOSTS."""
        url = str(url or "").strip()
        parts = urlsplit(url)
        if len(url) > 300 or parts.scheme != "https" or not parts.hostname:
            raise ValueError("A receiver is an https address, such as https://hooks.example.org/sendgate.")
        if parts.username or parts.password:
            raise ValueError("A receiver address carries no user name or password.")
        try:
            parts.port
        except ValueError:
            raise ValueError("The address has a port that is not a number.") from None
        if parts.hostname.lower() not in self.hosts:
            raise ValueError(f"{parts.hostname} is not on Allowed hosts (NOTICE_HOSTS). Add it there, restart, then add the receiver.")
        return url

    def _head(self) -> int:
        r = self._db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'status_event'").fetchone()
        return int(r[0]) if r is not None else 0

    def add(self, url: str, filt: Any = None) -> tuple[dict[str, Any], str]:
        """A new receiver, starting at the ledger's head. Returns (what the owner sees, the secret, shown this once)."""
        url, filt = self.check_url(url), clean_filter(filt)
        with self._lock:
            if len(self._receivers) >= MAX_RECEIVERS:
                raise ValueError(f"At most {MAX_RECEIVERS} receivers. Remove one first.")
            secret = new_secret()
            rec = {"id": "rcv_" + secrets.token_hex(6), "url": url, "filter": filt, "generation": 1, "from_seq": self._head(),
                   "created_at": time.time(), "secrets": [{"secret": secret, "until": None}]}
            changed = self._receivers + [rec]
            self._save(changed)                # memory follows the file, never runs ahead of it
            self._receivers = changed
        log.info("notices: a receiver on %s was added", _host_of(url))
        self._ensure_thread()
        return self.public(rec), secret

    def loosens(self, rid: str, url: str | None = None, filt: Any = None) -> bool:
        """Whether a change lets more through: a new URL, or a wider filter."""
        with self._lock:
            r = self._get(rid)
            return r is not None and _looser(r, url, filt)

    def change(self, rid: str, url: str | None = None, filt: Any = None, *,
               may_loosen: bool = True) -> tuple[dict[str, Any], str | None] | None:
        """A new URL is a new generation with a new secret (returned), starting at the head; the old generation's pending
        deliveries are dropped. A filter alone keeps the generation. None when there is no such receiver. Without may_loosen (no
        recent sign-in), NeedsConfirm when the change lets more through than the receiver as it is now."""
        new_url = self.check_url(url) if url is not None else None
        new_filt = clean_filter(filt) if filt is not None else None
        with self._lock:
            old = self._get(rid)
            if old is None:
                return None
            if not may_loosen and _looser(old, url, filt):
                raise NeedsConfirm("the change lets more through")
            r, secret = dict(old), None
            if new_url is not None and new_url != r["url"]:
                secret = new_secret()
                r.update(url=new_url, generation=int(r["generation"]) + 1, from_seq=self._head(), secrets=[{"secret": secret, "until": None}])
            if new_filt is not None:
                r["filter"] = new_filt
            changed = [r if x["id"] == rid else x for x in self._receivers]
            self._save(changed)                # first the config; a stop before the rows go leaves them to _drop_stale
            self._receivers = changed
            if secret is not None:
                with _Tx(self._db):
                    self._db.execute("DELETE FROM notice_delivery WHERE receiver = ? AND generation < ? AND state = 'pending'",
                                     (rid, r["generation"]))
        return self.public(r), secret

    def rotate(self, rid: str) -> str | None:
        """A new secret; the old one signs beside it for 24 hours. Returns the new secret, shown this once."""
        with self._lock:
            r = self._get(rid)
            if r is None:
                return None
            secret, now = new_secret(), time.time()
            r = {**r, "secrets": [{"secret": secret, "until": None}] + [{"secret": s["secret"], "until": now + OVERLAP}
                                                                       for s in r["secrets"][:1]]}
            changed = [r if x["id"] == rid else x for x in self._receivers]
            self._save(changed)
            self._receivers = changed
        log.info("notices: the secret of a receiver on %s was rotated", _host_of(r["url"]))
        return secret

    def end_old(self, rid: str) -> bool:
        """Stop signing with the old secret now."""
        with self._lock:
            r = self._get(rid)
            if r is None or len(r["secrets"]) < 2:
                return False
            changed = [{**x, "secrets": x["secrets"][:1]} if x["id"] == rid else x for x in self._receivers]
            self._save(changed)
            self._receivers = changed
        return True

    def remove(self, rid: str) -> dict[str, Any] | None:
        """Remove a receiver and drop its deliveries: nothing more goes to it."""
        with self._lock:
            r = self._get(rid)
            if r is None:
                return None
            shown = self.public(r)
            changed = [x for x in self._receivers if x["id"] != rid]
            self._save(changed)                # first the config; a stop before the rows go leaves them to _drop_stale
            self._receivers = changed
            with _Tx(self._db):
                self._db.execute("DELETE FROM notice_delivery WHERE receiver = ?", (rid,))
        log.info("notices: a receiver on %s was removed", _host_of(r["url"]))
        return shown

    def resend(self, rid: str) -> int:
        """Every dead delivery of a receiver goes out again, from its first attempt. Returns how many."""
        with self._lock:
            r = self._get(rid)
            if r is None:
                return 0
            with _Tx(self._db):
                n = self._db.execute("UPDATE notice_delivery SET state = 'pending', attempts = 0, next_at = ?, ended_at = NULL "
                                     "WHERE receiver = ? AND generation = ? AND state = 'dead'",
                                     (time.time(), rid, r["generation"])).rowcount
        if n:
            self._wake.set()
        return n

    def _status(self, r: dict[str, Any]) -> dict[str, Any]:
        rows = self._db.execute("SELECT state, COUNT(*) AS n FROM notice_delivery WHERE receiver = ? AND generation = ? GROUP BY state",
                                (r["id"], r["generation"])).fetchall()
        counts = {s: 0 for s in STATES} | {row["state"]: row["n"] for row in rows}
        ok = self._db.execute("SELECT MAX(ended_at) FROM notice_delivery WHERE receiver = ? AND generation = ? AND state = 'delivered'",
                              (r["id"], r["generation"])).fetchone()[0]
        last = self._db.execute("SELECT last_status, last_error FROM notice_delivery WHERE receiver = ? AND generation = ? AND "
                                "last_try_at IS NOT NULL ORDER BY last_try_at DESC LIMIT 1", (r["id"], r["generation"])).fetchone()
        failing = self._db.execute("SELECT MIN(first_try_at) FROM notice_delivery WHERE receiver = ? AND generation = ? AND "
                                   "((state = 'pending' AND attempts > 0) OR state = 'dead') AND first_try_at > ?",
                                   (r["id"], r["generation"], ok or 0)).fetchone()[0]
        error = None
        if last is not None and (last["last_error"] or (last["last_status"] and not 200 <= last["last_status"] < 300)):
            error = last["last_error"] or f"http_{last['last_status']}"
        return {"last_ok_at": ok, "pending": counts["pending"], "dead": counts["dead"], "refused": counts["refused"],
                "delivered": counts["delivered"], "last_error": error, "failing_since": failing}

    def public(self, r: dict[str, Any]) -> dict[str, Any]:
        """What the owner sees of a receiver: never a secret."""
        return {"id": r["id"], "url": r["url"], "host": _host_of(r["url"]), "filter": r["filter"], "generation": r["generation"],
                "created_at": r.get("created_at"), "rotating": any(s.get("until") and s["until"] > time.time() for s in r["secrets"])}

    def receivers(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{**self.public(r), "status": self._status(r)} for r in self._receivers]

    def problems(self, now: float | None = None) -> list[str]:
        """A line per receiver that has been failing for more than 24 hours (codes and hosts only)."""
        now = time.time() if now is None else now
        out = []
        for r in self.receivers():
            st = r["status"]
            if st["failing_since"] and now - st["failing_since"] > FAILING_LINE:
                out.append(f"Notices to {r['host']} have failed for more than 24 hours ({st['last_error'] or 'no answer'}).")
        return out

    # -- fan-out ---------------------------------------------------------------------------------------------------------------
    def _kind(self, ev: Any) -> str | None:
        if ev["kind"] == "external" and ev["action_type"] and self.kinds is not None:
            try:
                return self.kinds(ev["action_type"]) or ev["kind"]
            except Exception:  # noqa: BLE001
                return ev["kind"]
        return ev["kind"]

    def _passes(self, r: dict[str, Any], ev: Any) -> bool:
        f = r["filter"]
        return ((not f["kinds"] or self._kind(ev) in f["kinds"]) and (not f["action_types"] or ev["action_type"] in f["action_types"])
                and (not f["statuses"] or ev["status"] in f["statuses"]))

    def fan_out(self) -> int:
        """Make the deliveries of every event after the recorded progress, and move the progress, in one transaction. Returns
        how many deliveries it made."""
        made = 0
        with self._lock:
            if self._progress() >= self._head():     # nothing new: no write transaction
                return 0
        with self._lock, _Tx(self._db):
            done = self._progress()
            events = self._db.execute("SELECT * FROM status_event WHERE seq > ? ORDER BY seq", (done,)).fetchall()
            now = time.time()
            for ev in events:
                done = ev["seq"]
                if ev["reason"] == "exported":     # an export's discard is no decision: the request goes on in the file
                    continue
                to = [r for r in self._receivers if ev["seq"] > int(r["from_seq"]) and self._passes(r, ev)]
                if not to:
                    continue
                if not self._db.execute("SELECT 1 FROM notice WHERE seq = ?", (ev["seq"],)).fetchone():
                    sha = self._db.execute("SELECT sha256 FROM request WHERE id = ?", (ev["request_id"],)).fetchone()
                    self._db.execute("INSERT INTO notice (seq, event_id, request_id, sha256, created_at) VALUES (?,?,?,?,?)",
                                     (ev["seq"], new_event_id(), ev["request_id"], sha[0] if sha else None, now))
                for r in to:
                    made += self._db.execute("INSERT OR IGNORE INTO notice_delivery (seq, receiver, generation, request_id, state, next_at) "
                                             "VALUES (?,?,?,?, 'pending', ?)", (ev["seq"], r["id"], r["generation"], ev["request_id"], now)).rowcount
            self._db.execute("UPDATE notice_progress SET seq = ? WHERE name = 'fanout'", (done,))
            for r in self._receivers:           # a bounded queue: the oldest beyond MAX_PENDING go dead, and the owner sees it
                over = self._db.execute("SELECT COUNT(*) FROM notice_delivery WHERE receiver = ? AND state = 'pending'", (r["id"],)).fetchone()[0] - MAX_PENDING
                if over > 0:
                    self._db.execute("UPDATE notice_delivery SET state = 'dead', ended_at = ?, last_error = 'queue_full' WHERE rowid IN "
                                     "(SELECT rowid FROM notice_delivery WHERE receiver = ? AND state = 'pending' ORDER BY seq LIMIT ?)",
                                     (now, r["id"], over))
        return made

    def _progress(self) -> int:
        got = self._db.execute("SELECT seq FROM notice_progress WHERE name = 'fanout'").fetchone()
        return int(got[0]) if got is not None else 0

    # -- delivery --------------------------------------------------------------------------------------------------------------
    def cursor_for(self, r: dict[str, Any], seq: int) -> int:
        """The receiver's watermark while notice `seq` goes to it: below its oldest other pending or dead notice, at most the
        fan-out's progress."""
        done = self._progress()
        held = self._db.execute("SELECT MIN(seq) FROM notice_delivery WHERE receiver = ? AND generation = ? AND state IN ('pending','dead') "
                                "AND seq != ?", (r["id"], r["generation"], seq)).fetchone()[0]
        return min(done, held - 1) if held is not None else done

    def _due(self, now: float) -> list[sqlite3.Row]:
        return self._db.execute(f"SELECT * FROM notice_delivery d WHERE {_FREE} AND next_at <= ? ORDER BY next_at, seq LIMIT 100",
                                (now,)).fetchall()

    def _end(self, row: Any, state: str, now: float, status: int | None = None, error: str | None = None) -> None:
        with self._lock, _Tx(self._db):        # one connection for every thread: each transaction holds the lock
            self._db.execute("UPDATE notice_delivery SET state = ?, ended_at = ?, last_status = ?, last_error = ? WHERE seq = ? AND "
                             "receiver = ? AND generation = ? AND state = 'pending'",
                             (state, now, status, error, row["seq"], row["receiver"], row["generation"]))

    def _failed(self, row: Any, attempts: int, now: float, status: int | None, error: str | None, retry_after: float | None) -> None:
        if attempts >= len(BACKOFF):
            self._end(row, "dead", now, status, error)
            return
        wait = BACKOFF[attempts] * (0.9 + 0.2 * random.random())
        if status in SLOW_DOWN:
            wait = max(wait * 2, min(retry_after, RETRY_AFTER_MAX)) if retry_after is not None else wait * 2
        with self._lock, _Tx(self._db):
            self._db.execute("UPDATE notice_delivery SET next_at = ?, last_status = ?, last_error = ? WHERE seq = ? AND receiver = ? AND "
                             "generation = ? AND state = 'pending'", (now + wait, status, error, row["seq"], row["receiver"], row["generation"]))

    def deliver(self, row: Any) -> str:
        """One attempt for one delivery row. Returns the row's state afterwards, or "busy" when another caller is sending it."""
        key = (row["seq"], row["receiver"], row["generation"])
        with self._lock:
            if key in self._busy:
                return "busy"
            self._busy.add(key)
        try:
            return self._deliver(row)
        finally:
            with self._lock:
                self._busy.discard(key)

    def _deliver(self, row: Any) -> str:
        now = time.time()
        with self._lock:
            r = self._get(row["receiver"])
            if r is None or int(r["generation"]) != row["generation"]:
                self._drop_stale()              # a removal or a new URL that stopped before its rows went
                return "gone"
            row = self._db.execute("SELECT * FROM notice_delivery WHERE seq = ? AND receiver = ? AND generation = ? AND state = 'pending'",
                                   (row["seq"], row["receiver"], row["generation"])).fetchone()
            if row is None:                         # resent, removed or ended meanwhile
                return "gone"
            ev = self._db.execute("SELECT * FROM status_event WHERE seq = ?", (row["seq"],)).fetchone()
            note = self._db.execute("SELECT * FROM notice WHERE seq = ?", (row["seq"],)).fetchone()
            if ev is None or note is None:          # the event went with its request (retention): nothing left to say
                self._end(row, "dead", now, None, "gone")
                return "dead"
            attempts = row["attempts"] + 1
            with _Tx(self._db):                    # counted before it is sent: a stop while on the wire sends it again later
                self._db.execute("UPDATE notice_delivery SET attempts = ?, last_try_at = ?, first_try_at = COALESCE(first_try_at, ?), "
                                 "next_at = ? WHERE seq = ? AND receiver = ? AND generation = ? AND state = 'pending'",
                                 (attempts, now, now, now + (BACKOFF[attempts] if attempts < len(BACKOFF) else 0), row["seq"],
                                  row["receiver"], row["generation"]))
            data = body(dict(ev), kind=self._kind(ev), sha256=note["sha256"], cursor=self.cursor_for(r, row["seq"]))
            url, keys = r["url"], [s["secret"] for s in r["secrets"] if s.get("until") is None or s["until"] > now]
        ts = int(now)
        headers = {"content-type": "application/json", "user-agent": "icloud-mcp-notices", "webhook-id": note["event_id"],
                   "webhook-timestamp": str(ts), "webhook-signature": " ".join(sign(k, note["event_id"], ts, data) for k in keys),
                   "x-mcp-subscription-id": r["id"]}
        status, error, retry_after = None, None, None
        try:
            status, answer = self._post(url, headers, data)
            retry_after = _retry_after(answer.get("retry-after"), now)
        except Refused as e:
            error = e.code
        except (TimeoutError, socket.timeout):
            error = "timeout"
        except ssl.SSLError:
            error = "tls_failed"
        except (OSError, http.client.HTTPException):
            error = "connect_failed"
        except Exception:  # noqa: BLE001  -- whatever else went wrong is a failed attempt, retried like the others
            error = "failed"
        if status is not None and 200 <= status < 300:
            self._end(row, "delivered", time.time(), status)
            return "delivered"
        if status in REFUSED:
            self._end(row, "refused", time.time(), status)
            return "refused"
        if status is not None:
            error = "redirect" if 300 <= status < 400 else None
        self._failed(row, attempts, time.time(), status, error, retry_after)
        return "pending" if attempts < len(BACKOFF) else "dead"

    def _post(self, url: str, headers: dict[str, str], data: bytes) -> tuple[int, dict[str, str]]:
        """Check the URL and every address it resolves to now, then post to the checked one."""
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not host:
            raise Refused("not_https")
        if host not in self.hosts:
            raise Refused("host_not_allowed")
        port = parts.port or 443
        try:
            ips = self.resolve(host, port)
        except OSError:
            raise Refused("resolve_failed") from None
        private_ok = host in self.private_hosts
        if not ips or not all(address_ok(ip, private_ok) for ip in ips):
            raise Refused("address_not_allowed")
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        return self.transport(host, ips[0], port, path, headers, data, self.ssl_context, TIMEOUT)

    # -- the worker ----------------------------------------------------------------------------------------------------------
    def clock(self, now: float | None = None) -> None:
        """Read the stores once a minute: a request that ran out ends expired and its event is written."""
        now = time.time() if now is None else now
        if now - self._clock_at < CLOCK_EVERY:
            return
        self._clock_at = now
        for box in self.boxes:
            try:
                box.pending()
            except Exception as e:  # noqa: BLE001  -- a store that cannot be read now is read next time
                log.warning("notices: the store could not be read (%s)", type(e).__name__)

    def retain(self, now: float | None = None) -> None:
        """Ended deliveries older than keep_days go, and the notices nothing refers to any more."""
        now = time.time() if now is None else now
        if now - self._retained_at < 3600:
            return
        self._retained_at = now
        with self._lock, _Tx(self._db):
            self._db.execute("DELETE FROM notice_delivery WHERE state != 'pending' AND ended_at < ?", (now - self.keep_days * 86400,))
            self._db.execute("DELETE FROM notice WHERE seq NOT IN (SELECT seq FROM notice_delivery)")

    def run_once(self) -> dict[str, int]:
        """One round of the worker: the clock, the fan-out, every delivery that is due, the retention. Returns counts by result."""
        self.clock()
        self.fan_out()
        counts: dict[str, int] = {}
        tried: set[tuple[int, str, int]] = set()
        for _ in range(50):                    # a notice that waited for an earlier one of its request goes in the same round
            with self._lock:
                due = [r for r in self._due(time.time()) if (r["seq"], r["receiver"], r["generation"]) not in tried]
            if not due or self._stop.is_set():
                break
            for row in due:
                if self._stop.is_set():
                    break
                tried.add((row["seq"], row["receiver"], row["generation"]))
                res = self.deliver(row)
                counts[res] = counts.get(res, 0) + 1
        self.retain()
        return counts

    def _next_wait(self) -> float:
        with self._lock:
            r = self._db.execute(f"SELECT MIN(next_at) FROM notice_delivery d WHERE {_FREE}").fetchone()[0]   # as _due picks
        until = (r - time.time()) if r is not None else 30.0
        return max(0.0, min(until, 1.0))       # a new event is looked for every second

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001  -- the worker never dies of one bad round
                log.warning("notices: a round failed (%s)", type(e).__name__)
            self._wake.wait(self._next_wait())
            self._wake.clear()

    def _ensure_thread(self) -> None:
        with self._thread_lock:                # two callers at once start one worker
            if not self.autostart or not self._receivers or (self._thread is not None and self._thread.is_alive()):
                return
            self.fan_out()                     # the first run records where the fan-out starts
            self._thread = threading.Thread(target=self._loop, name="icloud-notices", daemon=True)
            self._thread.start()

    def start(self) -> None:
        """Run the worker thread while a receiver exists (it starts with the first one added)."""
        self.autostart = True
        self.fan_out()
        self._ensure_thread()

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=TIMEOUT + 2)

    def close(self) -> None:
        self.stop()
        self._db.close()


class _Tx:
    """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK on an error (as gate/store.py)."""

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
