"""approval_withdraw: an agent takes back a request it queued, while that request still waits for the owner.

Until now a request an agent queued by mistake could only be dealt with by the owner (discard on the dashboard). The agent knew
it was wrong and had no way to say so. This tool removes a *waiting* request the same way a discard does: it claims the entry
from its outbox (`Outbox.claim`, atomic, once), so a withdrawal and an Approve on the dashboard cannot both win. Whoever claims
first decides; the other one finds nothing and says so.

What it does not do:

- It never touches anything that left the queue. A request that was sent, discarded, expired or is being released answers
  `withdrawn: false` with the status `approval_status` gives, so nothing that looks like success is returned for it.
- It does not notify the owner, and it is not rate limited (a later change).
- The label (`requested_by`) is the name the queueing client gave itself in its handshake or, on a stateless server (the default,
  which keeps no handshake), the name its app registered under. It stops one assistant from taking back another's request by mistake. It is not a security boundary: a client cannot prove who it is, and the owner's own
  controls (the dashboard) do not depend on it.

A request is one entry today. The tool says nothing that ties a request to exactly one version of its text, so a later change
can keep several versions of one request, with a withdrawal as the end of the line.

The log entry is `approval.withdrawn` by the actor `assistant`: kind, outbox_id, reason and a summary with the title. Never the
text, like every other approval line.
"""
from __future__ import annotations

import email
import json
from email import policy
from typing import Annotated, Any, Literal, get_args

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import callctx
from .approval_status import _MAX_ID, ApprovalLedger

Reason = Literal["mistake", "duplicate", "superseded", "no_longer_needed"]
REASONS = get_args(Reason)
DEFAULT_REASON = "no_longer_needed"
# What the owner reads in the activity log, after "Reason:"
REASON_WORDS = {"mistake": "queued by mistake", "duplicate": "a duplicate", "superseded": "replaced by something newer",
                "no_longer_needed": "no longer needed"}

_CALENDAR_BADGES = {"create": "invitation", "update": "event update", "delete": "cancellation", "cancel": "cancellation", "reply": "reply"}


def describe(kind: str, q: Any) -> tuple[str, str]:
    """(badge, title) of a queued request the way the dashboard words its approval lines ('email', 'Re: Invoice 2291'), from
    the stored entry alone: nothing here reads a mailbox. ('', '') for a kind this does not know or an entry it cannot read."""
    try:
        if kind == "mail":
            msg = email.message_from_bytes(q.raw, policy=policy.default)
            return ("email reply" if msg["In-Reply-To"] else "email"), str(msg["Subject"] or "") or "(no subject)"
        if kind == "calendar":
            m = q.followup or {}
            return _CALENDAR_BADGES.get(m.get("op"), "calendar"), str(m.get("summary") or "") or "(no title)"
        if kind == "imessage":
            d = json.loads(q.raw.decode())
            to = d.get("to") or {}
            people = ", ".join(f"{p.get('name')} ({p.get('handle')})" if p.get("name") else str(p.get("handle", "")) for p in to.get("participants", []))
            return "imessage", f"iMessage to {to.get('name') or people or d.get('chat_id') or d.get('handle') or ''}".strip()
    except Exception:  # noqa: BLE001  -- a title is a nicety: an entry that cannot be read is still withdrawn
        pass
    return "", ""


class Withdrawals:
    def __init__(self, ledger: ApprovalLedger):
        self.ledger = ledger

    def _not_withdrawn(self, item_id: str) -> dict[str, Any]:
        """What an id that is not waiting answers: the status approval_status gives it, and withdrawn false."""
        got = self.ledger.status(item_id)
        return {"id": item_id, "withdrawn": False, "status": got["status"], **({"note": got["note"]} if "note" in got else {})}

    def withdraw(self, item_id: str, reason: str, client: str) -> dict[str, Any]:
        """Take a waiting request back. client: the name the calling client gave itself ('' when it gave none)."""
        hit = self.ledger._waiting().get(item_id)
        if hit is None:
            return self._not_withdrawn(item_id)
        kind, q = hit
        stored = getattr(q, "requested_by", None)
        if stored and stored != client:
            raise ToolError("Another client queued this request, so it is not yours to take back. Leave it: the owner decides on the "
                            "dashboard. Tell the owner if it is wrong.")
        claimed = self.ledger.outboxes[kind].claim(item_id)
        if claimed is None:                              # the owner (or the clock) got there between the look and the claim
            return self._not_withdrawn(item_id)
        badge, title = describe(kind, claimed)
        self.ledger.audit.record("approval.withdrawn", actor="assistant", via=client,
                                 summary=f"Withdrew {badge} “{title}”" if title else f"Withdrew a queued {kind} request",
                                 kind=kind, outbox_id=item_id, reason=reason)
        return {"id": item_id, "withdrawn": True, "status": "withdrawn"}


def _D(text: str) -> Any:
    return Field(description=text)


def register(tool: Any, guard: Any, ledger: ApprovalLedger) -> None:
    """Register the tool. `tool` and `guard` are the server's own decorators, so it is listed, guarded, paused and timed like the rest."""
    withdrawals = Withdrawals(ledger)

    @tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False))
    @guard
    def approval_withdraw(
        id: Annotated[str, _D("The outbox_id of the request you queued, from its queued_for_owner_approval result.")],
        reason: Annotated[Reason, _D("Why you take it back. A fixed choice, not free text; no_longer_needed is the default.")] = DEFAULT_REASON,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Take back a request you queued for the owner's approval, while it still waits.

        Use when: you queued a message, calendar change or other request by mistake, twice, or it is overtaken or no longer wanted, and approval_status still says waiting. Not for approving, rejecting or speeding up anything (only the owner can, on the dashboard), for a request that was already decided (approval_status says how it ended), or for removing mail or events that already exist.
        Parameters: id is the outbox_id from the queued result. reason is mistake (it was wrong), duplicate (queued twice), superseded (something newer replaces it) or no_longer_needed (the default); the owner reads it as a plain word in the activity log.
        Behavior: removes the waiting request at once, for every kind (mail, imessage, calendar). Nothing is sent and the owner can no longer approve it. It works only on a request that is still waiting and only once: if the owner approved or discarded it first, or it expired, nothing changes and the answer says withdrawn false. The owner gets no notification; the activity log records "withdrawn by your assistant" with the title and the reason, never the text. Only the client that queued a request can take it back: the names clients go by are compared. That guards against mix-ups between two assistants and is not a security boundary, since a client cannot prove who it is.
        Returns: {id, withdrawn, status}. withdrawn true means status withdrawn: it will not be sent. withdrawn false means it was not waiting: status is what approval_status reports (sent, discarded, expired, unclear, unknown or withdrawn), so never tell the owner it was taken back unless withdrawn is true.
        After a withdrawal, ask the owner before you queue a corrected version or the same text again. The owner can also change the words on the dashboard with "Edit text". Never queue it again to be safe.
        Errors: an id that is not an outbox_id; a request another client queued."""
        if not id.strip() or len(id) > _MAX_ID:
            raise ToolError("id is the outbox_id from a queued_for_owner_approval result.")
        return withdrawals.withdraw(id.strip(), reason, callctx.client())
