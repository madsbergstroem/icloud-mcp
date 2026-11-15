"""Actions defined in a file: a service behind the MCP gateway becomes an action the owner approves, with no Python
(docs/SENDGATE.md, package 2, and "Connector file reference").

Whoever runs the server writes the file and names it in GATE_ACTIONS_FILE; GATE_GATEWAY_URL and GATE_EXEC_KEY say where the
gateway is and which key carries actions out (environment only, masked in every error). The owner keeps each action's list of who
it may reach in the setting the file names (`allow_setting`); empty means nobody, `*` anyone.

For each action of the file this builds an ActionSpec (plugins/__init__.py), so the gate treats it like any plugin action:

- prepare = the file's `describe`: asks the service about the target with the agent's parameters and copies fields of its answer
  into the request (they are readOnly in the schema), so the card shows the service's words. A failure refuses the request
  (describe_failed, retryable; nothing queued).
- recheck = `describe` again, after the owner approved and before the send: when a field named in `recheck` differs, nothing is
  sent and the request ends as discarded (target_changed). A failure puts it back (nothing was sent).
- executor = one call of `tool` with `arguments`, its answer read through `result`: a value in `sent` is sent, one in `failed` is
  failed (it waits again), anything else is unclear (never repeated). A gateway that could not be reached or refused the key
  before the call is failed; once the call may have left, unclear.

The file is checked completely when it is read: its own schema, then every rule that schema cannot say (the rows, the copies, the
substitutions, the first row). A file that fails stops the start with the file, a JSON pointer and the command that checks it;
nothing of a half-read file is registered.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from ..gate import actions as A
from .gateway import Gateway, GatewayError

ENV_FILE, ENV_URL, ENV_KEY, ENV_PROBE = "GATE_ACTIONS_FILE", "GATE_GATEWAY_URL", "GATE_EXEC_KEY", "GATE_AGENT_KEY_PROBE"
RESERVED_SETTINGS = {"SEND_ALLOWLIST", "IMESSAGE_SEND_ALLOWLIST", "BEEPER_SEND_ALLOWLIST"}   # the core's and Beeper's own lists
SCALARS = {"string", "number", "integer", "boolean"}
CHECK = "python -m icloud_mcp.gate_connectors check"
_PATH = r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$"        # dotted, no array index (X8)

FILE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://example.org/sendgate/actions.schema.json",
    "title": "Sendgate connector file, version 1",
    "type": "object",
    "additionalProperties": False,
    "required": ["version", "actions"],
    "properties": {
        "$schema": {"type": "string"},
        "version": {"const": 1},
        "actions": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/action"}},
    },
    "$defs": {
        "action": {
            "type": "object",
            "additionalProperties": False,
            "required": ["action", "title", "badge", "approve_label", "tool", "arguments", "params_schema", "recipient_fields",
                         "rows", "result", "allow_setting"],
            "properties": {
                "action": {"type": "string", "pattern": r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"},
                "title": {"type": "string", "minLength": 1, "maxLength": 80},
                "badge": {"type": "string", "minLength": 1, "maxLength": 20},
                "approve_label": {"type": "string", "minLength": 1, "maxLength": 12},
                "tool": {"type": "string", "pattern": r"^[A-Za-z0-9_.:-]+$"},
                "arguments": {"type": "object"},
                "params_schema": {"type": "object"},
                "text_field": {"type": "string"},
                "recipient_fields": {"type": "array", "minItems": 1, "items": {"type": "string"}, "uniqueItems": True},
                "rows": {"type": "array", "minItems": 1,
                         "items": {"type": "array", "prefixItems": [{"type": "string", "minLength": 1}, {"type": "string"}],
                                   "minItems": 2, "maxItems": 2}},
                "describe": {
                    "type": "object", "additionalProperties": False, "required": ["tool", "arguments", "copy"],
                    "properties": {
                        "tool": {"type": "string", "pattern": r"^[A-Za-z0-9_.:-]+$"},
                        "arguments": {"type": "object"},
                        "copy": {"type": "object", "minProperties": 1, "additionalProperties": {"type": "string", "pattern": _PATH}},
                        "recheck": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
                    },
                },
                "result": {
                    "type": "object", "additionalProperties": False, "required": ["path", "sent", "failed"],
                    "properties": {
                        "path": {"type": "string", "pattern": _PATH},
                        "sent": {"type": "array", "minItems": 1, "items": {"type": ["string", "number", "boolean"]}},
                        "failed": {"type": "array", "items": {"type": ["string", "number", "boolean"]}},
                    },
                },
                "allow_setting": {"type": "string", "pattern": r"^[A-Z][A-Z0-9_]*_ALLOWLIST$"},
                "enabled": {"type": "boolean"},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 120},
            },
        },
    },
}


class LoadError(Exception):
    """The file cannot be used. `pointer` is the JSON pointer of the place in the file."""

    def __init__(self, pointer: str, message: str):
        super().__init__(f"{pointer or '/'}: {message}")
        self.pointer, self.message = pointer, message


@dataclass(frozen=True)
class FileAction:
    action: str
    title: str
    badge: str
    approve_label: str
    tool: str
    arguments: Mapping[str, Any]
    params_schema: Mapping[str, Any]
    text_field: str | None
    recipient_fields: tuple[str, ...]
    rows: tuple[tuple[str, str], ...]
    describe: Mapping[str, Any] | None
    result: Mapping[str, Any]
    allow_setting: str
    enabled: bool = True
    timeout_seconds: float = 30.0
    action_type: Any = field(default=None, compare=False)


# -- substitution and paths ---------------------------------------------------------------------------------------------------
def substitute(template: Any, params: Mapping[str, Any], attempt_key: str | None) -> Any:
    """The file's arguments with its placeholders filled in. Only whole string values are replaced: "$params" (all parameters
    the agent may set), "$params.<name>" (one of them), "$attempt_key" (<outbox_id>.<attempt>); "$$..." is a literal "$...".
    Anything else was refused when the file was read."""
    if isinstance(template, dict):                    # a $params.<name> the agent left out is left out of the call too
        return {k: substitute(v, params, attempt_key) for k, v in template.items()
                if not (isinstance(v, str) and v.startswith("$params.") and v[len("$params."):] not in params)}
    if isinstance(template, list):
        return [substitute(v, params, attempt_key) for v in template]
    if isinstance(template, str) and template.startswith("$"):
        if template.startswith("$$"):
            return template[1:]
        if template == "$params":
            return dict(params)
        if template.startswith("$params."):
            return params.get(template[len("$params."):])
        if template == "$attempt_key":
            return attempt_key
    return template


def _same(a: Any, b: Any) -> bool:
    """Equal in type and value: true is not 1, and 1 is not "1" (Python's == would say yes to the first)."""
    kind = lambda x: "bool" if isinstance(x, bool) else "number" if isinstance(x, (int, float)) else type(x).__name__   # noqa: E731
    return kind(a) == kind(b) and a == b


def read_path(data: Any, dotted: str) -> Any:
    """The value at a dotted path of a tool's answer, or None when it is not there."""
    for part in dotted.split("."):
        if not isinstance(data, Mapping) or part not in data:
            return None
        data = data[part]
    return data


def _placeholders(node: Any, pointer: str) -> list[tuple[str, str]]:
    if isinstance(node, dict):
        return [x for k, v in node.items() for x in _placeholders(v, f"{pointer}/{k}")]
    if isinstance(node, list):
        return [x for i, v in enumerate(node) for x in _placeholders(v, f"{pointer}/{i}")]
    return [(pointer, node)] if isinstance(node, str) and node.startswith("$") and not node.startswith("$$") else []


# -- reading the file -----------------------------------------------------------------------------------------------------------
def load(path: str | Path) -> list[FileAction]:
    """Every action of the file, checked. LoadError for the first thing wrong."""
    try:
        doc = json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise LoadError("", f"the file {path} does not exist") from None
    except (OSError, ValueError) as e:
        raise LoadError("", f"the file cannot be read as JSON ({type(e).__name__})") from None
    import jsonschema
    err = jsonschema.exceptions.best_match(jsonschema.Draft202012Validator(FILE_SCHEMA).iter_errors(doc))
    if err is not None:
        pointer = "/" + "/".join(str(p) for p in err.absolute_path)
        raise LoadError(pointer if err.absolute_path else "", err.message[:300])
    out, seen = [], set()
    for i, a in enumerate(doc["actions"]):
        at = f"/actions/{i}"
        if a["action"] in seen:
            raise LoadError(f"{at}/action", f"{a['action']} is defined twice")
        seen.add(a["action"])
        out.append(_check(a, at))
    return out


def _check(a: Mapping[str, Any], at: str) -> FileAction:
    schema = a["params_schema"]
    props = schema.get("properties") or {}
    if schema.get("type") != "object" or not isinstance(props, Mapping):
        raise LoadError(f"{at}/params_schema", "must describe an object with properties")
    read_only = {n for n, p in props.items() if isinstance(p, Mapping) and p.get("readOnly") is True}
    describe = a.get("describe")
    copies = dict(describe["copy"]) if describe else {}
    if a["allow_setting"] in RESERVED_SETTINGS or a["allow_setting"].startswith("GATE_"):
        raise LoadError(f"{at}/allow_setting", f"{a['allow_setting']} is a setting of its own; name one for this action")
    required = set(schema.get("required") or [])
    for name in a["recipient_fields"]:
        if name not in props:
            raise LoadError(f"{at}/recipient_fields", f"{name} is not a property of params_schema")
        if name in read_only or name not in required:
            raise LoadError(f"{at}/recipient_fields", f"{name} must be a required field the agent sets, so the owner's list always "
                                                     "sees who the request reaches")
    uses_all = any(v == "$params" for _, v in _placeholders(a["arguments"], ""))
    if uses_all and schema.get("additionalProperties") is not False:
        raise LoadError(f"{at}/params_schema", "with $params the schema must set additionalProperties to false, so an agent cannot "
                                               "pass a field the owner never sees")
    for name in read_only:
        spec_ = props[name]
        if spec_.get("type") not in SCALARS or (spec_.get("type") == "string" and not isinstance(spec_.get("maxLength"), int)):
            raise LoadError(f"{at}/params_schema/properties/{name}", "a field describe fills in needs a type of string (with maxLength), "
                                                                     "number, integer or boolean")
    for target in copies:
        if target not in read_only:
            raise LoadError(f"{at}/describe/copy/{target}", f"{target} must be a readOnly property of params_schema")
    if missing := sorted(read_only - set(copies)):
        raise LoadError(f"{at}/params_schema/properties/{missing[0]}", f"{missing[0]} is readOnly but describe does not fill it in")
    for name in (describe or {}).get("recheck", []):
        if name not in copies:
            raise LoadError(f"{at}/describe/recheck", f"{name} is not one of the fields describe copies")
    if describe and describe["tool"] == a["tool"]:
        raise LoadError(f"{at}/describe/tool", "describe must not call the tool that carries the action out: it would act while asking")
    both = [v for v in a["result"]["sent"] if any(_same(v, f) for f in a["result"]["failed"])]
    if both:
        raise LoadError(f"{at}/result", f"{both[0]!r} is listed as sent and as failed")
    first = a["rows"][0][1]
    if first not in set(a["recipient_fields"]) | set(copies):
        raise LoadError(f"{at}/rows/0", "the first row shows who or what the request reaches: name a recipient field or a field describe copies")
    agent_fields = set(props) - read_only
    for where, args, attempt_ok in ((f"{at}/arguments", a["arguments"], True),
                                    *(((f"{at}/describe/arguments", describe["arguments"], False),) if describe else ())):
        for pointer, value in _placeholders(args, where):
            name = value[len("$params."):] if value.startswith("$params.") else None
            ok = value == "$params" or (name in agent_fields) or (value == "$attempt_key" and attempt_ok)
            if not ok:
                raise LoadError(pointer, f"{value!r} is not a placeholder here: use $params, $params.<a field the agent sets>"
                                         + (", $attempt_key" if attempt_ok else "") + ", or $$ for a literal $")
    try:
        at_type = A.ActionType(
            id=a["action"], title=a["title"], badge=a["badge"], approve_label=a["approve_label"], params_schema=schema,
            rows=tuple((label, name) for label, name in a["rows"]), text_field=a.get("text_field"),
            recipients=lambda p, fields=tuple(a["recipient_fields"]): [str(p[f]) for f in fields if p.get(f) not in (None, "")])
    except ValueError as e:
        raise LoadError(at, str(e)) from None
    return FileAction(a["action"], a["title"], a["badge"], a["approve_label"], a["tool"], a["arguments"], schema,
                      a.get("text_field"), tuple(a["recipient_fields"]), tuple((r[0], r[1]) for r in a["rows"]), describe,
                      a["result"], a["allow_setting"], a.get("enabled", True), float(a.get("timeout_seconds", 30)), at_type)


# -- carrying it out ------------------------------------------------------------------------------------------------------------
def _agent_params(fa: FileAction, params: Mapping[str, Any]) -> dict[str, Any]:
    declared = set(fa.params_schema.get("properties") or {}) - A.read_only_fields(fa.action_type)
    return {k: v for k, v in params.items() if k in declared}


class Connection:
    """The gateway as the file's actions use it. `make_http` lets tests and the check command reach a fake gateway."""

    def __init__(self, url: str, key: str, make_http: Any = None):
        self.url, self.key, self.make_http = url, key, make_http

    def client(self, timeout: float) -> Gateway:
        return Gateway(self.url, self.key, timeout, make_http=self.make_http)


def _describe(conn: Connection, fa: FileAction, params: Mapping[str, Any]) -> dict[str, Any]:
    """Ask the service about the target; the copied fields. GatewayError or LookupError when the answer does not have them."""
    answer = conn.client(fa.timeout_seconds).call(fa.describe["tool"], substitute(fa.describe["arguments"], _agent_params(fa, params), None))
    if answer.is_error:
        raise LookupError("the service answered with an error")
    got = {target: read_path(answer.data, path) for target, path in fa.describe["copy"].items()}
    if any(v is None for v in got.values()):
        raise LookupError("the answer lacks a field the file copies")
    import jsonschema
    props = fa.params_schema.get("properties") or {}
    for target, value in got.items():                 # what the service says must fit its field, or nothing of it is used
        if not jsonschema.Draft202012Validator(props[target]).is_valid(value):
            raise LookupError("the answer has a field of another shape than the file declares")
    return got


def prepare(conn: Connection, fa: FileAction, params: Mapping[str, Any]) -> dict[str, Any]:
    if not fa.describe:
        return dict(params)
    try:
        return {**params, **_describe(conn, fa, params)}
    except (GatewayError, LookupError):
        raise A.ExternalError(f"The service behind {fa.action} could not be asked about the target. Nothing was queued.", "describe_failed") from None


def recheck(conn: Connection, fa: FileAction, params: Mapping[str, Any]) -> str | None:
    fields = (fa.describe or {}).get("recheck") or []
    if not fields:
        return None
    now = _describe(conn, fa, params)                 # a failure raises: the gate puts the request back, nothing was sent
    return "target_changed" if any(now.get(f) != params.get(f) for f in fields) else None


class Executor:
    def __init__(self, conn: Connection, fa: FileAction):
        self.conn, self.fa = conn, fa

    def execute(self, request: A.ActionRequest) -> A.Result:
        fa = self.fa
        args = substitute(fa.arguments, _agent_params(fa, request.params), f"{request.id}.{request.attempt}")
        try:
            answer = self.conn.client(fa.timeout_seconds).call(fa.tool, args)
        except GatewayError as e:
            if e.before_send:
                return A.Result("failed", f"{str(e).capitalize()}. Nothing was sent; try again in a moment.")
            return A.Result("unclear", f"Not sure it reached {fa.badge}: {e}. Look there first; nothing will be sent again.")
        if answer.is_error:
            return A.Result("unclear", f"Not sure it reached {fa.badge}: the service answered with an error. Look there first.")
        value = read_path(answer.data, fa.result["path"])
        if any(_same(value, v) for v in fa.result["sent"]):
            return A.Result("sent", f"Done. {fa.badge} accepted it.")
        if any(_same(value, v) for v in fa.result["failed"]):
            return A.Result("failed", f"{fa.badge} did not take it ({value}). Nothing was sent.")
        return A.Result("unclear", f"Not sure it reached {fa.badge}: its answer did not say. Look there first; nothing will be sent again.")


def spec(conn: Connection, fa: FileAction) -> Any:
    """The ActionSpec the gate registers for one action of the file."""
    from ..plugins import ActionSpec
    return ActionSpec(
        action_type=fa.action_type, executor=lambda ctx: Executor(conn, fa),
        allowed=lambda ctx: [x.strip() for x in ctx.value(fa.allow_setting).split(",") if x.strip()],
        prepare=(lambda ctx, p: prepare(conn, fa, p)) if fa.describe else None,
        recheck=(lambda ctx, p: recheck(conn, fa, p)) if (fa.describe or {}).get("recheck") else None)


# -- the plugin and the start ---------------------------------------------------------------------------------------------------
def secrets_of(env: Mapping[str, str]) -> list[str]:
    """What is masked in every error: the keys, and a user or password written into the gateway's address."""
    out = [env.get(ENV_KEY, "")] + [k.strip() for k in env.get(ENV_PROBE, "").split(",")]
    parts = urlsplit(env.get(ENV_URL, ""))
    out += [x for x in (parts.username, parts.password) if x]
    return [x for x in out if x]


def check_start(env: Mapping[str, str] | None = None) -> list[FileAction]:
    """Read GATE_ACTIONS_FILE when it is set. A file that cannot be used, or a missing gateway address or key, stops the start with
    one line that names the file, the place in it and the command that checks it. [] when no file is set."""
    env = os.environ if env is None else env
    path = env.get(ENV_FILE, "").strip()
    if not path:
        return []
    for var in (ENV_URL, ENV_KEY):
        if not env.get(var, "").strip():
            raise SystemExit(f"{ENV_FILE} is set, but {var} is not. Set it too, or unset {ENV_FILE}.")
    try:
        return load(path)
    except LoadError as e:
        raise SystemExit(f"{ENV_FILE} {path}: {e}. Check it with: {CHECK} {path}") from None


def plugin() -> Any:
    """The connector as a service plugin: one ActionSpec per enabled action of GATE_ACTIONS_FILE."""
    from ..plugins import Plugin
    env = os.environ
    found = load(env[ENV_FILE].strip()) if env.get(ENV_FILE, "").strip() else []   # a LoadError skips the plugin; check_start stops the server
    conn = Connection(env.get(ENV_URL, "").strip(), env.get(ENV_KEY, "").strip())
    live = [fa for fa in found if fa.enabled]
    return Plugin(
        id="gateway", name="Gateway actions", status="Set up by whoever runs this server",
        limits="Actions come from the file whoever runs this server wrote; each reaches only who you list.",
        description="Actions of services behind the MCP gateway, carried out after your approval.",
        actions=tuple(spec(conn, fa) for fa in live),
        settings_keys=frozenset(fa.allow_setting for fa in found),
        enabled=lambda s: bool(live),
        secrets=lambda s: secrets_of(env))
