"""approval_status: what became of something an agent queued for the owner's approval.

An agent can only queue a message, a calendar change or whatever else needs the owner; the owner releases it on the
dashboard. Queueing hands the agent an `outbox_id`, and until now nothing could tell it how the request ended, so an agent that
heard nothing had no safe choice between waiting and sending again.

This module answers from two places that already exist and keeps no copy of anything:

- the outboxes, for what is still waiting (an entry leaves the outbox when it is released, discarded or times out), and
- the activity log, for how it ended: an `approval.sent`, `approval.discarded`, `approval.failed`, `approval.expired` or
  `approval.withdrawn` (the agent took it back, approval_withdraw.py) entry that carries the same `outbox_id`.

`edited` comes from the same two places: a waiting entry that holds the text the agent asked for (`edited_from`) has been edited,
and an ended request has been edited when the newest of its `approval.edited` and `approval.edit_undone` entries is the first.
Neither says what the text was.

A record is {id, kind, status, edited, at} and nothing else: never the text, subject, names or recipients, which the log does
not hold either. `kind` is whatever the entry says (mail, imessage, calendar today) and is never checked against a list here,
so a new kind of approval needs no change in this file. The cursor that lists "what happened since" is the hash of an entry in
the log's chain, so the log needs no sequence number of its own.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

# What a record's status can be. `unknown` is the answer for an id that is not waiting and not in the log (too old, never queued
# here, or the log is off). More values may follow (changes_requested, with a note from the owner); a caller treats any value it
# does not know as "not sent".
STATUSES = ("waiting", "sent", "discarded", "withdrawn", "expired", "unclear", "unknown")

_GENESIS = "0" * 64                  # what the first entry of a log names as its predecessor
_EDITS = ("approval.edited", "approval.edit_undone")   # the owner changed the text / brought back the agent's; neither ends a request
_LOG_LIMIT = 1_000_000               # the log keeps AUDIT_KEEP_DAYS days; this is only "all of it"
_PAGE = 50                           # records per `since` answer
_MAX_ID = 64                         # outbox ids are 16 characters; anything longer is not one


class Released(str):
    """The one-line result of an approval, plus how delivery ended where the source can tell: 'sent', 'unconfirmed' or 'failed'
    (an iMessage Messages did not confirm). It reads as the string it is; the dashboard writes `delivery` into the log."""
    delivery: str | None

    def __new__(cls, text: str, delivery: str | None = None) -> "Released":
        obj = super().__new__(cls, text)
        obj.delivery = delivery
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


def _record(item_id: str, kind: str | None, status: str, t: Any, edited: bool = False) -> dict[str, Any]:
    return {"id": item_id, "kind": kind, "status": status, "edited": edited, "at": _iso(t) if t is not None else None}


class ApprovalLedger:
    def __init__(self, outboxes: dict[str, Any], audit: Any):
        """outboxes: kind -> Outbox (None entries are skipped). Also starts logging every entry that times out unanswered, which
        the outbox otherwise drops without a trace."""
        self.outboxes = {kind: box for kind, box in outboxes.items() if box is not None}
        self.audit = audit
        for kind, box in self.outboxes.items():
            box.on_expiry(lambda q, kind=kind: self._log_expiry(kind, q))

    def _log_expiry(self, kind: str, q: Any) -> None:
        self.audit.record("approval.expired", actor="server", summary=f"A queued {kind} message expired without an answer",
                          kind=kind, outbox_id=q.id, expired_at=round(q.expires_at, 3))

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

    def _answer(self, item_id: str, waiting: dict[str, tuple[str, Any]], latest: dict[str, dict[str, Any]],
                edited: dict[str, bool]) -> dict[str, Any]:
        if item_id in waiting:                      # still in the queue wins: a release that failed puts the entry back
            kind, q = waiting[item_id]
            return _record(item_id, kind, "waiting", q.created_at, getattr(q, "edited_from", None) is not None)
        e = latest.get(item_id)
        if e is None:
            return _record(item_id, None, "unknown", None)
        d = e.get("details") or {}
        kind = d.get("kind") if isinstance(d.get("kind"), str) else None
        when = d.get("expired_at") if e.get("action") == "approval.expired" and d.get("expired_at") else e.get("t")
        return _record(item_id, kind, _outcome(e) or "unknown", when, edited.get(item_id, False))

    def _note(self, out: dict[str, Any]) -> dict[str, Any]:
        if not getattr(self.audit, "enabled", True):
            out["note"] = ("The activity log is off (AUDIT_LOG=false), so only a request that is still waiting can be told apart; "
                           "everything else reads unknown.")
        return out

    # -- the two questions -----------------------------------------------------------------------------------------------
    def status(self, item_id: str) -> dict[str, Any]:
        waiting = self._waiting()
        log = self._log()
        return self._note(self._answer(item_id, waiting, self._latest(log), self._edited(log)))

    def since(self, cursor: str) -> dict[str, Any]:
        """What ended after the entry whose hash is `cursor` ('now' starts one at the end of the log), oldest first, _PAGE at a time."""
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
        - unknown: not waiting and not in the activity log (too old, never queued here, or the log is off). Do not send again blindly: check, or ask the owner.
        edited is true when the owner changed the text you queued and has not brought your original back. On a waiting request their wording is what would go out; on sent or unclear it is what went out, so it may differ from what you asked; on discarded or expired nothing was sent. It is false for unknown. More statuses may follow, for example changes_requested with a note from the owner: treat any status you do not know as not sent, and ask the owner before sending again.
        With since, pass the returned cursor next time; more=true means call again; cursor_unknown=true means it is older than the log (90 days by default) or was never issued, so ask by id. Errors: neither or both of id and since."""
        if (id is None) == (since is None):
            raise ToolError("Give exactly one of id (one outbox_id) or since (a cursor, or 'now' for the first).")
        if id is not None:
            if not id.strip() or len(id) > _MAX_ID:
                raise ToolError("id is the outbox_id from a queued_for_owner_approval result.")
            return ledger.status(id.strip())
        return ledger.since((since or "").strip())
