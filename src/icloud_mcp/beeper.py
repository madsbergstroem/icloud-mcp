"""The Beeper code moved into the plugin `icloud_mcp.plugins.beeper`. This name stays the same module, so existing imports and
tests that replace its functions keep working."""
import sys

from .plugins.beeper import gateway as _gateway

sys.modules[__name__] = _gateway
