"""Standing rules (docs/SENDGATE.md, package 10, "The standing release"): requests of one action type to one person go out
without waiting for the owner, until an end date.

A rule exists only once the owner approved a `gate.trust` request for it (trust.py), with a fresh sign-in. It never widens who may
be reached: the type's own list still applies, at queueing and again when the rule releases a request. It only skips the wait.

Tables, in approvals.db beside the store's (the previous version ignores them, so every request waits again after a rollback):

- `standing`: id, action type, recipient (case-folded), created, expires, the id of the request that created it, revoked. A rule
  for the same type and recipient replaces the older one, which is marked revoked.
- `standing_use`: rule id, request id, time. The daily limit counts a rule's uses over the last 24 hours.

`claim` is the one place a rule takes effect: in one transaction of the store it checks that the request still waits, that every
person it reaches has a live rule of its type, that no rule is past its daily limit, writes one use per rule and claims the
request for execution. Two requests cannot both take the last use of a day, and a rule removed before that transaction no longer
counts. Nothing here talks to anything outside this process.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, replace as dc_replace
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS standing (
  id          TEXT PRIMARY KEY,
  action_type TEXT NOT NULL,
  recipient   TEXT NOT NULL,
  created_at  REAL NOT NULL,
  expires_at  REAL NOT NULL,
  request_id  TEXT,
  revoked_at  REAL
);
CREATE INDEX IF NOT EXISTS standing_live ON standing (action_type, recipient, revoked_at);
CREATE TABLE IF NOT EXISTS standing_use (
  rule_id     TEXT NOT NULL,
  request_id  TEXT NOT NULL,
  at          REAL NOT NULL,
  PRIMARY KEY (rule_id, request_id)
);
CREATE INDEX IF NOT EXISTS standing_use_rule ON standing_use (rule_id, at);
"""
DAY = 24 * 3600
DAILY_LIMIT = 20            # D10-5: uses of one rule in 24 hours before its requests wait again
MAX_DAYS = 365              # D10-3
DEFAULT_DAYS = 90
KEEP_REVOKED_DAYS = 90      # a removed or expired rule is kept this long, so Activity can still name it


@dataclass(frozen=True)
class Rule:
    id: str
    action_type: str
    recipient: str
    created_at: float
    expires_at: float
    request_id: str | None = None
    revoked_at: float | None = None

    @property
    def live(self) -> bool:
        return self.revoked_at is None and self.expires_at > time.time()


@dataclass(frozen=True)
class Claimed:
    """What `claim` answers: the request now executing and the rules it used, or why it waits (`waits`: no_rule, limit, gone)."""
    q: Any = None
    rules: tuple[Rule, ...] = ()
    waits: str = ""


def identity(person: Any) -> str | None:
    """One person's stable identity as a rule holds it: a lower-case email address with one @ and a dot after it, or None for
    anything else (a domain, a wildcard, a name, several at once)."""
    s = str(person or "").strip().casefold()
    if not s or any(c in s for c in "*,; <>\"'") or s.count("@") != 1:
        return None
    local, _, dom = s.partition("@")
    if not local or "." not in dom or dom.startswith(".") or dom.endswith("."):
        return None
    return s


