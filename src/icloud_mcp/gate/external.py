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
from .. import textedit as T
from .status import Released
from .source import ApprovalSource
from . import store as S
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

    def kind_label(self, q: QueuedMessage) -> str:
        """What the dashboard calls this request where it names kinds: the badge of its action type ('Beeper'), else 'Other'."""
        try:
            reg = self._entry(q)[0]
        except A.ExternalError:
            return "Other"
        return reg.action_type.badge if reg is not None else "Other"

    def kind_labels(self) -> list[str]:
        """The badges of every type this server takes, so a kind with nothing waiting still has its line."""
        return list(dict.fromkeys(t.badge for t in self.registry.types()))

    def describe(self, q, context=None):
        try:
            reg, type_id, params = self._entry(q)
        except A.ExternalError as e:
            return {"title": "Unreadable request", "badge": "Other", "rows": [], "text": "", "warnings": [],
                    "notes": [f"{e} It cannot be approved. Discard it."], "needs_override": False, "approvable": False}
        if reg is None:
            return {"title": f"Unknown action {type_id}", "badge": "Other", "rows": [], "text": "", "warnings": [],
                    "notes": ["This kind of action is not available on this server any more, so it cannot be approved. Discard it."],
                    "needs_override": False, "approvable": False}
        at = reg.action_type
        rows = [(label, _shown(params[name])) for label, name in at.rows if params.get(name) not in (None, "", [])]
        warnings = self._call(at.warnings, params, "The cautions for this request could not be worked out.")
        people = self._call(at.recipients, params, "")
        hold = reg.hold_now()
        notes = ["Approving carries this out. If it is not certain that it went through, it is never repeated."]
        return {"title": at.title, "badge": at.badge, "rows": rows, "label": ", ".join(str(p) for p in people if p),
                "text": str(params.get(at.text_field) or "") if at.text_field else "",
                "notes": ([hold.text] if hold and hold.level == "note" else []) + notes,
                "warnings": warnings, "needs_override": False,
                **({"held": hold.text} if hold and hold.level == "blocked" else {})}

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
        return A.canonical(type_id, changed, A.owner_allowed(q.raw)), None

    # -- every field the owner may change (package 2d) ---------------------------------------------------------------------
    def fields_of(self, q) -> list[dict[str, Any]]:
        """The fields the owner may change before approving, as the edit form shows them: the rows first, then the text.
        [] when there is nothing beyond the text (the text alone keeps its own editor)."""
        try:
            reg, _, params = self._entry(q)
        except A.ExternalError:
            return []
        if reg is None or not reg.action_type.editable_fields:
            return []
        at = reg.action_type
        props = at.params_schema.get("properties") or {}
        labels = {name: label for label, name in at.rows}
        own = {x.casefold() for x in A.owner_allowed(q.raw)}
        people = self._people(at, params)
        order = [n for _, n in at.rows if n in A.editable(at)] + ([at.text_field] if at.text_field else [])
        out = []
        for name in dict.fromkeys(order):
            spec = props.get(name) or {}
            value = params.get(name)
            out.append({"name": name, "label": labels.get(name, "Text"), "type": spec.get("type", "string"),
                        "value": "" if value is None else value, "text": name == at.text_field,
                        "limit": self.text_limit_of(q) if name == at.text_field else spec.get("maxLength"),
                        "required": name in (at.params_schema.get("required") or []),
                        "recipient": name in at.recipient_fields,
                        "owner_allowed": str(value).strip().casefold() in own and str(value).strip() in people})
        return out

    @staticmethod
    def _people(at: A.ActionType, params: Mapping[str, Any]) -> list[str]:
        try:
            return [str(p).strip() for p in at.recipients(params) if str(p).strip()]
        except Exception:  # noqa: BLE001
            return []

    def with_fields(self, q, changes: Any) -> tuple[bytes, str, list[str], list[str], list[str]]:
        """The stored request with the owner's changes: (raw, key fingerprint, people, labels of the fields that changed, people
        the owner allowed by hand). A recipient the owner types in who is not on the list is allowed for this request only;
        the owner's other rules (the policy) still apply, and the service is asked about the target again.
        Raises TextLocked when nothing here can be changed, TextInvalid for a change that does not fit."""
        reg, type_id, params = self._entry(q)
        if reg is None or not reg.action_type.editable_fields:
            raise T.TextLocked(self.text_block(q) or "This kind of request has no fields to change.")
        at = reg.action_type
        if not isinstance(changes, Mapping) or not changes:
            raise T.TextInvalid("Nothing to change was sent.")
        allowed = set(A.editable(at))
        if bad := sorted(set(changes) - allowed):
            raise T.TextInvalid(f"{bad[0]} cannot be changed here.")
        labels = {name: label for label, name in at.rows}
        new = dict(params)
        for name, value in changes.items():
            if isinstance(value, str):
                value = T.clean(value, allow_empty=True, limit=self.text_limit, strip=name != at.text_field)
            if value == "" and name not in (at.params_schema.get("required") or []):
                new.pop(name, None)
            else:
                new[name] = value
        order = [n for _, n in at.rows if n in allowed] + ([at.text_field] if at.text_field else [])     # as the form lists them
        changed = [labels.get(n, "Text") for n in dict.fromkeys(order) if new.get(n) != params.get(n)]
        if not changed:
            return q.raw, self.key_fingerprint(at, params), self._people(at, params), [], A.owner_allowed(q.raw)
        try:
            A.check_params(at, new)
        except A.ExternalError as e:
            raise T.TextInvalid(f"That does not fit this request ({e}).") from e
        before = self._people(at, params)
        after = self._people(at, new)
        if after != before and reg.prepare is not None:          # a new target: the service tells what it is, as for the agent
            agent = {k: v for k, v in new.items() if k not in A.read_only_fields(at)}
            try:
                new = dict(reg.prepare(agent))
            except A.ExternalError as e:
                raise T.TextInvalid(str(e).replace(" Nothing was queued.", "")) from None
            except Exception:  # noqa: BLE001
                raise T.TextInvalid("The service could not be asked about the new target.") from None
        try:
            listed = reg.allowed_set()
        except Exception:  # noqa: BLE001
            raise T.TextInvalid("Your list for this action could not be read.") from None
        kept = {x.casefold() for x in A.owner_allowed(q.raw)}
        introduced = {p.casefold() for p in after} - {p.casefold() for p in before}
        own = [p for p in after if p.casefold() not in listed and "*" not in listed and (p.casefold() in introduced or p.casefold() in kept)]
        try:
            plain, people = self._check(reg, new, own)
        except A.ExternalError as e:
            raise T.TextInvalid(str(e)) from None
        raw = A.canonical(type_id, plain, own)
        if len(raw) > A.MAX_RAW_BYTES:
            raise T.TextInvalid(f"The request would be larger than {A.MAX_RAW_BYTES} bytes.")
        return raw, self.key_fingerprint(at, plain), people, changed, own

    # -- queueing: the one way in --------------------------------------------------------------------------------------------
    def _check(self, reg: A.Registration, params: Any, owner_allowed: Any = ()) -> tuple[dict[str, Any], list[str]]:
        """The parameters as plain JSON and the people the request reaches, when both are acceptable as things stand now.
        owner_allowed: recipients the owner typed in by hand for this version; the list does not apply to them, the rules do."""
        at = reg.action_type
        plain = A.check_params(at, params)
        try:
            people = [str(p).strip() for p in at.recipients(plain)]
        except Exception as e:  # noqa: BLE001
            raise A.ExternalError("Who this request reaches could not be worked out.", "schema", "params") from e
        people = list(dict.fromkeys(p for p in people if p))
        own = {str(x).strip().casefold() for x in owner_allowed}
        self._check_people(reg, [p for p in people if p.casefold() not in own])
        try:
            why = reg.policy(plain) if reg.policy is not None else None
        except Exception as e:  # noqa: BLE001  -- a rule that cannot be read permits nothing, and its error text stays here
            log.warning("The policy of %s raised %s", at.id, type(e).__name__)
            raise A.ExternalError("The owner's rules for this action could not be read, so nothing was queued.", "policy") from None
        if why:
            raise A.ExternalError(why, "policy")
        return plain, people

    @staticmethod
    def _check_people(reg: A.Registration, people: list[str]) -> None:
        try:
            allowed = reg.allowed_set()
        except Exception as e:  # noqa: BLE001  -- a list that cannot be read lets nobody through, and its entries stay here
            log.warning("The list of %s raised %s", reg.action_type.id, type(e).__name__)
            raise A.ExternalError("The owner's list for this action could not be read, so nothing was queued.", "policy") from None
        blocked = [] if "*" in allowed else [p for p in people if p.casefold() not in allowed]
        if blocked:
            raise A.ExternalError(f"Not permitted for {reg.action_type.id}: {', '.join(blocked)}. Tell the owner; only the owner can change "
                                  "who may be reached.", "not_allowed")

    def precheck(self, reg: A.Registration, params: Any) -> None:
        """What can be checked on an agent's own parameters before prepare asks the service anything: the schema (the readOnly
        fields not required yet), the size and the owner's list. A target the owner did not allow never reaches the service.
        Rules that need what prepare fills in run after it, in queue_external."""
        at = reg.action_type
        plain = A.check_params(at, params, A.request_schema(at))
        if len(A.canonical(at.id, plain)) > A.MAX_RAW_BYTES:
            raise A.ExternalError(f"The request is larger than {A.MAX_RAW_BYTES} bytes.", "schema", "params")
        try:
            people = [str(p).strip() for p in at.recipients(plain)]
        except Exception:  # noqa: BLE001  -- who it reaches is known only after prepare: checked there
            return
        self._check_people(reg, list(dict.fromkeys(p for p in people if p)))

    def queue_external(self, action_type_id: str, params: Any, requested_by: str | None, key: str | None = None) -> QueuedMessage:
        """Queue a request for the owner's approval, or return the entry already waiting for the same bytes or the same key.
        Raises ExternalError for an unknown type, parameters that do not fit, a recipient the owner did not allow or a full
        queue, and ExternalConflict for a key that was used for a different request. Nothing is carried out here."""
        reg = self.registry.get(action_type_id)
        if reg is None:
            raise A.ExternalError(f"Unknown action type {action_type_id!r}.", "unknown_action", "action")
        if key is not None and (not isinstance(key, str) or not key.strip() or len(key) > A.MAX_KEY_CHARS):
            raise A.ExternalError(f"key must be text of 1 to {A.MAX_KEY_CHARS} characters.", "schema", "request_id")
        plain, people = self._check(reg, params)
        raw = A.canonical(reg.action_type.id, plain)
        if len(raw) > A.MAX_RAW_BYTES:
            raise A.ExternalError(f"The request is larger than {A.MAX_RAW_BYTES} bytes.", "schema", "params")
        if key is not None and (old := next((x for x in self.box.pending() if x.key == key), None)) is not None \
                and not self._same_request(old, reg.action_type, plain):
            raise A.ExternalConflict("This key was already used for a different request. Use a new key, or ask with the same parameters.")
        extra = {"key_fingerprint": self.key_fingerprint(reg.action_type, plain)} if getattr(self.box, "keeps_keys", False) else {}
        try:
            return self.box.add(raw, people, None, key=key, approval_page="the dashboard's Approvals page",
                                requested_by=requested_by, action_type=reg.action_type.id, **extra)
        except OutboxFull as e:
            raise A.ExternalError(str(e), "queue_full") from e
        except S.AlreadyDecided as e:
            raise A.AlreadyDecided(e.item_id, e.state) from None
        except S.KeyConflict:
            raise A.ExternalConflict("This key was already used for a different request. Use a new key, or ask with the same parameters.") from None
        except S.StoreUnavailable as e:
            raise A.ExternalError(str(e), "store_unavailable") from None

    @staticmethod
    def key_fingerprint(at: A.ActionType, params: Mapping[str, Any]) -> str:
        """What a key stands for: the parameters without the text (the owner may edit it) and without what the service fills in."""
        skip = set(A.editable(at)) | A.read_only_fields(at)
        return S.fingerprint({k: v for k, v in params.items() if k not in skip})

    @staticmethod
    def _same_request(old: QueuedMessage, at: A.ActionType, params: Mapping[str, Any]) -> bool:
        """Whether a waiting entry is this request. The text may differ: the owner can have edited it."""
        try:
            type_id, theirs = A.read(old.raw)
        except A.ExternalError:
            return False
        skip = set(A.editable(at)) | A.read_only_fields(at)      # the owner may edit these; the service fills in readOnly fields
        drop = lambda p: {k: v for k, v in p.items() if k not in skip}            # noqa: E731
        return type_id == at.id and drop(theirs) == drop(params)

    # -- the owner's decision ----------------------------------------------------------------------------------------------
    def discard(self, q):
        self.box.claim(q.id, "discarded")

    def release(self, q):
        reg, type_id, params = self._entry(q)
        if reg is None:
            raise A.ExternalError(f"This kind of action ({type_id}) is not available on this server any more. Discard it.")
        own = A.owner_allowed(q.raw)
        plain, _ = self._check(reg, params, own)                 # as things stand now, like the recipient list of a mail
        hold = reg.hold_now(approving=True)
        if hold and hold.level == "blocked":                      # nothing claimed: the request keeps waiting
            raise A.ExternalError(f"Not carried out: {hold.text}")
        claimed = self.box.claim(q.id, "executing")
        if claimed is None:
            raise A.ExternalError("It is no longer waiting.")
        if claimed.sha256 != q.sha256:                           # edited between the owner's look and the click: that is not what was approved
            self.box.restore(claimed)
            raise A.ExternalError("The request changed just now. Nothing was done; look at it again.")
        if reg.recheck is not None:                              # is the target still the one the owner looked at?
            try:
                reason = reg.recheck(plain)
            except Exception as e:  # noqa: BLE001  -- nothing has been carried out: the owner can try again
                self.box.restore(claimed)
                log.warning("The recheck of %s raised %s; the request waits again", type_id, type(e).__name__)
                raise A.ExternalError("Not carried out: the service could not be asked about the target. Try again in a moment.") from None
            if isinstance(reason, Mapping) and hasattr(self.box, "revise"):   # the service's new values: a new version waits
                try:
                    fresh, _ = self._check(reg, dict(reason), own)
                except A.ExternalError:                  # new values the type does not take: nothing to show, it ends as before
                    fresh = None
                raw = A.canonical(type_id, fresh, own) if fresh is not None else b""
                if fresh is not None and len(raw) <= A.MAX_RAW_BYTES:
                    revised = self._record(self.box.revise, claimed, raw, self.key_fingerprint(reg.action_type, fresh))
                    if revised is None:
                        raise A.ExternalError("Not sent: the target changed since you looked, and the request is no longer waiting.")
                    n = len(self.box.versions(claimed.id))
                    raise A.Revised(n, claimed.id, revised.sha256)
            if reason or isinstance(reason, Mapping):           # claimed and not put back: it ends here, nothing was carried out
                code = reason if isinstance(reason, str) and reason in A.RECHECK_CODES else "target_changed"   # a code, never the plugin's own text
                self._record(self.box.finish, claimed.id, "discarded", code)
                raise A.Rechecked(code)
        request = A.ActionRequest(claimed.id, type_id, plain, claimed.requested_by, claimed.key, max(1, getattr(claimed, "attempts", 0) or 1))
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
            self._record(self.box.restore, claimed)               # the other side never got it: safe to keep for a retry
            raise A.ExternalError(line or "Not carried out, still waiting.")
        if result.status == "unclear":
            self._record(self.box.finish, claimed.id, "unclear")
            return Released(line or _UNCLEAR, "unconfirmed")
        self._record(self.box.finish, claimed.id, "sent")
        return Released(line or "Done.", "sent")

    @staticmethod
    def _record(step: Any, *args: Any) -> Any:
        """Write how a request ended after the executor ran. The executor's answer stands even when this write fails: the owner is
        told what happened, and the row, still `executing`, ends unclear at the next start and is never repeated."""
        try:
            return step(*args)
        except Exception as e:  # noqa: BLE001
            log.warning("could not record how a request ended (%s); it stays executing until the next start", type(e).__name__)
