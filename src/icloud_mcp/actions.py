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


class ExternalError(Exception):
    """A request that cannot be queued or carried out. The message is written for the agent or the owner who reads it."""


class ExternalConflict(ExternalError):
    """The idempotency key was already used for a different request."""


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
    text_field: str | None = None              # the parameter the owner may edit before approving, or None
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


class Executor(Protocol):
    def execute(self, request: ActionRequest) -> Result:
        """Carry the request out and say how it ended. Called from a worker thread, once per approval."""


@dataclass(frozen=True)
class Registration:
    action_type: ActionType
    executor: Executor
    allowed: Callable[[], Iterable[str]]       # who a request of this type may reach, as the owner has it now; empty means nobody
    # rules beyond the list, read on the parameters as the owner has them now: the reason a request is not permitted, or None
    policy: Callable[[Mapping[str, Any]], str | None] | None = None

    def allowed_set(self) -> set[str]:
        return {str(x).strip().casefold() for x in self.allowed()}


class ActionRegistry:
    def __init__(self) -> None:
        self._items: dict[str, Registration] = {}

    def register(self, action_type: ActionType, executor: Executor,
                 allowed_recipients: Callable[[], Iterable[str]] | Iterable[str] = (),
                 policy: Callable[[Mapping[str, Any]], str | None] | None = None) -> None:
        """allowed_recipients: the people (or places) a request of this type may reach, as a collection or as a function that
        reads the owner's list every time it is asked. A type whose requests name recipients and has none listed cannot queue
        anything: the list is the owner's, and an empty one allows nobody. An entry `*` allows every recipient. policy: further
        rules of the owner's (for Beeper: which networks, whether groups), checked like the list when a request is queued and
        again when it is approved; it answers why a request is not permitted, or None."""
        if action_type.id in self._items:
            raise ValueError(f"Action type {action_type.id} is already registered.")
        if not callable(getattr(executor, "execute", None)):
            raise TypeError("An executor has an execute(request) method.")
        allowed = allowed_recipients if callable(allowed_recipients) else (lambda fixed=tuple(allowed_recipients): fixed)
        self._items[action_type.id] = Registration(action_type, executor, allowed, policy)

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


REGISTRY = ActionRegistry()          # what the server builds the external source from; nothing registers into it yet


def register(action_type: ActionType, executor: Executor, allowed_recipients: Callable[[], Iterable[str]] | Iterable[str] = ()) -> None:
    REGISTRY.register(action_type, executor, allowed_recipients)


# -- the stored form -------------------------------------------------------------------------------------------------------------
def canonical(action_type_id: str, params: Mapping[str, Any]) -> bytes:
    """The bytes an entry stores, and the hash is taken over: {action_type, params} as JSON with sorted keys and no spaces."""
    return json.dumps({"action_type": action_type_id, "params": params}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def read(raw: bytes) -> tuple[str, dict[str, Any]]:
    """(action type id, params) of a stored entry. ExternalError for bytes that are not one."""
    try:
        d = json.loads(raw.decode())
        if isinstance(d, dict) and isinstance(d.get("action_type"), str) and isinstance(d.get("params"), dict):
            return d["action_type"], d["params"]
    except (ValueError, UnicodeDecodeError):
        pass
    raise ExternalError("This request could not be read.")


def check_params(action_type: ActionType, params: Any) -> dict[str, Any]:
    """The parameters as plain JSON, when they fit the type's schema. ExternalError (one line) when they do not."""
    if not isinstance(params, Mapping):
        raise ExternalError("params must be an object.")
    try:
        plain = json.loads(json.dumps(params, allow_nan=False))
    except (TypeError, ValueError) as e:
        raise ExternalError("params must be plain JSON: text, numbers, true or false, lists and objects.") from e
    js = _jsonschema()
    err = js.exceptions.best_match(js.Draft202012Validator(action_type.params_schema).iter_errors(plain))
    if err is not None:
        where = "/".join(str(p) for p in err.absolute_path) or "params"
        raise ExternalError(f"{where}: {err.message}"[:300])
    return plain
