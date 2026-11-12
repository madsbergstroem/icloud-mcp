"""Archiving and unarchiving Beeper chats for messages_archive_chat (a messages.Archiver).

It goes through the gateway with the exec key, the key that already sends after approval and reads a chat's details, never with
the read key: a key that only reads must not change anything. The tool is Beeper's `archive_chat` on the same server entry as
BEEPER_CHAT_TOOL (`beeper_alex-get_chat` gives `beeper_alex-archive_chat`); BEEPER_ARCHIVE_TOOL names another one where needed.

Before it archives, it asks Beeper for the chat (`get_chat`, metadata only): a chat Beeper does not know, or a service sender while
the owner hides those (BEEPER_HIDE_SHORT_CODES), is refused as unknown, as it is when read. The hub has already refused chats on the
owner's hidden list or outside "only these chats". Nothing is stored; the log lines come from the hub and name no chat.
"""
from __future__ import annotations

from typing import Any

from ...messages import MessagesError, Unclear, make_id
from . import gateway as G
from . import reading as R

ARCHIVE_TOOL = "BEEPER_ARCHIVE_TOOL"


def archive_tool(s: Any) -> str:
    """BEEPER_ARCHIVE_TOOL, or archive_chat on the server entry of BEEPER_CHAT_TOOL."""
    own = s.plugin_value(ARCHIVE_TOOL).strip()
    if own:
        return own
    server, dash, _tool = s.beeper_chat_tool.rpartition("-")
    return f"{server if dash else 'beeper'}-archive_chat"


class BeeperArchiver:
    name = R.PREFIX

    def __init__(self, beeper: G.Beeper, tool: str, s: Any):
        self.beeper, self.tool = beeper, tool
        self.short_codes = s.plugin_value(R.SHORT_CODES, "true").strip().lower() in R._TRUE

    def archive_chat(self, chat_id: str, archived: bool) -> dict[str, Any]:
        unknown = f"No conversation with chat_id '{make_id(R.PREFIX, chat_id)}' (take it from messages_list_chats). Nothing changed."
        try:
            chat = self.beeper.chat(chat_id)
        except G.GatewayError as e:
            raise MessagesError(f"Could not read that chat from Beeper ({e}). Nothing changed; try again in a moment.") from None
        if chat is None or (self.short_codes and R.short_code(chat.title, "group" if chat.is_group else "single")):
            raise MessagesError(unknown)
        try:
            answer = self.beeper.gateway.call(self.tool, {"chatID": chat_id, "archived": bool(archived)})
        except G.GatewayError as e:
            if e.before_send:
                raise MessagesError(f"Beeper could not be asked ({e}). Nothing changed; try again in a moment.") from None
            raise Unclear("No answer came back from Beeper, so it is not known whether the chat changed. Asking again is safe: "
                          "archiving an archived chat changes nothing.") from None
        if answer.is_error:
            raise MessagesError("Beeper turned the request away. Nothing changed as far as Beeper says.")
        return {}


def archiver(s: Any) -> BeeperArchiver | None:
    """The archiver while Beeper can be written to (gateway and exec key) and read (reading on); otherwise None."""
    if not (G.configured(s) and R.reading_on(s)):
        return None
    return BeeperArchiver(G.connect(s), archive_tool(s), s)
