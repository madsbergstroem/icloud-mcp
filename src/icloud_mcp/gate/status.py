"""approval_status: what became of something an agent queued for the owner's approval.

An agent can only queue a message, a calendar change or whatever else needs the owner; the owner releases it on the
dashboard. Queueing hands the agent an `outbox_id`, and until now nothing could tell it how the request ended, so an agent that
heard nothing had no safe choice between waiting and sending again.

This module answers from the places that already exist and keeps no copy of anything:

- the outboxes, for what is still waiting (an entry leaves the outbox when it is released, discarded or times out),
- the approval store's status ledger (package 9a, gate/store.py), for how a request in the store ended, whether the log is on or
  off: its newest event, written in the transaction that ended it, and
- the activity log, for everything the ledger does not know (a JSON queue, a request ended before the ledger existed, one being
  carried out right now): an `approval.sent`, `approval.discarded`, `approval.failed`, `approval.expired` or `approval.withdrawn`
  (the agent took it back, approval_withdraw.py) entry that carries the same `outbox_id`. An event the ledger wrote afterwards for
  a change made without it (reconciled) gives way to the log where the log has the entry, so an answer stays what it was.

`edited` comes from the same two places: a waiting entry that holds the text the agent asked for (`edited_from`) has been edited,
and an ended request has been edited when the newest of its `approval.edited` and `approval.edit_undone` entries is the first.
Neither says what the text was.

A record is {id, kind, status, edited, at} and nothing else: never the text, subject, names or recipients, which the log does
not hold either. `kind` is whatever the entry says (mail, imessage, calendar, external today) and is never checked against a
list here, so a new kind of approval needs no change in this file. The one addition is `action_type` (the registered kind of
action, such as test.echo), which a record has only when its entry says it: external requests do, the others never do.

The cursor that lists "what happened since" is `e<seq>` where the store keeps the ledger: the ledger's sequence number up to which
the list went (`since='now'` hands one out). A cursor that is the hash of an entry in the log's chain, the only kind before 9a,
still works while the log is on: it is read from the log as before and hands back log cursors.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Any

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

# What a record's status can be. `unknown` is the answer for an id that is not waiting and neither in the ledger nor in the log (too
# old, never queued here, or a JSON queue's request while the log is off). More values may follow (changes_requested, with a note from the owner); a caller treats any value it
# does not know as "not sent".
STATUSES = ("waiting", "sent", "discarded", "withdrawn", "expired", "unclear", "unknown")

_GENESIS = "0" * 64                  # what the first entry of a log names as its predecessor
_EDITS = ("approval.edited", "approval.edit_undone")   # the owner changed the text / brought back the agent's; neither ends a request
_LOG_LIMIT = 1_000_000               # the log keeps AUDIT_KEEP_DAYS days; this is only "all of it"
_PAGE = 50                           # records per `since` answer
_MAX_ID = 64                         # outbox ids are 16 characters; anything longer is not one
_EVENT_CURSOR = re.compile(r"e[0-9]{1,18}")    # a ledger cursor (package 9a); a log hash is 64 hex characters, so never one
# why the server, not the owner, discarded a request (a code in the log, never a parameter), and what an agent should do about it
_REASONS = {"target_changed": "The owner did not see this target: it changed after they looked. Ask again only if the owner still wants it.",
            "not_allowed": "The owner's lists no longer allow someone it would reach, so it was not sent. Only the owner can change the lists."}


class Released(str):
    """The one-line result of an approval, plus how delivery ended where the source can tell: 'sent', 'unconfirmed' or 'failed'
    (an iMessage Messages did not confirm), and how many recipients the service refused while it took the rest (mail, package
    6a). It reads as the string it is; the dashboard writes `delivery` and `refused` into the log."""
    delivery: str | None
    refused: int

    def __new__(cls, text: str, delivery: str | None = None, refused: int = 0) -> "Released":
        obj = super().__new__(cls, text)
        obj.delivery = delivery
        obj.refused = refused
        return obj


def _iso(t: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(t), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _ids(entry: dict[str, Any]) -> list[str]:
    d = entry.get("details") or {}
    one, many = d.get("outbox_id"), d.get("outbox_ids")
    return [x for x in ([one] if isinstance(one, str) else []) + (many if isinstance(many, list) else []) if isinstance(x, str)]


def _outcome(entry: dict[str, Any]) -> str | None:
    """The status a log entry puts a request in, or None when the entry says nothing about how one ended."""
    d = entry.get("details") or {}
    action = entry.get("action")
    if action == "approval.sent":
        # a send the Mac handed to Messages without confirmation, or reported as not delivered: it may have gone out
        return "unclear" if d.get("delivery") in ("unconfirmed", "failed") else "sent"
    if action == "approval.discarded":
        return "discarded"
    if action == "approval.withdrawn":
        return "withdrawn"         # the agent that queued it took it back while it waited
    if action == "approval.expired":
        return "expired"
    if action == "approval.failed":
        return "unclear"           # the release failed and the entry did not come back: the owner has to look
    return None


def _record(item_id: str, kind: str | None, status: str, t: Any, edited: bool = False, action_type: str | None = None) -> dict[str, Any]:
    out = {"id": item_id, "kind": kind, "status": status, "edited": edited, "at": _iso(t) if t is not None else None}
    if action_type:                      # only a kind that has several registered types of action (external) says which
        out["action_type"] = action_type
    return out


class ApprovalLedger:
    def __init__(self, outboxes: dict[str, Any], audit: Any, kinds: Any = None):
        """outboxes: kind -> Outbox (None entries are skipped). Also starts logging every entry that times out unanswered, which
        the outbox otherwise drops without a trace. kinds(action_type): what a request of the external source is called in the
        answer when its type says so (mail stays "mail" when it becomes an action, D6-8), else None."""
        self.outboxes = {kind: box for kind, box in outboxes.items() if box is not None}
        self.audit = audit
        self.kinds = kinds
        # the status ledger (package 9a): one approvals.db holds the events of every source, so the first store that has it serves
        self.events = next((box for box in self.outboxes.values() if hasattr(box, "last_event")), None)
        for kind, box in self.outboxes.items():
            box.on_expiry(lambda q, kind=kind: self._log_expiry(kind, q))
            if hasattr(box, "on_recovered"):          # the store: requests a crash left half carried out
                box.on_recovered(lambda q, kind=kind: self._log_recovered(kind, q))

    def _log_recovered(self, kind: str, q: Any) -> None:
        """A request that was being carried out when the server stopped: it may have gone out, so it reads unclear and is never
        repeated. Logged as an approval.sent whose delivery is unconfirmed, as an unconfirmed send always is."""
        self.audit.record("approval.sent", actor="server", summary=f"A {kind} request was being carried out when the server stopped; "
                          "it may have gone through", kind=kind, outbox_id=q.id, delivery="unconfirmed",
                          action_type=getattr(q, "action_type", None), sha256=getattr(q, "sha256", None))

    def _log_expiry(self, kind: str, q: Any) -> None:
        what = "request" if kind == "external" else f"{kind} message"     # an action of any type, an iMessage among them (4b)
        self.audit.record("approval.expired", actor="server", summary=f"A queued {what} expired without an answer",
                          kind=kind, outbox_id=q.id, expired_at=round(q.expires_at, 3), action_type=getattr(q, "action_type", None),
                          sha256=getattr(q, "sha256", None))

    # -- reading ---------------------------------------------------------------------------------------------------------
    def _waiting(self) -> dict[str, tuple[str, Any]]:
        """Reading the queues also lets anything that has just expired write its entry, so it is in the log before the log is read."""
        return {q.id: (kind, q) for kind, box in self.outboxes.items() for q in box.pending()}

    def _log(self) -> list[dict[str, Any]]:
        return list(reversed(self.audit.entries(_LOG_LIMIT)))          # oldest first

    @staticmethod
    def _latest(log: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for e in log:
            if _outcome(e) is not None:
                for item_id in _ids(e):
                    latest[item_id] = e
        return latest

    @staticmethod
    def _edited(log: list[dict[str, Any]]) -> dict[str, bool]:
        """id -> whether its newest edit entry is `approval.edited` (an edit that was brought back again reads False)."""
        edited: dict[str, bool] = {}
        for e in log:
            if e.get("action") in _EDITS:
                for item_id in _ids(e):
                    edited[item_id] = e["action"] == "approval.edited"
        return edited

    def _kind(self, kind: str | None, action_type: str | None) -> str | None:
        """The kind an answer names: a type's own (D6-8) for a request of the external source, else the queue's."""
        if kind == "external" and action_type and self.kinds is not None:
            try:
                return self.kinds(action_type) or kind
            except Exception:  # noqa: BLE001
                return kind
        return kind

    def _answer(self, item_id: str, waiting: dict[str, tuple[str, Any]], latest: dict[str, dict[str, Any]],
                edited: dict[str, bool]) -> dict[str, Any]:
        if item_id in waiting:                      # still in the queue wins: a release that failed puts the entry back
            kind, q = waiting[item_id]
            return _record(item_id, self._kind(kind, getattr(q, "action_type", None)), "waiting", q.created_at,
                           getattr(q, "edited_from", None) is not None, getattr(q, "action_type", None))
        ev = self.events.last_event(item_id) if self.events is not None else None
        if ev is not None and ev["status"] != "waiting" and ev["reason"] != "exported" and not (ev["reconciled"] and item_id in latest):
            out = _record(item_id, self._kind(ev["kind"], ev["action_type"]), ev["status"], ev["at"], bool(ev["edited"]), ev["action_type"])
            if ev["status"] == "discarded" and ev["reason"] in _REASONS:                   # the server ended it, not the owner
                out["reason"] = ev["reason"]
                out["note"] = _REASONS[ev["reason"]]
            return out
        e = latest.get(item_id)
        if e is None:
            return _record(item_id, None, "unknown", None)
        d = e.get("details") or {}
        kind = d.get("kind") if isinstance(d.get("kind"), str) else None
        when = d.get("expired_at") if e.get("action") == "approval.expired" and d.get("expired_at") else e.get("t")
        kind_of = d.get("action_type") if isinstance(d.get("action_type"), str) else None
        out = _record(item_id, self._kind(kind, kind_of), _outcome(e) or "unknown", when, edited.get(item_id, False), kind_of)
        if e.get("action") == "approval.discarded" and d.get("reason") in _REASONS:      # the server ended it, not the owner
            out["reason"] = d["reason"]
            out["note"] = _REASONS[d["reason"]]
        return out

    def _log_on(self) -> bool:
        return bool(getattr(self.audit, "enabled", True))

    def _note(self, out: dict[str, Any]) -> dict[str, Any]:
        if self._log_on():
            return out
        if self.events is None:
            out["note"] = ("The activity log is off (AUDIT_LOG=false), so only a request that is still waiting can be told apart; "
                           "everything else reads unknown.")
        elif out.get("status") == "unknown":           # the ledger answers the rest
            out["note"] = "The activity log is off (AUDIT_LOG=false) and the approval store has no record of this id."
        return out

    # -- the two questions -----------------------------------------------------------------------------------------------
    def status(self, item_id: str) -> dict[str, Any]:
        waiting = self._waiting()
        log = self._log()
        return self._note(self._answer(item_id, waiting, self._latest(log), self._edited(log)))

    def since(self, cursor: str) -> dict[str, Any]:
        """What ended after `cursor`, oldest first, _PAGE at a time. 'now' starts one at the end of the ledger (`e<seq>`), or of the
        log where no store keeps a ledger; a log hash is read from the log as before 9a."""
        if self.events is not None and (cursor == "now" or _EVENT_CURSOR.fullmatch(cursor) or not self._log_on()):
            return self._since_events(cursor)
        waiting = self._waiting()
        log = self._log()
        head = log[-1].get("h", _GENESIS) if log else _GENESIS
        if cursor == "now":
            return self._note({"items": [], "cursor": head, "more": False})
        start = None
        if cursor == (log[0].get("prev", _GENESIS) if log else _GENESIS):
            start = 0                                    # the whole log: the first entry's predecessor, or genesis
        else:
            start = next((i + 1 for i, e in enumerate(log) if e.get("h") == cursor), None)
        if start is None:
            return self._note({"items": [], "cursor": head, "more": False, "cursor_unknown": True})
        last_at: dict[str, int] = {}                     # id -> where in the log it last ended
        for i in range(start, len(log)):
            if _outcome(log[i]) is not None:
                for item_id in _ids(log[i]):
                    last_at[item_id] = i
        ordered = sorted(last_at, key=last_at.__getitem__)
        shown, more = ordered[:_PAGE], len(ordered) > _PAGE
        latest, edited = self._latest(log), self._edited(log)
        out = {"items": [self._answer(i, waiting, latest, edited) for i in shown],
               "cursor": log[last_at[shown[-1]]].get("h", _GENESIS) if more else head, "more": more}
        return self._note(out)

    def _since_events(self, cursor: str) -> dict[str, Any]:
        """since() from the ledger: requests whose newest event after `e<seq>` ended them, in the order of those events. A cursor
        that is not one the ledger handed out (or a log hash while the log is off) reads cursor_unknown, with a cursor at the end.
        The head is read before the queue: an ending at or below it is final, so the queue read afterwards cannot show its request
        waiting, and an ending after it (one this read's expiry wrote among them) is listed by the next call."""
        head = self.events.event_head()
        waiting = self._waiting()
        log = self._log()
        seq = int(cursor[1:]) if _EVENT_CURSOR.fullmatch(cursor) else None
        if cursor == "now":
            return {"items": [], "cursor": f"e{head}", "more": False}
        if seq is None or seq > head:
            return self._note({"items": [], "cursor": f"e{head}", "more": False, "cursor_unknown": True})
        last_at: dict[str, int] = {}                     # id -> the seq of its newest ending after the cursor
        for ev in self.events.events_after(seq, head):
            last_at[ev["request_id"]] = ev["seq"]
        ordered = sorted(last_at, key=last_at.__getitem__)
        shown, more = ordered[:_PAGE], len(ordered) > _PAGE
        latest, edited = self._latest(log), self._edited(log)
        return {"items": [self._answer(i, waiting, latest, edited) for i in shown],
                "cursor": f"e{last_at[shown[-1]]}" if more else f"e{head}", "more": more}


def _D(text: str) -> Any:
    return Field(description=text)


def register(tool: Any, guard: Any, ledger: ApprovalLedger) -> None:
    """Register the tool. `tool` and `guard` are the server's own decorators, so it is listed, guarded, paused and timed like the rest."""
    @tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False))
    @guard
    def approval_status(
        id: Annotated[str | None, _D("The outbox_id of one queued request, from its queued_for_owner_approval result.")] = None,
        since: Annotated[str | None, _D("A cursor from an earlier call, or 'now' for the first: lists what was decided after it.")] = None,
    ) -> dict[str, Any]:
        """Ask what became of a message, calendar change or other action you queued for the owner's approval.

        Use when: a result said queued_for_owner_approval and you need its outcome before you tell the owner, retry or move on. Not for approving (only the owner can), for taking a request back (approval_withdraw) or for reading mail.
        Parameters: id is the outbox_id from the queued result; since is a cursor ('now' starts one) and lists what was decided after it. Give exactly one.
        Behavior: read-only. It shows only kind, status and time, never text, names or recipients. Never send again because it is still waiting: that queues a second copy.
        Returns: {id, kind, status, edited, at}; with since, {items, cursor, more} and items shaped the same. kind is an open string (mail, imessage, calendar so far). status is one of:
        - waiting: undecided. Do not send again.
        - sent: approved and carried out.
        - discarded: the owner said no. Do not send again unless asked.
        - withdrawn: you took it back with approval_withdraw. Nothing was sent. Ask the owner before queueing it again.
        - expired: nobody approved in time and nothing was sent. Queue it again only if it is still wanted.
        - unclear: it may or may not have gone out (Messages did not confirm, or the release failed). Do not send again: check the chat or Sent folder and tell the owner.
        - unknown: not waiting and not on record (too old or never queued here). Do not send again blindly: check, or ask the owner.
        edited is true when the owner changed the text you queued and has not brought your original back. On a waiting request their wording is what would go out; on sent or unclear it is what went out, so it may differ from what you asked; on discarded or expired nothing was sent. It is false for unknown. More statuses may follow, for example changes_requested with a note from the owner: treat any status you do not know as not sent, and ask the owner before sending again.
        With since, pass the returned cursor next time; more=true means call again; cursor_unknown=true means it is older than the log (90 days by default) or was never issued, so ask by id. Errors: neither or both of id and since."""
        if (id is None) == (since is None):
            raise ToolError("Give exactly one of id (one outbox_id) or since (a cursor, or 'now' for the first).")
        if id is not None:
            if not id.strip() or len(id) > _MAX_ID:
                raise ToolError("id is the outbox_id from a queued_for_owner_approval result.")
            return ledger.status(id.strip())
        return ledger.since((since or "").strip())
