"""The contract a service plugin is written against: plugin_api version 1 (docs/SENDGATE.md, package 7).

A plugin imports from this module only, and says which version it was written for with `Plugin(api=1, ...)`. The core loads a
plugin whose `api` it supports (API_VERSIONS in plugins/__init__.py) and skips any other with one log line and a row on Connect a
service. A plugin without `api` predates the field: during v1 it is loaded as v1 with a warning, from v2 on it is refused.

What stays v1: adding an optional field with a default, a new capability or a new name here. What makes v2: renaming or removing a
name, changing a type, or changing what a callback receives.

Every callback gets a PluginContext (value, secret, approval_link, plugin_id), never the server's settings. That limits what a
plugin is handed, not what Python lets it import: a plugin runs in the server's process. The names re-exported here are the
promise; anything else of icloud_mcp may change without notice.
"""
from __future__ import annotations

from typing import Any

from .gate.actions import ActionRequest, ActionType, Executor, ExternalError, Hold, Result
from .gate_connectors.gateway import Answer, Gateway, GatewayError
from .messages import (Account, Archiver, Backend, Chat, ChatFilter, Filters, Match, Matches, Message, MessagesError, Page, Person,
                       SendRequest, Sender, Unclear, make_id, split_id)
from .plugins import (AccountRef, ActionSpec, Capabilities, ChatRef, Connection, Field, Listing, MessagesCapability, Plugin,
                      PluginContext)
from .settings import Group, Spec, changed, more_history, removes_entry, to_false, to_true

API_VERSION = 1


def released_answer(got: Any) -> dict[str, Any]:
    """What a queuing tool answers when a standing rule let the request through at once (package 10b): the release's outcome."""
    from .gate.external import released_answer as answer
    return answer(got)


__all__ = [
    "API_VERSION",
    # the plugin and what it declares
    "Plugin", "Connection", "Field", "Listing", "AccountRef", "Capabilities", "MessagesCapability", "ChatRef", "ActionSpec",
    "PluginContext",
    # actions the owner approves
    "ActionType", "ActionRequest", "Executor", "Hold", "Result", "ExternalError", "released_answer",
    # chats
    "Backend", "Sender", "Archiver", "SendRequest", "Account", "Chat", "ChatFilter", "Filters", "Match", "Matches", "Message",
    "MessagesError", "Unclear", "Page", "Person", "make_id", "split_id",
    # settings the dashboard draws, and when a change loosens protection
    "Group", "Spec", "changed", "more_history", "removes_entry", "to_false", "to_true",
    # the LLM gateway client
    "Gateway", "GatewayError", "Answer",
]
