"""Kinds of action the owner can be asked to approve, and who carries them out.

Mail, calendar changes and iMessages each have their own approval source (dashboard.py). This module is what a further kind
needs instead: an `ActionType` says how a request looks on the card, which parameters it takes (a JSON Schema), which of them is
the text the owner may edit and who it reaches; an `Executor` does the work once the owner approves. The registry holds both. The
external source (external.py) turns a registered type into a source for the dashboard, so a new type needs no new screen and
no new status code.

Nothing here talks to anything outside this process. Nothing registers a type yet: with an empty registry the server does not
build the external source at all and behaves as it did before.

Parameters are checked with `jsonschema` (the library `mcp` already depends on), not with a validator of our own: the schema
is the contract an agent sees, and the library is what implements it. A type's own functions (`recipients`, `warnings`) read
checked parameters only.

What an executor answers with decides what happens to the entry:

- `sent`: it was carried out.
- `failed`: the other side provably never received the request. Only then does the entry go back into the queue, so the owner
  can try again.
- `unclear`: it may or may not have gone through (a timeout after the request left, an answer that cannot be read). Never
  repeated by this server; the owner is told to look. An executor that raises counts as `unclear` too: an exception proves
  nothing about the other side.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Literal, Mapping, Protocol, Sequence

ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
# The dashboard picks the people of a request, for its list and for the activity log, from rows with these labels. A type's own
# rows must not use them, so nothing a type shows can end up in the log by its label.
RESERVED_LABELS = ("To", "Guests", "Will be delivered to", "Conversation")
MAX_RAW_BYTES = 200_000              # the stored request, as canonical JSON
MAX_KEY_CHARS = 200

Status = Literal["sent", "failed", "unclear"]
STATUSES: tuple[str, ...] = ("sent", "failed", "unclear")


# Why a request is refused, as an agent reads it in approval_request's answer (docs/SENDGATE.md, "Refusal codes"). Each code has
# one line that says what to do instead. `retryable` codes may pass when asked again unchanged; the others never do.
REFUSALS: dict[str, str] = {
    "unknown_action": "Call approval_list_actions and use one of the actions it lists.",
    "channel_tool": "Use the channel's own tool named in the message.",
    "schema": "Fix the parameter named in path; approval_list_actions shows the schema.",
    "read_only": "Leave out the field named in path: the server fills it in from the service.",
    "not_allowed": "Tell the owner; only the owner can change who may be reached.",
    "policy": "Tell the owner; only the owner can change this rule.",
    "describe_failed": "The service could not be asked about the target. Nothing was queued; try again in a minute.",
    "key_conflict": "Use a new request_id, or ask again with the same parameters.",
    "queue_full": "Do not retry. Tell the owner to review the waiting requests on the dashboard.",
    "store_unavailable": "The approval store could not take it. Nothing was queued; try again in a minute.",
    "not_replaceable": "replaces names a request of yours that still waits, for the same action; otherwise ask without replaces.",
}
RETRYABLE = ("describe_failed", "store_unavailable")
RECHECK_CODES = ("target_changed", "not_allowed")     # what a recheck may answer; anything else is logged as target_changed


class ExternalError(Exception):
    """A request that cannot be queued or carried out. The message is written for the agent or the owner who reads it.
    code: one of REFUSALS, for the agent's answer; path: the parameter it is about, when there is one."""

    def __init__(self, message: str, code: str = "policy", path: str | None = None):
        super().__init__(message)
        self.code = code if code in REFUSALS else "policy"
        self.path = path


class ExternalConflict(ExternalError):
    """The idempotency key was already used for a different request."""

    def __init__(self, message: str, code: str = "key_conflict", path: str | None = "request_id"):
        super().__init__(message, code, path)


class AlreadyDecided(ExternalError):
    """The request_id names a request that has already ended. Nothing is queued; the answer is that request's outcome."""

    def __init__(self, item_id: str, state: str):
        super().__init__(f"This request_id belongs to a request that already ended ({state}). Nothing was queued.", "key_conflict", "request_id")
        self.item_id, self.state = item_id, state


class Revised(ExternalError):
    """Checked again just before the executor was called, the service had other values than the owner looked at. Nothing was
    carried out; the request waits again as version `n` with the service's values (package 2c), for the owner to look at."""

    def __init__(self, n: int, item_id: str, sha256: str = ""):
        super().__init__("Not sent: the target changed since you looked. It waits again with the new details; check it before approving.",
                         "policy")
        self.n, self.item_id, self.sha256 = n, item_id, sha256


