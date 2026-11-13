"""The line between the gate and the rest (docs/SENDGATE.md, package 0).

- Every file under src/, mac-helper/, menubar/ and dev/ belongs to exactly one bin of dev/provenance.toml, so the fork can later
  be cut into what goes upstream, what becomes Sendgate and what stays.
- Nothing in icloud_mcp.gate imports a service, the server or its settings, so the gate can move without them.
- The old module paths are the same modules as their new homes, so imports and monkeypatches written against them still work.
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import subprocess
from pathlib import Path

import pytest

import tomllib

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "src" / "icloud_mcp" / "gate"
DIRS = ("src", "mac-helper", "menubar", "dev")

# What the gate must not import: the services, the chat model, the Mac bridge, the server, its settings and pages, a plugin.
FORBIDDEN = ("mail", "cal", "contacts", "imessage", "messages", "beeper", "bridge", "server", "config", "settings", "dashboard",
             "dashboard_api", "approvals", "plugins")

MOVED = {"actions": "actions", "external": "external", "outbox": "outbox", "approval_status": "status",
         "approval_withdraw": "withdraw", "audit": "audit", "push": "push"}


def _files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", *DIRS],
                             cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return sorted(set(x for x in out.splitlines() if x and (ROOT / x).exists()))


def _matches(pattern: str, path: str) -> bool:
    return path.startswith(pattern[:-2]) if pattern.endswith("/**") else path == pattern


def _bins() -> dict[str, list[str]]:
    return tomllib.loads((ROOT / "dev" / "provenance.toml").read_text())["bins"]


def test_every_file_has_exactly_one_bin():
    """protects: the provenance file stays complete as files are added or moved.
    fails_when: a file matches no entry, or two.
    no_claim: that a file sits in the right bin; that is a judgement the file records, not something this test can check."""
    bins = _bins()
    unsorted, twice = [], []
    for path in _files():
        hits = [name for name, patterns in bins.items() for p in patterns if _matches(p, path)]
        (unsorted if not hits else twice if len(hits) > 1 else []).append(f"{path} {hits}" if hits else path)
    assert not unsorted, "add these to dev/provenance.toml: " + ", ".join(unsorted)
    assert not twice, "these match more than one entry: " + ", ".join(twice)


def test_every_entry_still_names_a_file():
    """protects: entries do not outlive their files, so the table says what is there.
    fails_when: an entry matches no tracked file.
    no_claim: none beyond protects."""
    files = _files()
    stale = [p for patterns in _bins().values() for p in patterns if not any(_matches(p, f) for f in files)]
    assert not stale, "these entries match no file: " + ", ".join(stale)


def _imports(path: Path, text: str | None = None) -> list[str]:
    """The icloud_mcp modules a file imports, as names relative to the package ('mail', 'gate.outbox'), wherever the import is."""
    tree, out = ast.parse(path.read_text() if text is None else text), []
    here = path.relative_to(ROOT / "src" / "icloud_mcp").with_suffix("").parts      # ('gate', 'outbox')
    package = ".".join(("icloud_mcp", *here[:-1]))

    def ours(name: str) -> bool:
        return name == "icloud_mcp" or name.startswith("icloud_mcp.")

    def short(name: str) -> str:
        return name.removeprefix("icloud_mcp").strip(".")

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = importlib.util.resolve_name("." * node.level + (node.module or ""), package) if node.level else node.module or ""
            if not ours(base):
                continue
            if not node.module or base == "icloud_mcp":       # from .. import config, from icloud_mcp import mail: the names are modules
                out += [short(f"{base}.{a.name}") for a in node.names]
            else:
                out.append(short(base))
        elif isinstance(node, ast.Import):
            out += [short(a.name) for a in node.names if ours(a.name)]
    return out


def test_the_gate_imports_no_service():
    """protects: the gate can be lifted out of this server, or offered upstream as a package, without taking a service along.
    fails_when: a module under icloud_mcp/gate imports one of FORBIDDEN, at the top or inside a function.
    no_claim: that the gate works without the server; textedit, callctx and the rest it imports still live outside gate/."""
    bad = []
    for path in sorted(GATE.rglob("*.py")):
        for mod in _imports(path):
            if mod.split(".")[0] in FORBIDDEN:
                bad.append(f"{path.relative_to(ROOT)} imports {mod}")
    assert not bad, "\n".join(bad)


def test_the_import_walk_sees_a_forbidden_import():
    """protects: the check above can fail at all.
    fails_when: a relative import of mail from inside gate/ is not reported as 'mail'.
    no_claim: none beyond protects."""
    probe = GATE / "probe.py"                   # never written: the text is passed in
    text = "from ..mail import MailService\nfrom .. import config, beeper\nfrom .outbox import Outbox\nimport icloud_mcp.cal\n" \
           "from icloud_mcp import contacts as c\nimport json\nfrom typing import Any\n" \
           "def f():\n    from ..plugins.beeper import gateway\n"
    assert _imports(probe, text) == ["mail", "config", "beeper", "gate.outbox", "cal", "contacts", "plugins.beeper"]


def test_every_approval_source_shares_one_base():
    """protects: the dashboard and the gate use one ApprovalSource, so a method added to it reaches every kind of request.
    fails_when: dashboard.py defines its own ApprovalSource again, for example after an upstream merge is resolved by hand.
    no_claim: that each source implements the interface correctly; their own tests cover that."""
    from icloud_mcp import dashboard, external
    from icloud_mcp.gate.source import ApprovalSource
    assert dashboard.ApprovalSource is ApprovalSource
    for cls in (dashboard.MailApprovals, dashboard.CalendarApprovals, dashboard.IMessageApprovals, external.ExternalApprovals):
        assert issubclass(cls, ApprovalSource), cls.__name__


@pytest.mark.parametrize("old,new", sorted(MOVED.items()))
def test_an_old_module_path_is_the_new_module(old, new):
    """protects: code and tests that import or monkeypatch icloud_mcp.<old> reach the module that runs.
    fails_when: the old path gives a copy instead of the module itself.
    no_claim: that every name the old module had is still there; the rest of the suite imports those."""
    assert importlib.import_module(f"icloud_mcp.{old}") is importlib.import_module(f"icloud_mcp.gate.{new}")
