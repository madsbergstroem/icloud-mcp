"""gate.trust: an agent asks the owner for a standing rule, so requests of one action type to one person go out without waiting
(docs/SENDGATE.md, package 10, "The request for a rule").

The card is built from the fields alone: the action's title and badge (never its id), the person, the end date. The agent's reason
is shown as its own words and cannot be edited. Approving needs a recent sign-in (`step_up`), because a rule loosens protection.

What is refused, with `policy`, when queued and again when approved: an action type that is not registered, is gate.trust itself
or has not opted in (it has no standing_identity); a recipient that is not one person by email address (a domain, a wildcard,
empty); and a recipient the type's own list does not allow, because a rule never widens who may be reached, it only skips the
wait. The executor writes the rule (standing.py) and answers sent; a write that fails answers failed, so the request waits again.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from . import actions as A
from . import standing as ST

log = logging.getLogger(__name__)

ACTION_ID = "gate.trust"
MAX_REASON = 500


def opted_in(registry: A.ActionRegistry) -> dict[str, A.Registration]:
    """The registered types a rule may name: the server's own with standing_identity, never gate.trust itself. Stable order."""
    return {r.action_type.id: r for r in sorted(registry.registrations(), key=lambda r: r.action_type.id)
            if r.builtin and r.action_type.standing_identity is not None and r.action_type.id != ACTION_ID}


def schema(types: Mapping[str, A.Registration]) -> dict[str, Any]:
    which = "; ".join(f"{k}: {r.action_type.title}" for k, r in types.items())
    return {
        "type": "object", "additionalProperties": False,
        "required": ["action_type", "recipient", "reason", "action_label", "until"],
        "properties": {
            "action_type": {"type": "string", "minLength": 1, "maxLength": 100,
                            "description": f"The kind of request the rule lets through, one of: {which}."},
            "recipient": {"type": "string", "minLength": 1, "maxLength": 254,
                          "description": "One person, by email address, exactly as their requests name them. Never a domain, * or several."},
            "days": {"type": "integer", "minimum": 1, "maximum": ST.MAX_DAYS, "default": ST.DEFAULT_DAYS,
                     "description": f"How long the rule lasts, in days: {ST.DEFAULT_DAYS} unless the owner said otherwise, at most {ST.MAX_DAYS}."},
            "reason": {"type": "string", "minLength": 1, "maxLength": MAX_REASON,
                       "description": "Why, in one sentence the owner reads on the card."},
            "action_label": {"type": "string", "readOnly": True},
            "until": {"type": "string", "readOnly": True},
        },
    }


def until_text(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%-d %b %Y")


def action_type(types: Mapping[str, A.Registration]) -> A.ActionType:
    return A.ActionType(
        id=ACTION_ID, title="Allow without approval", badge="Without approval", approve_label="Allow", params_schema=schema(types),
        rows=(("Action", "action_label"), ("Person", "recipient"), ("Until", "until")),
        text_field="reason", text_caption="Your assistant's words",
        text_locked=lambda p: "Your assistant's reason stays as it was written.",
        subject=lambda p: f"{p.get('action_label') or 'Action'} · {p.get('recipient') or ''}".strip(" ·"),
        step_up=True,
        note="Requests of this kind to this person then go out without waiting for you, until the date shown, at most "
             f"{ST.DAILY_LIMIT} a day. They still have to be on your list. You can remove the rule in Settings › Without approval at any time.")


class Trust:
    """Policy, card details and executor of gate.trust for one server. registry: the server's; standing: set once the approval
    store is open (the type is registered before it)."""

    def __init__(self, registry: A.ActionRegistry):
        self.registry = registry
        self.standing: ST.Standing | None = None

    def types(self) -> dict[str, A.Registration]:
        return opted_in(self.registry)

    def _why(self, params: Mapping[str, Any]) -> str | None:
        type_id, recipient = str(params.get("action_type") or ""), str(params.get("recipient") or "")
        reg = self.types().get(type_id)
        if reg is None:
            return "This kind of request cannot go out without approval. Only the owner's own kinds of request can."
        who = ST.identity(recipient)
        if who is None:
            return "A rule names one person by email address: not *, a domain or several at once."
        try:
            allowed = reg.allows(who) if reg.allows is not None else ("*" in reg.allowed_set() or who in reg.allowed_set())
        except Exception as e:  # noqa: BLE001  -- a list that cannot be read lets nobody through
            log.warning("The list of %s raised %s", type_id, type(e).__name__)
            return "The owner's list for this action could not be read."
        if not allowed:
            return f"{who} is not on your list for {reg.action_type.title.lower()}s, and a rule never widens who may be reached. Only the owner can change the list."
        return None

    def prepare(self, params: Mapping[str, Any]) -> dict[str, Any]:
        if why := self._why(params):
            raise A.ExternalError(why, "policy", "params")
        reg = self.types()[str(params["action_type"])]
        days = int(params.get("days") or ST.DEFAULT_DAYS)
        return {"action_type": reg.action_type.id, "recipient": ST.identity(params["recipient"]), "days": days,
                "reason": str(params.get("reason") or ""), "action_label": reg.action_type.title,
                "until": until_text(time.time() + days * ST.DAY)}

    def policy(self, params: Mapping[str, Any]) -> str | None:
        return self._why(params)

    def recheck(self, params: Mapping[str, Any]) -> Any:
        if why := self._why(params):
            raise A.Rechecked("not_allowed", f"Not allowed: {why}")
        return None

    def execute(self, request: A.ActionRequest) -> A.Result:
        p = request.params
        if self.standing is None:
            return A.Result("failed", "Rules cannot be kept on this server right now. It waits again.")
        try:
            rule = self.standing.add(str(p["action_type"]), str(p["recipient"]), int(p.get("days") or ST.DEFAULT_DAYS), request.id)
        except ValueError as e:
            return A.Result("failed", f"Not allowed: {e}")
        except Exception as e:  # noqa: BLE001  -- nothing was written: the owner can try again
            log.warning("gate.trust could not write the rule (%s)", type(e).__name__)
            return A.Result("failed", "The rule could not be written. It waits again.")
        return A.Result("sent", f"Allowed until {until_text(rule.expires_at)}.")


def register(registry: A.ActionRegistry, trust: Trust) -> bool:
    """gate.trust on the external source, when at least one type may be named. True when it was registered."""
    types = trust.types()
    if not types:
        return False
    registry.register(action_type(types), trust, ["*"], trust.policy, prepare=trust.prepare, recheck=trust.recheck, builtin=True)
    return True