class Rechecked(ExternalError):
    """Checked again just before the executor was called, the target was not what the owner looked at. Nothing was carried out
    and the request ends as discarded with `reason` (a code, never a parameter): it is not put back, because approving it again
    would fail the same way."""

    def __init__(self, reason: str = "target_changed", message: str | None = None):
        super().__init__(message or "Not sent: the target changed since you looked. Your assistant has to ask again.", "policy")
        self.reason = reason


def _jsonschema() -> Any:
    import jsonschema          # only when a type is registered, so a server without one does not load it
    return jsonschema


def _nothing(_: Mapping[str, Any]) -> Sequence[str]:
    return ()


@dataclass(frozen=True)
class ActionType:
    """One kind of action. Checked when it is created, so a mistake shows at start-up and not on the owner's screen."""
    id: str                                    # 'test.echo': lower-case words joined by dots
    title: str                                 # the card's title; the same for every request of this type, never taken from a parameter
    badge: str                                 # the small label ('Echo')
    approve_label: str                         # the main button ('Send')
    params_schema: Mapping[str, Any]           # JSON Schema (draft 2020-12) of the parameters: an object
    rows: tuple[tuple[str, str], ...] = ()     # (label, parameter name) lines of the card
    text_field: str | None = None              # the parameter shown as the request's text, which the owner may edit, or None
    # further parameters the owner may change before approving (package 2d): shown as rows of the card, set by the agent
    editable_fields: tuple[str, ...] = ()
    recipient_fields: tuple[str, ...] = ()     # the parameters that say who it reaches, so the edit form can say the list applies
    recipients: Callable[[Mapping[str, Any]], Sequence[str]] = _nothing   # who the request reaches, for the list that is checked and shown
    warnings: Callable[[Mapping[str, Any]], Sequence[str]] = _nothing     # lines of caution for the owner, from checked parameters

    def __post_init__(self) -> None:
        if not ID_PATTERN.match(self.id or ""):
            raise ValueError(f"Action type id {self.id!r} must be lower-case words joined by dots, such as 'test.echo'.")
        for field in ("title", "badge", "approve_label"):
            if not str(getattr(self, field)).strip():
                raise ValueError(f"Action type {self.id}: {field} is empty.")
        schema = self.params_schema
        try:
            _jsonschema().Draft202012Validator.check_schema(schema)
        except Exception as e:  # noqa: BLE001  -- a SchemaError, or something that is no schema at all
            raise ValueError(f"Action type {self.id}: params_schema is not a valid JSON Schema ({type(e).__name__}).") from e
        if schema.get("type") != "object":
            raise ValueError(f"Action type {self.id}: params_schema must describe an object.")
        props = schema.get("properties") or {}
        for label, name in self.rows:
            if label in RESERVED_LABELS:
                raise ValueError(f"Action type {self.id}: the row label {label!r} is reserved for who a request reaches.")
            if name not in props:
                raise ValueError(f"Action type {self.id}: row {label!r} shows {name!r}, which is not in the schema.")
        if self.text_field is not None and (props.get(self.text_field) or {}).get("type") != "string":
            raise ValueError(f"Action type {self.id}: text_field {self.text_field!r} must be a string property of the schema.")
        shown = {name for _, name in self.rows}
        for name in self.editable_fields:
            spec = props.get(name)
            if not isinstance(spec, Mapping) or spec.get("readOnly") is True:
                raise ValueError(f"Action type {self.id}: editable field {name!r} must be a property of the schema the agent sets.")
            if spec.get("type") not in EDITABLE_TYPES:
                raise ValueError(f"Action type {self.id}: editable field {name!r} must be of type string, number, integer or boolean.")
            if name != self.text_field and name not in shown:
                raise ValueError(f"Action type {self.id}: editable field {name!r} must be shown in a row, so every change is on the card.")
        if _nested_read_only(schema):
            raise ValueError(f"Action type {self.id}: readOnly is allowed only on top-level properties, where it is enforced.")


EDITABLE_TYPES = ("string", "number", "integer", "boolean")


def editable(action_type: ActionType) -> tuple[str, ...]:
    """Every parameter the owner may change before approving: the text first, then the editable rows, each once."""
    return tuple(dict.fromkeys(([action_type.text_field] if action_type.text_field else []) + list(action_type.editable_fields)))


