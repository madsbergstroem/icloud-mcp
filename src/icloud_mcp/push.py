"""Moved to `icloud_mcp.gate.push` (docs/SENDGATE.md, package 0). This name stays the same module, so existing imports and tests
that replace its functions keep working."""
import sys

from .gate import push as _moved

sys.modules[__name__] = _moved
