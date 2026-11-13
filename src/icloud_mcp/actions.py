"""Moved to `icloud_mcp.gate.actions` (docs/SENDGATE.md, package 0). This name stays the same module, so existing imports and tests
that replace its functions keep working."""
import sys

from .gate import actions as _moved

sys.modules[__name__] = _moved