def _nested_read_only(node: Any, top: bool = True) -> bool:
    """Whether readOnly appears anywhere but on a top-level property: there jsonschema would not refuse it and neither would we."""
    if isinstance(node, Mapping):
        for k, v in node.items():
            if k == "readOnly" and not top:
                return True
            if top and k == "properties" and isinstance(v, Mapping):
                if any(_nested_read_only({kk: vv for kk, vv in p.items() if kk != "readOnly"}, False) for p in v.values() if isinstance(p, Mapping)):
                    return True
                continue
            if isinstance(v, (Mapping, list)) and _nested_read_only(v, False):
                return True
        return False
    if isinstance(node, list):
        return any(_nested_read_only(x, False) for x in node)
    return False


@dataclass(frozen=True)
class Result:
    """What an executor reports: how it ended and one line for the owner (no text of the request)."""
    status: Status
    line: str = ""

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"Result status must be one of {', '.join(STATUSES)}.")


@dataclass(frozen=True)
class ActionRequest:
    """What an executor receives: the request as the owner approved it."""
    id: str
    action_type: str
    params: Mapping[str, Any]
    requested_by: str | None = None
    key: str | None = None
    attempt: int = 1                           # which execution this is: 1, then one more after each `failed` (the store counts)


class Executor(Protocol):
    def execute(self, request: ActionRequest) -> Result:
        """Carry the request out and say how it ended. Called from a worker thread, once per approval."""


@dataclass(frozen=True)
class Hold:
    """What a type says about approving right now, apart from any one request. level "blocked": approving is off and `text` says
    why (nothing is carried out; the request keeps waiting); level "note": a line the card shows, approving stays possible. `code`
    is what an agent's answer names (approval_request's warnings)."""
    level: str
    code: str
    text: str


@dataclass(frozen=True)
class Registration:
    action_type: ActionType
    executor: Executor
    allowed: Callable[[], Iterable[str]]       # who a request of this type may reach, as the owner has it now; empty means nobody
    # rules beyond the list, read on the parameters as the owner has them now: the reason a request is not permitted, or None
    policy: Callable[[Mapping[str, Any]], str | None] | None = None
    # the agent's parameters -> the parameters that are stored: asks the service what the target is, so the card shows the
    # service's words. Fields it adds are readOnly in the schema; an agent that sets one is refused. Raises ExternalError to refuse.
    prepare: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None
    # called after the owner approved and before the executor, on the parameters the owner looked at: None when the target is
    # unchanged; the parameters with the service's new values (a mapping) when it changed, so the request waits again as a new
    # version (package 2c); or a reason code ('target_changed') when no new version can be made: the request then ends as
    # discarded. Nothing is carried out in either case.
    recheck: Callable[[Mapping[str, Any]], str | None] | None = None
    channel_tool: str | None = None            # the tool that owns this type (messages_send_request); approval_request refuses it
    # whether approving is off right now (blocked) or needs a note, read every time; called with approving=True just before a
    # release, so a type may check again then (the gateway connector probes when its last look is old)
    hold: Callable[[bool], Hold | None] | None = None

    def hold_now(self, approving: bool = False) -> Hold | None:
        """The type's hold as it is now. One that raises blocks: approving must not depend on a function that failed."""
        if self.hold is None:
            return None
        try:
            got = self.hold(approving)
        except Exception:  # noqa: BLE001
            return Hold("blocked", "hold_failed", "Whether this may be approved could not be worked out. Try again in a moment.")
        return got if isinstance(got, Hold) and got.level in ("blocked", "note") else None

    def allowed_set(self) -> set[str]:
        return {str(x).strip().casefold() for x in self.allowed()}


