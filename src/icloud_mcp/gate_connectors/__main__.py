"""Check a connector file before the server reads it (docs/SENDGATE.md, "Connector file reference"):

    python -m icloud_mcp.gate_connectors check FILE [--probe]

It reads the file with the same loader the server uses and prints each action as its card would show it. With --probe it also
asks the gateway (GATE_GATEWAY_URL, GATE_EXEC_KEY from the environment) whether the execution key can see each action's tools,
and, when GATE_AGENT_KEY_PROBE is set, whether an agent's key can see the send tool (then agents could bypass the approval).
Exit codes: 0 fine, 2 the file cannot be used (the reason, with the JSON pointer, on stderr), 1 the probe found a problem.
"""
from __future__ import annotations

import os
import sys

from .actions import ENV_KEY, ENV_PROBE, ENV_URL, LoadError, load
from .gateway import Gateway, GatewayError


def _preview(fa) -> str:
    rows = ", ".join(f"{label} ({name})" for label, name in fa.rows)
    lines = [f"{fa.action}: “{fa.title}” [{fa.badge}], button “{fa.approve_label}”",
             f"  card: {rows}" + (f", then the text ({fa.text_field})" if fa.text_field else ""),
             f"  reaches: {', '.join(fa.recipient_fields)}; who may be reached: the setting {fa.allow_setting} (empty means nobody)",
             f"  calls {fa.tool}" + (f"; asks {fa.describe['tool']} first, checks {', '.join(fa.describe.get('recheck') or []) or 'nothing'} again before sending" if fa.describe else ""),
             f"  sent when {fa.result['path']} is one of {fa.result['sent']}; failed (it waits again) when one of {fa.result['failed']}; "
             "anything else is unclear and never repeated. Only list under failed what provably had no effect."]
    if not fa.enabled:
        lines.append("  enabled: false (read, not offered)")
    return "\n".join(lines)


def _probe(actions, make_http=None) -> int:
    url, key = os.environ.get(ENV_URL, "").strip(), os.environ.get(ENV_KEY, "").strip()
    if not url or not key:
        print(f"--probe needs {ENV_URL} and {ENV_KEY} in the environment.", file=sys.stderr)
        return 1
    problems = 0
    try:
        seen = set(Gateway(url, key, 15, make_http=make_http).list_tools())
    except GatewayError as e:
        print(f"probe: the execution key could not list tools ({e}).", file=sys.stderr)
        return 1
    for fa in actions:
        for tool in [fa.tool] + ([fa.describe["tool"]] if fa.describe else []):
            ok = tool in seen
            problems += not ok
            print(f"probe: {fa.action}: the execution key {'sees' if ok else 'does NOT see'} {tool}")
        if fa.describe and fa.describe["tool"] == fa.tool:
            print(f"probe: {fa.action}: describe calls the send tool itself; it would send while asking", file=sys.stderr)
            problems += 1
    for probe_key in [k.strip() for k in os.environ.get(ENV_PROBE, "").split(",") if k.strip()]:
        try:
            agent_sees = set(Gateway(url, probe_key, 15, make_http=make_http).list_tools())
        except GatewayError as e:
            print(f"probe: an agent key could not list tools ({e}); the bypass was not checked.", file=sys.stderr)
            problems += 1
            continue
        for fa in actions:
            if fa.tool in agent_sees:
                problems += 1
                print(f"probe: {fa.action}: an agent key can see {fa.tool}; agents could bypass the approval. Remove that grant.", file=sys.stderr)
    return 1 if problems else 0


def main(argv: list[str], make_http=None) -> int:
    args = [a for a in argv[1:] if a != "--probe"]
    if len(args) != 2 or args[0] != "check":
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        actions = load(args[1])
    except LoadError as e:
        print(f"{args[1]}: {e}", file=sys.stderr)
        return 2
    for fa in actions:
        print(_preview(fa))
    print(f"{len(actions)} action(s), the file is fine.")
    return _probe(actions, make_http) if "--probe" in argv else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
