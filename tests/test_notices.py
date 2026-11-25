"""Notices of decisions by webhook (docs/SENDGATE.md, package 9b): fan-out from the status ledger, the body, Standard Webhooks
signing, retries, the receiver's cursor, address checks and the rollback. Every request, receiver and secret here is made up. Each
test says what it protects, the wrong result it rejects and what a green run does not prove."""
import base64
import datetime as _dt
import hashlib
import hmac
import io
import json
import logging
import os
import ssl
import stat
import subprocess
import sys
import tarfile
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from icloud_mcp.gate import notices as N
from icloud_mcp.gate import store as S
from icloud_mcp.gate.audit import AuditLog
from icloud_mcp.gate.status import ApprovalLedger

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "acebbf9"                       # the last commit before package 9b
URL = "https://hooks.example.org/sendgate"
HOST = "hooks.example.org"
PUBLIC_IP = "93.184.216.34"              # a globally routable address the fake resolver hands out


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def time(self):
        return self.now

    def tick(self, seconds=5.0):
        self.now += seconds


class Receiver:
    """A fake transport: records every attempt and answers from a list of statuses (the last one repeats)."""

    def __init__(self, *answers, headers=None):
        self.answers = list(answers) or [200]
        self.headers = headers or {}
        self.calls = []

    def __call__(self, host, ip, port, path, headers, data, context, timeout):
        self.calls.append({"host": host, "ip": ip, "port": port, "path": path, "headers": dict(headers), "body": data})
        status = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(status, Exception):
            raise status
        return status, dict(self.headers)

    def bodies(self):
        return [json.loads(c["body"]) for c in self.calls]


def kinds(type_id):
    return "mail" if type_id.startswith("mail.") else None


TTL = 30 * 86400                         # requests outlive every schedule here unless a test makes one run out


def setup(tmp_path, monkeypatch, *answers, hosts=(HOST,), private=(), resolve=None, keep_days=90):
    clock = Clock()
    monkeypatch.setattr(S, "time", clock)
    monkeypatch.setattr(N, "time", clock)
    monkeypatch.setattr(N, "random", types.SimpleNamespace(random=lambda: 0.5))       # no jitter: the schedule as written
    st = S.Store(str(tmp_path), "external", TTL, 50)
    rx = Receiver(*answers)
    svc = N.NoticeService(str(tmp_path), [st], hosts=hosts, private_hosts=private, keep_days=keep_days, kinds=kinds,
                          resolve=resolve or (lambda h, p: [PUBLIC_IP]), transport=rx)
    return clock, st, svc, rx


def raw_of(action_type, who, text):
    return json.dumps({"action_type": action_type, "params": {"to": f"{who}@example.org", "text": text}}, sort_keys=True).encode()


def add(st, who="jana", text="Hello", action_type="t.echo"):
    return st.add(raw_of(action_type, who, text), [f"{who}@example.org"], action_type=action_type)


def rows(svc, receiver=None):
    q = "SELECT * FROM notice_delivery" + (" WHERE receiver = ?" if receiver else "") + " ORDER BY seq"
    return [dict(r) for r in svc._db.execute(q, (receiver,) if receiver else ())]


def verify(secret, headers, data, now, tolerance=300):
    """The receiver's side, written from the Standard Webhooks spec: a fresh timestamp and one matching v1 signature."""
    msg_id, ts = headers["webhook-id"], headers["webhook-timestamp"]
    if abs(now - int(ts)) > tolerance:
        return False
    key = base64.b64decode(secret.split("_", 1)[1])
    want = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + data, hashlib.sha256).digest()).decode()
    return any(s.startswith("v1,") and hmac.compare_digest(s[3:], want) for s in headers["webhook-signature"].split(" "))


def release(tmp_path):
    os.close(S._locks.pop(str(Path(tmp_path).resolve())))


def reopen(st, svc, tmp_path):
    """The store opened again, as at a start: a request left executing is recovered (unclear, interrupted)."""
    st._db.close()
    release(tmp_path)
    st = S.Store(str(tmp_path), "external", TTL, 50)
    svc.boxes = [st]
    return st


def per_request(items):
    out = {}
    for item_id, *rest in items:
        out.setdefault(item_id, []).append(tuple(rest))
    return out


# -- the body ---------------------------------------------------------------------------------------------------------------------
def every_row_of_the_status_table(tmp_path, st):
    """One request per row of the status table in docs/SENDGATE.md (package 9); returns [(id, status, reason)] in the order made."""
    want = []
    q = add(st, "jana")
    want.append((q.id, "waiting", "queued"))
    st.revise(st.claim(q.id, "executing"), raw_of("t.echo", "jana", "Changed at the service"))
    want.append((q.id, "waiting", "target_changed"))
    st.claim(q.id, "executing")
    st.finish(q.id, "sent")
    want.append((q.id, "sent", None))
    for status, reason in (("unclear", None), ("discarded", "target_changed"), ("discarded", "not_allowed")):
        r = add(st, "tom", f"{status} {reason}")
        st.claim(r.id, "executing")
        st.finish(r.id, status, reason)
        want += [(r.id, "waiting", "queued"), (r.id, status, reason)]
    for how in ("discarded", "withdrawn"):
        r = add(st, "sam", how)
        st.claim(r.id, how)
        want += [(r.id, "waiting", "queued"), (r.id, how, None)]
    r = add(st, "lea", "runs out")
    st._db.execute("UPDATE request SET expires_at = ? WHERE id = ?", (r.created_at - 1, r.id))
    st.pending()
    want += [(r.id, "waiting", "queued"), (r.id, "expired", None)]
    r = add(st, "ben", "on the wire")
    st.claim(r.id, "executing")
    want += [(r.id, "waiting", "queued"), (r.id, "unclear", "interrupted")]
    return want