class ActionRegistry:
    def __init__(self) -> None:
        self._items: dict[str, Registration] = {}

    def register(self, action_type: ActionType, executor: Executor,
                 allowed_recipients: Callable[[], Iterable[str]] | Iterable[str] = (),
                 policy: Callable[[Mapping[str, Any]], str | None] | None = None, *,
                 prepare: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
                 recheck: Callable[[Mapping[str, Any]], str | None] | None = None,
                 channel_tool: str | None = None, hold: Callable[[bool], "Hold | None"] | None = None) -> None:
        """allowed_recipients: the people (or places) a request of this type may reach, as a collection or as a function that
        reads the owner's list every time it is asked. A type whose requests name recipients and has none listed cannot queue
        anything: the list is the owner's, and an empty one allows nobody. An entry `*` allows every recipient. policy: further
        rules of the owner's (for Beeper: which networks, whether groups), checked like the list when a request is queued and
        again when it is approved; it answers why a request is not permitted, or None."""
        if action_type.id in self._items:
            raise ValueError(f"Action type {action_type.id} is already registered.")
        if not callable(getattr(executor, "execute", None)):
            raise TypeError("An executor has an execute(request) method.")
        if read_only_fields(action_type) and prepare is None:
            raise ValueError(f"Action type {action_type.id} has readOnly fields but no prepare to fill them in.")
        allowed = allowed_recipients if callable(allowed_recipients) else (lambda fixed=tuple(allowed_recipients): fixed)
        self._items[action_type.id] = Registration(action_type, executor, allowed, policy, prepare, recheck, channel_tool, hold)

    def copy(self) -> "ActionRegistry":
        """Another registry with the same registrations, so a server adds the types its own settings turn on without touching the
        shared one (a process may build several servers)."""
        other = ActionRegistry()
        other._items = dict(self._items)
        return other

    def get(self, action_type_id: str) -> Registration | None:
        return self._items.get(action_type_id)

    def types(self) -> list[ActionType]:
        return [r.action_type for r in self._items.values()]

    def registrations(self) -> list[Registration]:
        return list(self._items.values())


REGISTRY = ActionRegistry()          # what the server builds the external source from; nothing registers into it yet


def register(action_type: ActionType, executor: Executor, allowed_recipients: Callable[[], Iterable[str]] | Iterable[str] = ()) -> None:
    REGISTRY.register(action_type, executor, allowed_recipients)


# -- the stored form -------------------------------------------------------------------------------------------------------------
def canonical(action_type_id: str, params: Mapping[str, Any], owner_allowed: Iterable[str] = ()) -> bytes:
    """The bytes an entry stores, and the hash is taken over: {action_type, params} as JSON with sorted keys and no spaces.
    owner_allowed: recipients the owner typed in by hand who are not on the list (package 2d); they belong to this version,
    so its hash covers them, and are left out when there are none, so older entries keep their bytes."""
    doc: dict[str, Any] = {"action_type": action_type_id, "params": params}
    if extra := sorted({str(x) for x in owner_allowed if str(x)}):
        doc["owner_allowed"] = extra
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def owner_allowed(raw: bytes) -> list[str]:
    """The recipients the owner allowed by hand for this version, or [] (also for bytes that cannot be read)."""
    try:
        got = json.loads(raw.decode()).get("owner_allowed")
    except (ValueError, UnicodeDecodeError, AttributeError):
        return []
    return [str(x) for x in got] if isinstance(got, list) else []


def read(raw: bytes) -> tuple[str, dict[str, Any]]:
    """(action type id, params) of a stored entry. ExternalError for bytes that are not one."""
    try:
        d = json.loads(raw.decode())
        if isinstance(d, dict) and isinstance(d.get("action_type"), str) and isinstance(d.get("params"), dict):
            return d["action_type"], d["params"]
    except (ValueError, UnicodeDecodeError):
        pass
    raise ExternalError("This request could not be read.")


def read_only_fields(action_type: ActionType) -> set[str]:
    """The parameters the server fills in itself (readOnly in the schema). jsonschema does not refuse them on its own."""
    props = action_type.params_schema.get("properties") or {}
    return {name for name, spec in props.items() if isinstance(spec, Mapping) and spec.get("readOnly") is True}


def request_schema(action_type: ActionType) -> dict[str, Any]:
    """The schema an agent's own parameters must fit before prepare runs: the type's, with the readOnly fields no longer required
    (the server fills them in afterwards)."""
    schema = dict(action_type.params_schema)
    ro = read_only_fields(action_type)
    if ro and isinstance(schema.get("required"), list):
        schema["required"] = [r for r in schema["required"] if r not in ro]
    return schema


def check_params(action_type: ActionType, params: Any, schema: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The parameters as plain JSON, when they fit the type's schema (or `schema`). ExternalError (one line) when they do not."""
    if not isinstance(params, Mapping):
        raise ExternalError("params must be an object.", "schema", "params")
    try:
        plain = json.loads(json.dumps(params, allow_nan=False))
    except (TypeError, ValueError) as e:
        raise ExternalError("params must be plain JSON: text, numbers, true or false, lists and objects.", "schema", "params") from e
    js = _jsonschema()
    err = js.exceptions.best_match(js.Draft202012Validator(schema or action_type.params_schema).iter_errors(plain))
    if err is not None:
        where = "/".join(str(p) for p in err.absolute_path) or "params"
        raise ExternalError(f"{where}: {err.message}"[:300], "schema", where)
    return plain
