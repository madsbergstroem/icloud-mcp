"""The external approval source: requests of any registered action type (actions.py) on the owner's dashboard.

It is an `ApprovalSource` like the ones for mail, calendar changes and iMessages, so the dashboard lists, shows, edits, discards,
approves and logs its entries with the code it already has, and `approval_status` and `approval_withdraw` answer for them
through the same outbox and the same log lines. What it adds:

- its own queue file (`external-outbox.json`); an entry is the canonical JSON of {action_type, params}, so its hash is taken over
  exactly those bytes, and an edit of the text changes the hash as it does for the other kinds;
- the card, the text the owner may edit and the warnings come from the action type;
- `queue_external`, the one way in: it checks the schema, the recipients against the list the owner allowed and the length of
  the queue. Nothing in the server calls it yet; a connector or an input would.
- `release`, which hands the approved request to the type's executor. See actions.py for what its answers mean: only `failed`
  puts the entry back; `unclear` (also an executor that raises) is never repeated and goes the way an unconfirmed iMessage goes,
  into the log as `approval.sent` with delivery `unconfirmed`, which `approval_status` reads as `unclear`.

The activity log gets the same lines as for the other kinds and never the parameters: the title is the type's own, and no row
of a type can carry a label that makes the dashboard copy its value into the log (actions.RESERVED_LABELS).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from . import actions as A
from . import textedit as T
from .approval_status import Released
from .dashboard import ApprovalSource
from .outbox import Outbox, OutboxFull, QueuedMessage

log = logging.getLogger(__name__)

_UNCLEAR = "Not sure it went through. Nothing will be sent again; look on the other side first."


def _shown(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _line(text: Any) -> str:
    return " ".join(str(text).split())[:300]


class ExternalApprovals(ApprovalSource):
    kind, badge, approve_label = "external", "Action", "Approve"
    text_limit = 10000

    def __init__(self, box: Outbox, registry: A.ActionRegistry):
        self.box, self.registry = box, registry

    def pending(self):
        return self.box.pending()

    # -- reading an entry --------------------------------------------------------------------------------------------------
    def _entry(self, q: QueuedMessage) -> tuple[A.Registration | None, str, dict[str, Any]]:
        type_id, params = A.read(q.raw)
        return self.registry.get(type_id), type_id, params

    def describe(self, q, context=None):
        try:
            reg, type_id, params = self._entry(q)
        except A.ExternalError as e:
            return {"title": "Unreadable request", "badge": "Action", "rows": [], "text": "", "warnings": [str(e)],
                    "notes": ["It cannot be approved. Discard it."], "needs_override": False}
        if reg is None:
            return {"title": f"Unknown action {type_id}", "badge": "Action", "rows": [], "text": "",
                    "warnings": ["This kind of action is not available on this server any more, so it cannot be approved."],
                    "notes": ["Discard it."], "needs_override": False}
        at = reg.action_type
        rows = [(label, _shown(params[name])) for label, name in at.rows if params.get(name) not in (None, "", [])]
        warnings = self._call(at.warnings, params, "The cautions for this request could not be worked out.")
        people = self._call(at.recipients, params, "")
        return {"title": at.title, "badge": at.badge, "rows": rows, "label": ", ".join(str(p) for p in people if p),
                "text": str(params.get(at.text_field) or "") if at.text_field else "",
                "notes": ["Approving carries this out. If it is not certain that it went through, it is never repeated."],
                "warnings": warnings, "needs_override": False}

    @staticmethod
    def _call(fn: Any, params: Mapping[str, Any], failed: str) -> list[str]:
        """What a type's own function says, as lines. One that raises must not break the dashboard: it says so instead."""
        try:
            return [str(x) for x in fn(params) if str(x)]
        except Exception:  # noqa: BLE001
            return [failed] if failed else []

    def approve_label_for(self, q) -> str:
        try:
            reg = self._entry(q)[0]
        except A.ExternalError:
            reg = None
        return reg.action_type.approve_label if reg is not None else "Approve"

    # -- the text the owner may edit ---------------------------------------------------------------------------------------
    def text_limit_of(self, q):
        """The schema's maxLength for the text, when the type sets one below the general limit."""
        try:
            at = self._entry(q)[0].action_type
        except (A.ExternalError, AttributeError):                 # unreadable, or a type that is not registered any more
            return self.text_limit
        cap = ((at.params_schema.get("properties") or {}).get(at.text_field) or {}).get("maxLength") if at.text_field else None
        return min(self.text_limit, cap) if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0 else self.text_limit

    def text_block(self, q):
        try:
            reg = self._entry(q)[0]
        except A.ExternalError as e:
            return str(e)
        if reg is None:
            return "This kind of action is not available on this server any more."
        return "" if reg.action_type.text_field else "This kind of request has no text to edit."

    def text_of(self, q):
        reg, _, params = self._entry(q)
        if reg is None or not reg.action_type.text_field:
            raise T.TextLocked(self.text_block(q))
        return str(params.get(reg.action_type.text_field) or "")

    def clean_text(self, text):
        return T.clean(text, allow_empty=True, limit=self.text_limit)           # the schema decides whether empty is acceptable

    def with_text(self, q, text):
        reg, type_id, params = self._entry(q)
        if reg is None or not reg.action_type.text_field:
            raise T.TextLocked(self.text_block(q))
        changed = {**params, reg.action_type.text_field: text}
        try:
            A.check_params(reg.action_type, changed)
        except A.ExternalError as e:
            raise T.TextInvalid(f"That text does not fit this request ({e}).") from e
        return A.canonical(type_id, changed), None

    # -- queueing: the one way in --------------------------------------------------------------------------------------------
    def _check(self, reg: A.Registration, params: Any) -> tuple[dict[str, Any], list[str]]:
        """The parameters as plain JSON and the people the request reaches, when both are acceptable as things stand now."""
        at = reg.action_type
        plain = A.check_params(at, params)
        try:
            people = [str(p).strip() for p in at.recipients(plain)]
        except Exception as e:  # noqa: BLE001
            raise A.ExternalError("Who this request reaches could not be worked out.") from e
        people = list(dict.fromkeys(p for p in people if p))
        allowed = reg.allowed_set()
        blocked = [] if "*" in allowed else [p for p in people if p.casefold() not in allowed]
        if blocked:
            raise A.ExternalError(f"Not permitted for {at.id}: {', '.join(blocked)}. Tell the owner; only the owner can change who may be reached.")
        return plain, people

    def queue_external(self, action_type_id: str, params: Any, requested_by: str | None, key: str | None = None) -> QueuedMessage:
        """Queue a request for the owner's approval, or return the entry already waiting for the same bytes or the same key.
        Raises ExternalError for an unknown type, parameters that do not fit, a recipient the owner did not allow or a full
        queue, and ExternalConflict for a key that was used for a different request. Nothing is carried out here."""
        reg = self.registry.get(action_type_id)
        if reg is None:
            raise A.ExternalError(f"Unknown action type {action_type_id!r}.")
        if key is not None and (not isinstance(key, str) or not key.strip() or len(key) > A.MAX_KEY_CHARS):
            raise A.ExternalError(f"key must be text of 1 to {A.MAX_KEY_CHARS} characters.")
        plain, people = self._check(reg, params)
        raw = A.canonical(reg.action_type.id, plain)
        if len(raw) > A.MAX_RAW_BYTES:
            raise A.ExternalError(f"The request is larger than {A.MAX_RAW_BYTES} bytes.")
        if key is not None and (old := next((x for x in self.box.pending() if x.key == key), None)) is not None \
                and not self._same_request(old, reg.action_type, plain):
            raise A.ExternalConflict("This key was already used for a different request. Use a new key, or ask with the same parameters.")
        try:
            return self.box.add(raw, people, None, key=key, approval_page="the dashboard's Approvals page",
                                requested_by=requested_by, action_type=reg.action_type.id)
        except OutboxFull as e:
            raise A.ExternalError(str(e)) from e

    @staticmethod
    def _same_request(old: QueuedMessage, at: A.ActionType, params: Mapping[str, Any]) -> bool:
        """Whether a waiting entry is this request. The text may differ: the owner can have edited it."""
        try:
            type_id, theirs = A.read(old.raw)
        except A.ExternalError:
            return False
        drop = lambda p: {k: v for k, v in p.items() if k != at.text_field}      # noqa: E731
        return type_id == at.id and drop(theirs) == drop(params)

    # -- the owner's decision ----------------------------------------------------------------------------------------------
    def discard(self, q):
        self.box.claim(q.id)

    def release(self, q):
        reg, type_id, params = self._entry(q)
        if reg is None:
            raise A.ExternalError(f"This kind of action ({type_id}) is not available on this server any more. Discard it.")
        plain, _ = self._check(reg, params)                      # as things stand now, like the recipient list of a mail
        claimed = self.box.claim(q.id)
        if claimed is None:
            raise A.ExternalError("It is no longer waiting.")
        if claimed.sha256 != q.sha256:                           # edited between the owner's look and the click: that is not what was approved
            self.box.restore(claimed)
            raise A.ExternalError("The request changed just now. Nothing was done; look at it again.")
        request = A.ActionRequest(claimed.id, type_id, plain, claimed.requested_by, claimed.key)
        try:
            result = reg.executor.execute(request)
        except Exception as e:  # noqa: BLE001  -- an exception proves nothing about the other side
            log.warning("The executor of %s raised %s; the outcome is not known", type_id, type(e).__name__)
            result = A.Result("unclear", _UNCLEAR)
        if not isinstance(result, A.Result):
            log.warning("The executor of %s answered with %s, not a Result; the outcome is not known", type_id, type(result).__name__)
            result = A.Result("unclear", _UNCLEAR)
        line = _line(result.line)
        if result.status == "failed":
            self.box.restore(claimed)                             # the other side never got it: safe to keep for a retry
            raise A.ExternalError(line or "Not carried out, still waiting.")
        if result.status == "unclear":
            return Released(line or _UNCLEAR, "unconfirmed")
        return Released(line or "Done.", "sent")