def test_one_notice_per_row_of_the_status_table_with_only_the_listed_fields(tmp_path, monkeypatch):
    """protects=the status table and the body rule (fields by name, no requested_by, at most 2 KiB); fails_when=a change is
    missing, sent twice, in the wrong order, or a body carries a field beyond the list; no_claim=a real HTTPS receiver (the
    integration test below)."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch)
    svc.add(URL)
    want = every_row_of_the_status_table(tmp_path, st)
    st = reopen(st, svc, tmp_path)                                        # a restart finds ben executing: unclear, interrupted
    svc.run_once()
    got = rx.bodies()
    assert per_request((b["data"]["id"], b["data"]["status"], b["data"]["reason"]) for b in got) == per_request(want)
    assert len(got) == len(want)
    for b in got:
        assert set(b) == {"type", "timestamp", "data"} and b["type"] == "approval.status"
        assert set(b["data"]) == {"id", "kind", "action_type", "status", "reason", "edited", "sha256", "cursor"}
        assert b["data"]["kind"] == "external" and b["data"]["action_type"] == "t.echo" and b["timestamp"].endswith("Z")
        assert len(b["data"]["sha256"]) == 64 and b["data"]["cursor"].startswith("e")
    assert all(len(c["body"]) <= N.MAX_BODY for c in rx.calls) and all(b"requested_by" not in c["body"] for c in rx.calls)
    assert len({c["headers"]["webhook-id"] for c in rx.calls}) == len(want)
    assert {r["state"] for r in rows(svc)} == {"delivered"}


def test_the_body_is_built_by_name_and_refuses_more_than_2_kib():
    """protects=the builder's limit and its exact shape; fails_when=an oversized body is sent or the JSON is not minified;
    no_claim=none beyond protects."""
    ev = {"request_id": "Qm9vaz2kX1pLr0aZ", "kind": "external", "action_type": "market.reply", "status": "sent", "reason": None,
          "edited": 1, "at": 1_800_000_000.25}
    out = N.body(ev, kind="external", sha256="a" * 64, cursor=1042)
    assert out == (b'{"type":"approval.status","timestamp":"2027-01-15T08:00:00.250Z","data":{"id":"Qm9vaz2kX1pLr0aZ","kind":"external",'
                   b'"action_type":"market.reply","status":"sent","reason":null,"edited":true,"sha256":"' + b"a" * 64
                   + b'","cursor":"e1042"}}')
    with pytest.raises(ValueError):
        N.body({**ev, "action_type": "x" * 3000}, kind="external", sha256=None, cursor=1)


def test_a_mail_notice_names_the_kind_approval_status_names(tmp_path, monkeypatch):
    """protects=one vocabulary (D6-8): a mail action is kind mail in a notice as in approval_status; fails_when=the notice says
    external; no_claim=other plugin kinds (they keep their source's kind)."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch)
    svc.add(URL)
    q = add(st, "jana", action_type="mail.send")
    st.claim(q.id, "discarded")
    svc.run_once()
    ledger = ApprovalLedger({"external": st}, AuditLog(str(tmp_path), enabled=False), kinds=kinds)
    assert [b["data"]["kind"] for b in rx.bodies()] == ["mail", "mail"] == [ledger.status(q.id)["kind"]] * 2


def test_no_parameter_title_recipient_key_or_label_appears_in_any_body_header_or_row(tmp_path, monkeypatch):
    """protects=criterion 6: metadata only, D9-6 (no requested_by); fails_when=any parameter value, title, recipient, key or the
    agent's label appears in a body, a header or a row of the notice tables; no_claim=every type a plugin may bring (four made-up
    ones here; tests/test_notices_dashboard.py covers a real mail end to end)."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch)
    svc.add(URL)
    values = ["Jana Example", "jana@example.org", "tom@example.net", "Sam Example", "sam@example.net", "Dinner at the lake house",
              "Please bring the salad", "planner-tom", "key-for-jana-77", "chat-room-4411", "Owner wrote this instead"]
    for n, t in enumerate(("t.echo", "market.reply", "calendar.invite", "mail.send")):
        raw = json.dumps({"action_type": t, "params": {"to": ["jana@example.org", "tom@example.net"], "cc": "sam@example.net",
                                                       "name": "Jana Example", "guest": "Sam Example", "title": "Dinner at the lake house",
                                                       "text": "Please bring the salad", "chat_id": "chat-room-4411", "n": n}},
                         sort_keys=True).encode()
        q = st.add(raw, ["jana@example.org", "tom@example.net"], {"subject": "Dinner at the lake house"},
                   key="key-for-jana-77" if n == 0 else None, requested_by="planner-tom", action_type=t)
        q = st.replace(q.id, q.sha256, raw + b" ", edited_from="Owner wrote this instead")
        st.claim(q.id, "executing")
        st.finish(q.id, "sent")
    svc.run_once()
    assert len(rx.calls) == 8
    seen = json.dumps([{"h": c["headers"], "b": c["body"].decode()} for c in rx.calls])
    seen += json.dumps([dict(r) for t in ("notice", "notice_delivery", "notice_progress") for r in svc._db.execute(f"SELECT * FROM {t}")])
    for v in values:
        assert v not in seen, v


# -- signing ----------------------------------------------------------------------------------------------------------------------
def test_signing_matches_the_standard_webhooks_vector():
    """protects=the formula: v1, base64 HMAC-SHA256 over id.timestamp.body, keyed with the base64 after whsec_; fails_when=any part
    of the formula differs (the vector is the spec's own example, computed from that formula); no_claim=asymmetric v1a."""
    assert N.sign("whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw", "msg_p5jXN8AQM9LWM0D4loKWxJek", 1614265330, b'{"test": 2432232314}') \
        == "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="
    secret = N.new_secret()
    assert secret.startswith("whsec_") and len(base64.b64decode(secret[6:])) == 32 and N.new_secret() != secret
    eid = N.new_event_id()
    assert eid.startswith("evt_") and len(base64.urlsafe_b64decode(eid[4:] + "==")) == 16


def test_a_rotated_secret_signs_beside_the_old_one_for_24_hours(tmp_path, monkeypatch):
    """protects=zero-downtime rotation: two signatures for 24 hours, then one, and the owner can end the old one early;
    fails_when=the old secret stops verifying at once, verifies after 24 hours, or the header is not two space-separated v1
    signatures; no_claim=none beyond protects."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch)
    rec, old = svc.add(URL)
    new = svc.rotate(rec["id"])
    assert svc.receivers()[0]["rotating"] is True and new != old
    add(st)
    svc.run_once()
    c = rx.calls[-1]
    assert len(c["headers"]["webhook-signature"].split(" ")) == 2
    assert verify(old, c["headers"], c["body"], clock.now) and verify(new, c["headers"], c["body"], clock.now)
    clock.tick(N.OVERLAP + 1)
    add(st, "tom")
    svc.run_once()
    c = rx.calls[-1]
    assert verify(new, c["headers"], c["body"], clock.now) and not verify(old, c["headers"], c["body"], clock.now)
    newer = svc.rotate(rec["id"])
    assert svc.end_old(rec["id"]) is True and svc.receivers()[0]["rotating"] is False
    add(st, "sam")
    svc.run_once()
    c = rx.calls[-1]
    assert c["headers"]["webhook-signature"].count("v1,") == 1 and verify(newer, c["headers"], c["body"], clock.now)


# -- retries ----------------------------------------------------------------------------------------------------------------------
def test_500_twice_then_200_is_three_attempts_with_one_id_and_new_timestamps(tmp_path, monkeypatch):
    """protects=criteria 2 and 3: at least once, one webhook-id, a fresh timestamp and signature per attempt, the backoff at once,
    5 s, 5 min; fails_when=the id changes, a timestamp repeats, it is sent before it is due, or it ends with other than 3
    attempts; no_claim=jitter (fixed here)."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch, 500, 500, 200)
    _, secret = svc.add(URL)
    q = add(st)
    svc.run_once()
    assert rows(svc)[0]["attempts"] == 1 and rows(svc)[0]["next_at"] == clock.now + 5
    clock.tick(4)
    svc.run_once()
    assert len(rx.calls) == 1
    clock.tick(1)
    svc.run_once()
    assert rows(svc)[0]["next_at"] == clock.now + 300
    clock.tick(300)
    svc.run_once()
    r = rows(svc)[0]
    assert (r["state"], r["attempts"], r["last_status"]) == ("delivered", 3, 200)
    assert len({c["headers"]["webhook-id"] for c in rx.calls}) == 1 and len({c["headers"]["webhook-timestamp"] for c in rx.calls}) == 3
    assert all(verify(secret, c["headers"], c["body"], int(c["headers"]["webhook-timestamp"])) for c in rx.calls)
    assert rx.bodies()[0]["data"]["id"] == q.id


def test_six_failures_make_a_dead_delivery_that_the_owner_can_send_again(tmp_path, monkeypatch):
    """protects=D9-4: six attempts over 7 h 35 min, then dead, visible and resendable; fails_when=a seventh attempt, an earlier
    dead state, a dead row nobody sees, or a resend that does not go out; no_claim=none beyond protects."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch, 500)
    rec, _ = svc.add(URL)
    add(st)
    start = clock.now
    for wait in (0,) + N.BACKOFF[1:]:
        clock.tick(wait)
        svc.run_once()
    assert clock.now - start == 7 * 3600 + 35 * 60 + 5 and len(rx.calls) == 6
    r = rows(svc)[0]
    assert (r["state"], r["attempts"], r["last_status"]) == ("dead", 6, 500)
    clock.tick(10 * 3600)
    svc.run_once()
    st_ = svc.receivers()[0]["status"]
    assert len(rx.calls) == 6 and (st_["dead"], st_["pending"], st_["last_error"]) == (1, 0, "http_500")
    rx.answers = [200]
    assert svc.resend(rec["id"]) == 1
    svc.run_once()
    assert rows(svc)[0]["state"] == "delivered" and svc.receivers()[0]["status"]["dead"] == 0


def test_slow_down_answers_wait_longer_and_retry_after_is_honoured_up_to_an_hour(tmp_path, monkeypatch):
    """protects=429/502/503/504 back off further, Retry-After up to 1 hour; fails_when=a Retry-After is ignored, honoured beyond
    an hour, or a slow-down waits no longer than a plain failure; no_claim=HTTP-date Retry-After (parsed, not tested here)."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch, 503)
    svc.add(URL)
    add(st)
    svc.run_once()
    assert rows(svc)[0]["next_at"] == clock.now + 10                      # twice the 5 s
    rx.headers = {"retry-after": "120"}
    clock.tick(10)
    svc.run_once()
    assert rows(svc)[0]["next_at"] == clock.now + 600                     # 2 x 300 s beats 120 s
    rx.headers = {"retry-after": "86400"}
    clock.tick(600)
    svc.run_once()
    assert rows(svc)[0]["next_at"] == clock.now + 3600                    # 2 x 1800 s, and a day is cut to an hour
    rx.answers, rx.headers = [429], {"retry-after": "5000"}
    clock.tick(3600)
    svc.run_once()
    assert rows(svc)[0]["next_at"] == clock.now + 14400                   # 2 x 2 h


@pytest.mark.parametrize("status", [410, 413])
def test_410_and_413_refuse_that_delivery_and_later_notices_still_go(tmp_path, monkeypatch, status):
    """protects=D9-5 and criterion 8; fails_when=a retry follows, or the receiver gets no later notice; no_claim=none."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch, status, 200)
    svc.add(URL)
    add(st)
    svc.run_once()
    clock.tick(3600)
    svc.run_once()
    assert len(rx.calls) == 1 and rows(svc)[0]["state"] == "refused"
    add(st, "tom")
    svc.run_once()
    assert len(rx.calls) == 2 and [r["state"] for r in rows(svc)] == ["refused", "delivered"]


def test_order_per_request_and_receiver_without_holding_other_requests(tmp_path, monkeypatch):
    """protects=criterion 9; fails_when=a later notice for the same request overtakes one being retried, or another request waits
    for it; no_claim=order across requests (there is none)."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch, 500, 200)
    svc.add(URL)
    a = add(st, "jana")
    svc.run_once()                                                        # a's queued notice fails once
    st.claim(a.id, "discarded")
    b = add(st, "tom")
    svc.run_once()
    assert [(x["data"]["id"], x["data"]["status"]) for x in rx.bodies()] == [(a.id, "waiting"), (b.id, "waiting")]
    clock.tick(5)
    svc.run_once()
    assert [(x["data"]["id"], x["data"]["status"]) for x in rx.bodies()][2:] == [(a.id, "waiting"), (a.id, "discarded")]


# -- the cursor -------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("log_on", [True, False])
def test_the_cursor_never_passes_a_gap_and_since_fetches_it(tmp_path, monkeypatch, log_on):
    """protects=the review's cursor point and criterion 10: two receivers that progress differently; dead notices followed by a
    delivered one keep the cursor below them; approval_status(since=<cursor>) returns the requests missed, the same with the log
    off; fails_when=a later success moves a cursor past a gap, or since misses a request; no_claim=an old log-hash cursor (9a's
    tests cover it)."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch, hosts=(HOST, "other.example.net"))
    good, _ = svc.add(URL)
    bad, _ = svc.add("https://other.example.net/hook")
    missed, to_bad = set(), []

    def route(host, ip, port, path, headers, data, ctx, timeout):
        if host == HOST:
            return rx(host, ip, port, path, headers, data, ctx, timeout)
        to_bad.append(json.loads(data))
        return (500 if to_bad[-1]["data"]["id"] in missed else 200), {}
    svc.transport = route
    ledger = ApprovalLedger({"external": st}, AuditLog(str(tmp_path), enabled=log_on), kinds=kinds)
    first = add(st, "jana")
    st.claim(first.id, "discarded")
    svc.run_once()
    order = []
    for who in ("tom", "sam", "lea"):
        q = add(st, who)
        missed.add(q.id)
        order.append(q.id)
        st.claim(q.id, "withdrawn")
    for wait in (0,) + N.BACKOFF[1:] * 2:                                 # queued goes dead, then withdrawn (one request in order)
        clock.tick(wait)
        svc.run_once()
    last = add(st, "ben")                                                 # delivered to both after the others went dead
    st.claim(last.id, "discarded")
    svc.run_once()
    assert {r["state"] for r in rows(svc, bad["id"]) if r["request_id"] in missed} == {"dead"}
    assert [r["state"] for r in rows(svc, bad["id"])][-2:] == ["delivered", "delivered"]
    assert rx.bodies()[-1]["data"]["cursor"] == f"e{st.event_head()}"
    gap = f"e{st.last_event(first.id)['seq']}"
    assert [b["data"]["cursor"] for b in to_bad if b["data"]["id"] == last.id] == [gap, gap]     # held at the first dead notice
    got = ledger.since(gap)
    assert [i["id"] for i in got["items"]] == [*order, last.id] and got["more"] is False


# -- addresses --------------------------------------------------------------------------------------------------------------------
def test_receiver_urls_are_https_on_the_host_list_without_credentials_and_at_most_five(tmp_path, monkeypatch):
    """protects=the owner's lists at add time (criterion 7, first half); fails_when=http, a host not on NOTICE_HOSTS, a URL with a
    password or a sixth receiver is taken; no_claim=the dashboard's step-up (tests/test_notices_dashboard.py)."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch, hosts=(HOST,))
    for bad in ("http://hooks.example.org/x", "https://elsewhere.example.net/x", "https://user:pw@hooks.example.org/x", "hooks.example.org",
                "https://hooks.example.org:99999/x"):
        with pytest.raises(ValueError):
            svc.add(bad)
    for n in range(5):
        svc.add(f"{URL}/{n}")
    with pytest.raises(ValueError, match="At most 5"):
        svc.add(URL)
    empty = N.NoticeService(str(tmp_path), [st], hosts=())
    with pytest.raises(ValueError, match="not on Allowed hosts"):
        empty.add(URL)
    assert stat.S_IMODE(os.stat(tmp_path / "notices.json").st_mode) == 0o600


@pytest.mark.parametrize("ip, public, private", [
    ("93.184.216.34", True, True),
    ("127.0.0.1", False, True),
    ("10.1.2.3", False, True),                       # privacy-ok: a made-up RFC 1918 address
    ("192.168.1.20", False, True),                   # privacy-ok
    ("172.16.0.9", False, True),                     # privacy-ok
    ("100.64.0.1", False, True),
    ("169.254.169.254", False, False),
    ("::1", False, True),
    ("fd00::1", False, True),
    ("fe80::1%en0", False, False),
    ("::ffff:127.0.0.1", False, True),
    ("0.0.0.0", False, False),
    ("224.0.0.1", False, False),
    ("not an address", False, False),
])
def test_addresses(ip, public, private):
    """protects=the SSRF rule: globally routable only, private ones only for NOTICE_PRIVATE_HOSTS, link-local (metadata),
    multicast and unspecified never; fails_when=one of these answers differs; no_claim=DNS rebinding beyond one resolution (each
    attempt resolves once and connects to the address it checked)."""
    assert N.address_ok(ip, False) is public and N.address_ok(ip, True) is private


@pytest.mark.parametrize("answer", [["127.0.0.1"], ["10.0.0.7"], ["169.254.169.254"], [PUBLIC_IP, "192.168.0.10"]])  # privacy-ok
def test_nothing_is_sent_to_an_address_that_is_not_allowed_at_delivery_time(tmp_path, monkeypatch, answer):
    """protects=criterion 7: the host is resolved at each attempt and every address is checked; fails_when=the transport is
    called for a loopback, private or metadata address, or for a name that also resolves to one; no_claim=none beyond protects."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch, resolve=lambda h, p: answer)
    svc.add(URL)
    add(st)
    svc.run_once()
    assert rx.calls == [] and rows(svc)[0]["last_error"] == "address_not_allowed" and rows(svc)[0]["state"] == "pending"


def test_a_private_host_reaches_its_private_address_and_a_dropped_host_is_not_reached(tmp_path, monkeypatch):
    """protects=NOTICE_PRIVATE_HOSTS and a host list checked at delivery; fails_when=a private host is refused, the transport is
    given another address than the checked one, or a host no longer on NOTICE_HOSTS is reached; no_claim=none."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch, private=(HOST,), resolve=lambda h, p: ["10.0.0.7"])  # privacy-ok
    svc.add(URL)
    add(st)
    svc.run_once()
    assert rx.calls[0]["ip"] == "10.0.0.7" and rx.calls[0]["host"] == HOST and rx.calls[0]["path"] == "/sendgate"  # privacy-ok
    svc.hosts = set()
    add(st, "tom")
    svc.run_once()
    assert len(rx.calls) == 1 and rows(svc)[-1]["last_error"] == "host_not_allowed"


def test_a_redirect_is_a_failure_and_is_never_followed(tmp_path, monkeypatch):
    """protects=criterion 7's redirect case; fails_when=a 3xx counts as delivered or anything is sent to its Location;
    no_claim=none beyond protects."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch, 302)
    rx.headers = {"location": "http://127.0.0.1/steal"}
    svc.add(URL)
    add(st)
    svc.run_once()
    assert len(rx.calls) == 1 and rx.calls[0]["ip"] == PUBLIC_IP
    r = rows(svc)[0]
    assert (r["state"], r["last_status"], r["last_error"]) == ("pending", 302, "redirect")


# -- fan-out, generations, restarts -----------------------------------------------------------------------------------------------
def test_a_crash_during_the_fan_out_loses_nothing_and_makes_nothing_twice(tmp_path, monkeypatch):
    """protects=the review's fan-out point: one transaction, idempotent, with its progress; fails_when=a crash leaves half the
    deliveries, moves the progress, or a rerun makes a delivery twice; no_claim=none beyond protects."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch)
    svc.add(URL)
    svc.add(URL + "/two")
    for who in ("jana", "tom", "sam"):
        add(st, who)
    real, n = N.new_event_id, iter(range(10))

    def flaky():
        if next(n) == 2:
            raise RuntimeError("the process stopped here")
        return real()
    monkeypatch.setattr(N, "new_event_id", flaky)
    with pytest.raises(RuntimeError):
        svc.fan_out()
    assert rows(svc) == [] and svc._progress() == 0
    monkeypatch.setattr(N, "new_event_id", real)
    assert svc.fan_out() == 6 and svc.fan_out() == 0 and len(rows(svc)) == 6


def test_events_written_while_no_worker_ran_are_delivered_after_the_start(tmp_path, monkeypatch):
    """protects=a crash between an event and its deliveries loses nothing: the progress is a row; fails_when=an event made while
    the service was down is never sent; no_claim=none beyond protects."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch)
    rec, _ = svc.add(URL)
    svc.close()
    q = add(st)
    st.claim(q.id, "discarded")
    again = N.NoticeService(str(tmp_path), [st], hosts=(HOST,), resolve=lambda h, p: [PUBLIC_IP], transport=rx)
    again.run_once()
    assert [(b["data"]["id"], b["data"]["status"]) for b in rx.bodies()] == [(q.id, "waiting"), (q.id, "discarded")]


def test_a_new_receiver_starts_at_the_head_and_a_new_url_is_a_new_generation(tmp_path, monkeypatch):
    """protects=the review's generations: no backlog for a new receiver; a new URL drops the old generation's pending deliveries,
    gets a new secret and starts at the head, also while retries run; removing drops everything; fails_when=a backlog is sent, a
    pending retry reaches the new URL, the old secret signs for it, or a removed receiver is reached; no_claim=none."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch, 500, hosts=(HOST, "new.example.net"))
    add(st, "jana")
    rec, old = svc.add(URL)
    svc.run_once()
    assert rx.calls == []                                                 # nothing from before
    add(st, "tom")
    svc.run_once()
    assert len(rx.calls) == 1 and rows(svc)[0]["state"] == "pending"
    assert svc.loosens(rec["id"], url="https://new.example.net/hook") and not svc.loosens(rec["id"], url=URL)
    shown, secret = svc.change(rec["id"], url="https://new.example.net/hook")
    assert shown["generation"] == 2 and secret not in (None, old) and rows(svc) == []
    rx.answers = [200]
    clock.tick(5)
    svc.run_once()
    assert len(rx.calls) == 1                                             # the dropped retry went nowhere
    add(st, "sam")
    svc.run_once()
    c = rx.calls[-1]
    assert c["host"] == "new.example.net" and verify(secret, c["headers"], c["body"], clock.now)
    assert not verify(old, c["headers"], c["body"], clock.now) and rows(svc)[-1]["generation"] == 2
    assert svc.remove(rec["id"])["host"] == "new.example.net" and rows(svc) == [] and svc.receivers() == []
    add(st, "lea")
    svc.run_once()
    assert len(rx.calls) == 2


def test_a_filter_picks_kinds_types_and_statuses_and_a_wider_one_loosens(tmp_path, monkeypatch):
    """protects=the receiver's filter (empty means all) and what counts as widening it; fails_when=a notice outside the filter is
    sent, or widening it is not seen as loosening; no_claim=none beyond protects."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch)
    rec, _ = svc.add(URL, {"kinds": ["mail"], "statuses": ["sent", "discarded"]})
    a = add(st, "jana", action_type="mail.send")
    b = add(st, "tom", action_type="t.echo")
    st.claim(a.id, "discarded")
    st.claim(b.id, "discarded")
    svc.run_once()
    assert [(x["data"]["id"], x["data"]["status"]) for x in rx.bodies()] == [(a.id, "discarded")]
    assert svc.loosens(rec["id"], filt={"kinds": [], "statuses": ["sent", "discarded"]})
    assert svc.loosens(rec["id"], filt={"kinds": ["mail", "external"], "statuses": ["sent", "discarded"]})
    assert not svc.loosens(rec["id"], filt={"kinds": ["mail"], "statuses": ["sent"]})
    with pytest.raises(ValueError, match="Statuses"):
        svc.add(URL, {"statuses": ["approved"]})


def test_a_stop_while_waiting_for_the_second_try_and_one_on_the_wire(tmp_path, monkeypatch):
    """protects=criterion 4 (simulated in one process: the service and the store are closed and opened again, as a restart does);
    fails_when=a delivery waiting for its retry is lost, or one on the wire at the stop is not sent again with the same
    webhook-id; no_claim=a real SIGKILL of the server process."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch, 500)
    svc.add(URL)
    add(st, "jana")
    svc.run_once()                                                        # first try failed; the second is due in 5 s
    q = add(st, "tom")

    class Stopped(BaseException):
        pass

    def on_the_wire(*a):
        rx(*a)                                                            # the receiver got it ...
        raise Stopped()                                                   # ... and the process stopped before the answer was read
    svc.transport = on_the_wire
    rx.answers = [200]
    with pytest.raises(Stopped):
        svc.run_once()
    svc._db.close()
    st._db.close()
    release(tmp_path)
    st = S.Store(str(tmp_path), "external", TTL, 50)
    again = N.NoticeService(str(tmp_path), [st], hosts=(HOST,), resolve=lambda h, p: [PUBLIC_IP], transport=rx)
    clock.tick(N.BACKOFF[1])
    again.run_once()
    assert {r["state"] for r in rows(again)} == {"delivered"}
    ids = [c["headers"]["webhook-id"] for c in rx.calls if json.loads(c["body"])["data"]["id"] == q.id]
    assert len(ids) == 2 and len(set(ids)) == 1


def test_an_expiry_with_no_other_traffic_is_sent_within_two_minutes(tmp_path, monkeypatch):
    """protects=criterion 11: the worker reads the store once a minute; fails_when=the expired notice waits for other traffic or
    takes longer than 2 minutes; no_claim=the thread's own timing (run_once is called here as the loop calls it)."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch)
    svc.add(URL)
    q = add(st)
    svc.run_once()
    ran_out = clock.now + 10
    st._db.execute("UPDATE request SET expires_at = ? WHERE id = ?", (ran_out, q.id))
    while clock.now < ran_out + 120 and not any(b["data"]["status"] == "expired" for b in rx.bodies()):
        clock.tick(1)
        svc.run_once()
    assert rx.bodies()[-1]["data"]["status"] == "expired" and clock.now - ran_out <= 120


def test_ended_rows_go_after_keep_days_and_the_queue_per_receiver_is_bounded(tmp_path, monkeypatch):
    """protects=retention (AUDIT_KEEP_DAYS) and the bounded queue; fails_when=ended rows stay forever, a pending row is dropped by
    the retention, or more than MAX_PENDING wait; no_claim=none beyond protects."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch, keep_days=1)
    svc.add(URL)
    add(st, "jana")
    svc.run_once()
    rx.answers = [500]
    add(st, "tom")
    svc.run_once()
    clock.tick(2 * 86400)
    svc._retained_at = 0
    svc.retain()
    assert [r["state"] for r in rows(svc)] == ["pending"]
    monkeypatch.setattr(N, "MAX_PENDING", 2)
    for who in ("sam", "lea", "ben"):
        add(st, who)
    svc.fan_out()
    assert [(r["state"], r["last_error"]) for r in rows(svc)] == [("dead", "queue_full"), ("dead", "queue_full"), ("pending", None),
                                                                     ("pending", None)]


def test_a_receiver_failing_for_a_day_gets_a_health_line_with_a_code(tmp_path, monkeypatch):
    """protects=the owner sees a receiver that keeps failing; fails_when=no line after 24 hours, a line before, or a line with a
    secret or a URL path; no_claim=none beyond protects."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch, 500)
    _, secret = svc.add(URL)
    add(st)
    svc.run_once()
    assert svc.problems() == []
    for wait in N.BACKOFF[1:]:
        clock.tick(wait)
        svc.run_once()
    clock.tick(17 * 3600)
    assert svc.problems() == [f"Notices to {HOST} have failed for more than 24 hours (http_500)."]
    assert secret not in json.dumps(svc.problems()) and "/sendgate" not in json.dumps(svc.problems())


def test_the_secret_is_never_logged_or_shown_again(tmp_path, monkeypatch, caplog):
    """protects=criterion 12 for the service itself: no log line, receiver list, health line or error holds a secret;
    fails_when=any of them does; no_claim=the dashboard's answers and the activity log (tests/test_notices_dashboard.py)."""
    caplog.set_level(logging.DEBUG)
    clock, st, svc, rx = setup(tmp_path, monkeypatch, 500, OSError("refused"))
    rec, s1 = svc.add(URL)
    s2 = svc.rotate(rec["id"])
    _, s3 = svc.change(rec["id"], url=URL + "/v2")
    add(st)
    for wait in (0,) + N.BACKOFF[1:]:
        clock.tick(wait)
        svc.run_once()
    clock.tick(86400)
    out = caplog.text + json.dumps(svc.receivers()) + json.dumps(svc.problems()) + json.dumps(rows(svc))
    for s in (s1, s2, s3):
        assert s[6:] not in out
    assert svc.receivers()[0]["status"]["last_error"] == "connect_failed"


# -- a real HTTPS receiver --------------------------------------------------------------------------------------------------------
def certificate(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(days=1)).not_valid_after(now + _dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
    cert_file, key_file = tmp_path / "test-cert.pem", tmp_path / "test-key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return cert_file, key_file


class HttpsReceiver:
    """A local HTTPS server with a test certificate for hooks.example.org that checks every notice as a receiver would."""

    def __init__(self, tmp_path, answers, secret_box, now):
        cert, key = certificate(tmp_path)
        self.got, self.answers = [], list(answers)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = self.rfile.read(int(self.headers["content-length"]))
                h = {k.lower(): v for k, v in self.headers.items()}
                outer.got.append({"headers": h, "body": data, "valid": verify(secret_box[0], h, data, now())})
                status = outer.answers.pop(0) if len(outer.answers) > 1 else outer.answers[0]
                self.send_response(status)
                self.send_header("content-length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        self.client_ctx = ssl.create_default_context(cafile=str(cert))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_a_local_https_receiver_gets_every_notice_signed_and_a_retry_with_one_id(tmp_path, monkeypatch):
    """protects=criteria 1 and 2 over real TLS: a receiver on NOTICE_PRIVATE_HOSTS (loopback here) gets one notice per row of the
    status table, each verified against its secret; a wrong secret, a changed byte and a timestamp older than 5 minutes fail the
    receiver's check; a retried notice verifies with a new timestamp and the same webhook-id; fails_when=any notice is missing,
    unsigned, wrongly signed or sent to another address; no_claim=the deployment's egress list (outside the server)."""
    clock, st, _, _ = setup(tmp_path, monkeypatch)
    box = [""]
    rx = HttpsReceiver(tmp_path, [500, 200], box, clock.time)
    try:
        svc = N.NoticeService(str(tmp_path), [st], hosts=(HOST,), private_hosts=(HOST,), kinds=kinds,
                              resolve=lambda h, p: ["127.0.0.1"], ssl_context=rx.client_ctx)
        _, box[0] = svc.add(f"https://{HOST}:{rx.port}/sendgate")
        want = every_row_of_the_status_table(tmp_path, st)
        st = reopen(st, svc, tmp_path)
        svc.run_once()
        clock.tick(5)
        svc.run_once()
        first = rx.got[0]
        retry = next(g for g in rx.got[1:] if g["headers"]["webhook-id"] == first["headers"]["webhook-id"])
        assert first["headers"]["webhook-timestamp"] != retry["headers"]["webhook-timestamp"] and first["valid"] and retry["valid"]
        bodies = [json.loads(g["body"]) for g in rx.got if g is not first]
        assert per_request((b["data"]["id"], b["data"]["status"], b["data"]["reason"]) for b in bodies) == per_request(want)
        assert len(bodies) == len(want)
        assert all(g["valid"] for g in rx.got) and {g["headers"]["host"] for g in rx.got} == {f"{HOST}:{rx.port}"}
        assert {g["headers"]["x-mcp-subscription-id"] for g in rx.got} == {svc.receivers()[0]["id"]}
        g = rx.got[-1]
        assert not verify(N.new_secret(), g["headers"], g["body"], clock.now)
        assert not verify(box[0], g["headers"], g["body"][:-2] + b"x}", clock.now)
        assert not verify(box[0], g["headers"], g["body"], clock.now + 301)
        assert {r["state"] for r in rows(svc)} == {"delivered"}
    finally:
        rx.close()


def test_a_certificate_for_another_name_is_refused(tmp_path, monkeypatch):
    """protects=TLS is verified for the host name although the connection goes to a checked address; fails_when=a notice reaches
    a server whose certificate is not for the receiver's host; no_claim=revocation."""
    clock, st, _, _ = setup(tmp_path, monkeypatch, hosts=(HOST, "other.example.net"))
    rx = HttpsReceiver(tmp_path, [200], [""], clock.time)
    try:
        svc = N.NoticeService(str(tmp_path), [st], hosts=("other.example.net",), private_hosts=("other.example.net",),
                              resolve=lambda h, p: ["127.0.0.1"], ssl_context=rx.client_ctx)
        svc.add(f"https://other.example.net:{rx.port}/hook")
        add(st)
        svc.run_once()
        assert rx.got == [] and rows(svc)[0]["last_error"] == "tls_failed"
    finally:
        rx.close()


def test_the_worker_thread_delivers_without_being_asked(tmp_path, monkeypatch):
    """protects=start(): one thread that wakes on its own; fails_when=nothing is sent until someone calls run_once;
    no_claim=the 30-second and one-minute timings (run_once tests cover the schedule)."""
    st = S.Store(str(tmp_path), "external", 3600, 50)
    rx = Receiver(200)
    svc = N.NoticeService(str(tmp_path), [st], hosts=(HOST,), resolve=lambda h, p: [PUBLIC_IP], transport=rx)
    svc.start()
    try:
        svc.add(URL)
        add(st)
        for _ in range(100):
            if rx.calls:
                break
            threading.Event().wait(0.05)
        assert len(rx.calls) == 1 and [t.name for t in threading.enumerate()].count("icloud-notices") == 1
    finally:
        svc.close()


# -- rollback ---------------------------------------------------------------------------------------------------------------------
OLD = """
import importlib.util, json, sys
from icloud_mcp.gate import store as S
d = sys.argv[1]
st = S.Store(d, "external", 3600, 50)
ids = json.load(open(d + "/ids.json"))
st.claim(ids["tom"], "discarded")
raw = json.dumps({"action_type": "t.echo", "params": {"to": "sam@example.org", "text": "Hello sam"}}, sort_keys=True).encode()
ids["sam"] = st.add(raw, ["sam@example.org"], action_type="t.echo").id
json.dump(ids, open(d + "/ids.json", "w"))
print(json.dumps({"notices": importlib.util.find_spec("icloud_mcp.gate.notices") is not None,
                  "user_version": st._db.execute("PRAGMA user_version").fetchone()[0]}))
"""


def test_the_previous_version_starts_ignores_the_notice_tables_and_the_next_start_sends_what_it_decided(tmp_path, monkeypatch):
    """protects=D9-8 and criterion 14: the previous version (commit acebbf9, 9a) starts on a DATA_DIR with the notice tables,
    has no notice code, sends nothing and leaves the tables as they were; this version afterwards delivers the pending notices and
    the events the previous version wrote to the ledger meanwhile; fails_when=the previous version does not start, the store
    version changes, a notice row changes while it ran, or a decision it made is never sent; no_claim=notices that were due while
    it ran arrive late, not on time."""
    if subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", f"{BEFORE}^{{commit}}"], capture_output=True).returncode:
        pytest.skip(f"commit {BEFORE} is not in this checkout")
    old = tmp_path / "previous"
    old.mkdir()
    with tarfile.open(fileobj=io.BytesIO(subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", BEFORE, "src"],
                                                         capture_output=True, check=True).stdout)) as tar:
        tar.extractall(old, filter="data")
    data = tmp_path / "data"
    data.mkdir()
    clock = Clock()
    clock.now = time.time()                                               # the previous version runs on the real clock
    monkeypatch.setattr(S, "time", clock)
    monkeypatch.setattr(N, "time", clock)
    monkeypatch.setattr(N, "random", types.SimpleNamespace(random=lambda: 0.5))
    st = S.Store(str(data), "external", 3600, 50)
    rx = Receiver(500)
    svc = N.NoticeService(str(data), [st], hosts=(HOST,), resolve=lambda h, p: [PUBLIC_IP], transport=rx)
    svc.add(URL)
    ids = {"jana": add(st, "jana").id, "tom": add(st, "tom").id}
    svc.run_once()
    (data / "ids.json").write_text(json.dumps(ids))
    before = rows(svc)
    svc.close()
    st._db.close()
    release(data)
    got = subprocess.run([sys.executable, "-c", OLD, str(data)], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "PYTHONPATH": str(old / "src")}, cwd=str(tmp_path))
    assert got.returncode == 0, got.stderr
    assert json.loads(got.stdout.strip().splitlines()[-1]) == {"notices": False, "user_version": 2}
    ids = json.loads((data / "ids.json").read_text())
    st = S.Store(str(data), "external", 3600, 50)                         # this version again: 9a reconciles what the old one did
    rx.answers = [200]
    svc = N.NoticeService(str(data), [st], hosts=(HOST,), resolve=lambda h, p: [PUBLIC_IP], transport=rx)
    try:
        assert rows(svc) == before
        clock.tick(N.BACKOFF[1])
        svc.run_once()
        assert [(b["data"]["id"], b["data"]["status"]) for b in rx.bodies()][2:] == [
            (ids["jana"], "waiting"), (ids["tom"], "waiting"), (ids["sam"], "waiting"), (ids["tom"], "discarded")]
        assert {r["state"] for r in rows(svc)} == {"delivered"} and st._db.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        svc.close()
        st._db.close()
        release(data)


# -- review of 9b (Codex): each test below failed on commit 19e2a3c ------------------------------------------------------------
def test_review1_a_crash_around_a_url_change_never_loses_pending_deliveries_or_sends_them_to_the_new_url(tmp_path, monkeypatch):
    """protects=review finding 1: a URL change is one recoverable step (config first, then the old generation's rows, which a
    start drops if the step stopped in between); fails_when=a crash leaves the old URL with its pending deliveries gone, or the new
    URL gets the old generation's notices; no_claim=a crash inside SQLite's own commit."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch, 500, hosts=(HOST, "new.example.net"))
    rec, _ = svc.add(URL)
    add(st)
    svc.run_once()
    assert [r["state"] for r in rows(svc)] == ["pending"]

    def no_disk(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(svc, "_save", no_disk)                           # the config write fails: nothing may change
    with pytest.raises(OSError):
        svc.change(rec["id"], url="https://new.example.net/hook")
    assert svc.receivers()[0]["url"] == URL and svc.receivers()[0]["generation"] == 1
    again = N.NoticeService(str(tmp_path), [st], hosts=(HOST, "new.example.net"), resolve=lambda h, p: [PUBLIC_IP], transport=rx)
    assert again.receivers()[0]["url"] == URL and [r["state"] for r in rows(again)] == ["pending"]
    monkeypatch.undo()
    monkeypatch.setattr(N, "time", clock)
    monkeypatch.setattr(N, "_Tx", lambda db: (_ for _ in ()).throw(RuntimeError("the process stopped here")))
    with pytest.raises(RuntimeError):                                     # stopped after the config, before the rows
        again.change(rec["id"], url="https://new.example.net/hook")
    monkeypatch.undo()
    monkeypatch.setattr(N, "time", clock)
    rx.answers = [200]
    third = N.NoticeService(str(tmp_path), [st], hosts=(HOST, "new.example.net"), resolve=lambda h, p: [PUBLIC_IP], transport=rx)
    assert third.receivers()[0]["generation"] == 2 and rows(third) == []
    clock.tick(3600)
    third.run_once()
    assert all(c["host"] == HOST for c in rx.calls)


def test_review2_a_receiver_that_trickles_its_answer_is_cut_off_at_the_deadline(tmp_path, monkeypatch):
    """protects=review finding 2: the timeout is one deadline for the whole attempt after the connection is made, not per read;
    fails_when=a receiver that sends a byte now and then holds the worker past the deadline; no_claim=the connect's own timeout."""
    clock, st, _, _ = setup(tmp_path, monkeypatch)
    cert, key = certificate(tmp_path)

    class Trickle(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["content-length"]))
            try:
                self.wfile.write(b"HTTP/1.1 200 OK\r\n")
                for _ in range(12):                                       # 6 s in all, a line every half second, then it stops
                    time.sleep(0.5)
                    self.wfile.write(b"x-slow: 1\r\n")
                self.wfile.write(b"content-length: 0\r\n\r\n")
            except OSError:
                pass

        def log_message(self, *a):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
    server.daemon_threads, server.block_on_close = True, False
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(N, "TIMEOUT", 1.0)
        svc = N.NoticeService(str(tmp_path), [st], hosts=(HOST,), private_hosts=(HOST,), resolve=lambda h, p: ["127.0.0.1"],
                              ssl_context=ssl.create_default_context(cafile=str(cert)))
        svc.add(f"https://{HOST}:{server.server_address[1]}/hook")
        add(st)
        began = time.monotonic()
        svc.run_once()
        took = time.monotonic() - began
        assert took < 3.0, took
        assert [(r["state"], r["last_error"]) for r in rows(svc)] == [("pending", "timeout")]
    finally:
        server.shutdown()
        server.server_close()


def test_review4_every_transaction_waits_for_the_lock_and_memory_changes_only_after_the_write(tmp_path, monkeypatch):
    """protects=review finding 4: the worker's writes (_end, _failed) take the service's lock like every other transaction on the
    one connection, and add, rotate, end_old and remove change the receivers in memory only once notices.json is written;
    fails_when=a write of the worker meets another thread's open transaction, or a failed write leaves memory changed;
    no_claim=other processes (the DATA_DIR lock keeps them out)."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch)
    rec, _ = svc.add(URL)
    add(st)
    add(st, "tom")
    svc.fan_out()
    first, second = rows(svc)
    for write in (lambda: svc._end(first, "delivered", clock.now, 200), lambda: svc._failed(second, 1, clock.now, 500, None, None)):
        began, done = threading.Event(), threading.Event()

        def other():
            with svc._lock, N._Tx(svc._db):                                   # the dashboard's thread, in a transaction
                began.set()
                done.wait(0.3)
        t = threading.Thread(target=other)
        t.start()
        began.wait(2)
        try:
            write()
        finally:
            t.join()
    assert [r["state"] for r in rows(svc)] == ["delivered", "pending"] and rows(svc)[1]["last_status"] == 500

    svc.rotate(rec["id"])
    before = json.dumps(svc._receivers, sort_keys=True)

    def no_disk(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(svc, "_save", no_disk)
    for step in (lambda: svc.add("https://hooks.example.org/two"), lambda: svc.rotate(rec["id"]), lambda: svc.end_old(rec["id"]),
                 lambda: svc.remove(rec["id"])):
        with pytest.raises(OSError):
            step()
        assert json.dumps(svc._receivers, sort_keys=True) == before


def test_review5_one_worker_and_one_attempt_at_a_time_per_delivery(tmp_path, monkeypatch):
    """protects=review finding 5: one worker thread however many callers start it at once, and a delivery that is on the wire is
    not sent a second time by another caller; fails_when=two threads run the worker, or two callers send the same delivery;
    no_claim=a second process (the DATA_DIR lock keeps it out)."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch)
    svc.add(URL)
    add(st)
    svc.fan_out()
    row = rows(svc)[0]
    gate, calls = threading.Event(), []

    def slow(*a, **k):
        calls.append(1)
        gate.wait(2)
        return 200, {}
    svc.transport = slow
    t = threading.Thread(target=svc.deliver, args=(row,))
    t.start()
    for _ in range(100):
        if calls:
            break
        threading.Event().wait(0.02)
    try:
        svc.deliver(row)                                                       # a second caller while the first is on the wire
    finally:
        gate.set()
        t.join()
    assert len(calls) == 1 and [r["state"] for r in rows(svc)] == ["delivered"]

    svc.transport = Receiver(200)
    svc.autostart = True
    plain = svc.fan_out

    def slow_fan_out():
        time.sleep(0.2)
        return plain()
    monkeypatch.setattr(svc, "fan_out", slow_fan_out)
    before = sum(1 for x in threading.enumerate() if x.name == "icloud-notices")
    starters = [threading.Thread(target=svc._ensure_thread) for _ in range(2)]
    try:
        for s in starters:
            s.start()
        for s in starters:
            s.join()
        assert sum(1 for x in threading.enumerate() if x.name == "icloud-notices") - before == 1
    finally:
        svc.stop()


def test_review6_the_worker_waits_while_the_only_due_row_waits_for_an_earlier_one(tmp_path, monkeypatch):
    """protects=review finding 6: the worker's wait counts only rows _due could pick (none waiting for an earlier notice of its
    request and receiver), so a later notice held back by a retry does not make it spin; fails_when=the wait is 0 while nothing can
    be sent; no_claim=the length of a round."""
    clock, st, svc, rx = setup(tmp_path, monkeypatch, 500, 200)
    svc.add(URL)
    q = add(st)
    st.claim(q.id, "discarded")
    svc.run_once()
    assert [(r["state"], r["attempts"]) for r in rows(svc)] == [("pending", 1), ("pending", 0)]
    assert svc._due(clock.now) == []
    assert svc._next_wait() == 1.0