class Standing:
    """The rules of one DATA_DIR, kept in the approval store's database and claimed in its transactions.

    store: the gate's Store (store.py) of the external source; its connection, lock and transactions are used, so a claim here and
    every other change of a request are serialised alike."""

    def __init__(self, store: Any, daily_limit: int = DAILY_LIMIT):
        self.store, self.daily_limit = store, max(1, int(daily_limit))
        with store._lock:
            store._db.executescript(SCHEMA)

    # -- reading ---------------------------------------------------------------------------------------------------------
    @staticmethod
    def _rule(r: Any) -> Rule:
        return Rule(r["id"], r["action_type"], r["recipient"], r["created_at"], r["expires_at"], r["request_id"], r["revoked_at"])

    def get(self, rule_id: str) -> Rule | None:
        with self.store._lock:
            r = self.store._db.execute("SELECT * FROM standing WHERE id = ?", (rule_id,)).fetchone()
            return self._rule(r) if r is not None else None

    def live(self) -> list[dict[str, Any]]:
        """Every live rule with how often it was used: in all, and in the last 24 hours. Newest first."""
        now = time.time()
        with self.store._lock:
            rows = self.store._db.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM standing_use u WHERE u.rule_id = s.id) AS uses, "
                "(SELECT COUNT(*) FROM standing_use u WHERE u.rule_id = s.id AND u.at > ?) AS today "
                "FROM standing s WHERE s.revoked_at IS NULL AND s.expires_at > ? ORDER BY s.created_at DESC", (now - DAY, now)).fetchall()
        return [{"id": r["id"], "action_type": r["action_type"], "recipient": r["recipient"], "created_at": r["created_at"],
                 "expires_at": r["expires_at"], "request_id": r["request_id"], "uses": r["uses"], "today": r["today"],
                 "limit": self.daily_limit} for r in rows]

    # -- changing --------------------------------------------------------------------------------------------------------
    def add(self, action_type: str, recipient: str, days: int, request_id: str | None = None, expires_at: float | None = None) -> Rule:
        """A rule for this type and person, until `days` from now (1 to 365), or until expires_at when given (the end date the owner
        saw on the card, never further than 365 days ahead). An older live rule for the same pair is revoked in the same
        transaction, so there is at most one. ValueError for a recipient that is not one person, days out of range, or an end
        date that has passed."""
        who = identity(recipient)
        if who is None:
            raise ValueError("A rule names one person by email address.")
        days = int(days)
        if not 1 <= days <= MAX_DAYS:
            raise ValueError(f"days must be 1 to {MAX_DAYS}.")
        now = time.time()
        end = now + days * DAY if expires_at is None else min(float(expires_at), now + MAX_DAYS * DAY)
        if end <= now:
            raise ValueError("The end date has passed.")
        rule = Rule("r_" + secrets.token_urlsafe(9), action_type, who, now, end, request_id)
        with self.store._lock, self.store._tx():
            self.store._db.execute("UPDATE standing SET revoked_at = ? WHERE action_type = ? AND recipient = ? AND revoked_at IS NULL",
                                   (now, action_type, who))
            self.store._db.execute("INSERT INTO standing (id, action_type, recipient, created_at, expires_at, request_id) VALUES (?,?,?,?,?,?)",
                                   (rule.id, rule.action_type, rule.recipient, rule.created_at, rule.expires_at, rule.request_id))
            self._prune(now)
        return rule

    def remove(self, rule_id: str) -> Rule | None:
        """Ends a rule at once (the owner, in Settings; no approval, because it only makes requests wait again). The rule as it
        was, or None when there is no live rule with this id."""
        now = time.time()
        with self.store._lock, self.store._tx():
            r = self.store._db.execute("SELECT * FROM standing WHERE id = ? AND revoked_at IS NULL", (rule_id,)).fetchone()
            if r is None:
                return None
            self.store._db.execute("UPDATE standing SET revoked_at = ? WHERE id = ?", (now, rule_id))
        return self._rule(r)

    def _prune(self, now: float) -> None:
        """Inside a transaction: drop rules that ended long ago, and their uses."""
        cut = now - KEEP_REVOKED_DAYS * DAY
        self.store._db.execute("DELETE FROM standing_use WHERE rule_id IN (SELECT id FROM standing WHERE COALESCE(revoked_at, expires_at) < ?)", (cut,))
        self.store._db.execute("DELETE FROM standing WHERE COALESCE(revoked_at, expires_at) < ?", (cut,))

    # -- the standing release ----------------------------------------------------------------------------------------------
    def claim(self, item_id: str, action_type: str, people: Iterable[str]) -> Claimed:
        """In one transaction: the request still waits, every person has a live rule of this type, none is past its daily limit;
        then one use per rule is written and the request is claimed for execution (as the owner's click claims it). Otherwise
        nothing changes and `waits` says why: gone (no longer waiting), no_rule, limit."""
        who = [identity(p) for p in people]
        if not who or any(w is None for w in who):
            return Claimed(waits="no_rule")
        who = list(dict.fromkeys(who))
        st = self.store
        with st._lock, st._tx():
            now = time.time()                    # read under the lock, so a rule or a request that ran out meanwhile does not count
            # tried once, when first queued: a request the owner already approved once (a failure put it back) is theirs
            r = st._db.execute("SELECT * FROM request WHERE id = ? AND source = ? AND state = 'waiting' AND attempts = 0 AND expires_at > ?",
                               (item_id, st.source, now)).fetchone()
            if r is None:
                return Claimed(waits="gone")
            rules: list[Rule] = []
            for w in who:
                got = st._db.execute("SELECT * FROM standing WHERE action_type = ? AND recipient = ? AND revoked_at IS NULL AND expires_at > ?",
                                     (action_type, w, now)).fetchone()
                if got is None:
                    return Claimed(waits="no_rule")
                rules.append(self._rule(got))
            for rule in rules:
                used = st._db.execute("SELECT COUNT(*) FROM standing_use WHERE rule_id = ? AND at > ?", (rule.id, now - DAY)).fetchone()[0]
                if used >= self.daily_limit:
                    return Claimed(waits="limit")
            for rule in rules:
                st._db.execute("INSERT OR IGNORE INTO standing_use (rule_id, request_id, at) VALUES (?,?,?)", (rule.id, item_id, now))
            st._db.execute("UPDATE request SET state = 'executing', attempts = attempts + 1 WHERE id = ?", (item_id,))
            q = st._q(r)
        return Claimed(dc_replace(q, attempts=q.attempts + 1), tuple(rules))
