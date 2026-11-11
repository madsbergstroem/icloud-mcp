"""Beeper messages through the approval hub: an agent asks, the owner approves, this server sends through the gateway.

An agent calls `messages_send_request` with a chat id that starts with `beeper:`. Nothing is sent: the request waits on the owner's dashboard as an action of type
`beeper.message.send` (actions.py, external.py). When the owner approves, the executor here sends it once, through the LLM gateway
(a LiteLLM MCP endpoint), with a key of its own that may send on one server and read chat details on another. This server holds
no Beeper token and no copy of the owner's chats.

What the card shows comes from Beeper, not from the agent: the tool asks Beeper for the chat (`get_chat`, metadata only, no
participants' names and no messages) and stores title, network and group or single with the request (and the size, when the answer has one). The executor asks again just before
it sends, and when the chat is not the one the owner looked at (renamed, another network, more people) it sends nothing and the
entry stays, so the owner discards it and asks again.

What happens to the entry after an approval (actions.py has the general rule):

- `sent`: Beeper accepted the message.
- `failed`: nothing reached Beeper's send tool: the gateway could not be reached or refused the key (401, 403) before the send,
  or the chat is not what was approved. The entry goes back to waiting.
- `unclear`: the send left and the answer did not come back or could not be read (a timeout, a 5xx, an error from the tool).
  Never repeated here. The owner is told to look in the chat first.

Everything the owner can change about who may be reached is the list `BEEPER_SEND_ALLOWLIST` (chat ids as Beeper gives them, `*`
for every chat, empty for nobody). It is checked when a request is queued and again when it is approved.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from mcp.server.mcpserver.exceptions import ToolError

from . import actions as A
from .config import Settings
from .messages import SendRequest

log = logging.getLogger(__name__)

ACTION_ID = "beeper.message.send"
MAX_TEXT = 10000
MAX_ID = 300


class GatewayError(Exception):
    """The gateway call did not give an answer. `before_send` is True when nothing can have reached the tool: the gateway could not
    be reached, or it refused the key (401, 403). The message is ours and holds nothing the gateway said."""

    WORDS = {"unreachable": "the gateway could not be reached", "auth": "the gateway refused the key",
             "unknown": "no readable answer came back"}

    def __init__(self, kind: str, before_send: bool):
        super().__init__(self.WORDS.get(kind, kind))
        self.kind, self.before_send = kind, before_send


@dataclass(frozen=True)
class Answer:
    is_error: bool
    data: Any            # the structured result, or the text parsed as JSON, or None
    text: str


@dataclass(frozen=True)
class Chat:
    title: str
    network: str
    is_group: bool
    people: int | None


def _classify(sent: bool, statuses: list[int]) -> GatewayError:
    """Where a failure happened decides whether it is safe to try again. The client reports an HTTP error as a plain JSON-RPC error
    without its status, so the statuses the gateway answered with are collected on the way (`statuses`, of the calls made in the
    phase that failed). 401 and 403 are the gateway itself turning the call away: nothing was dispatched. Without such a status,
    a failure before the call left is a gateway that could not be reached; one after it left is not known."""
    if any(s in (401, 403) for s in statuses):
        return GatewayError("auth", True)
    return GatewayError("unknown", False) if sent else GatewayError("unreachable", True)


class Gateway:
    """Calls tools on an MCP endpoint of the gateway with a bearer key. One short connection per call (the gateway keeps no session
    for us), run from a worker thread. `make_http(headers, timeout)` builds the HTTP client; tests pass one that talks to a local app."""

    def __init__(self, url: str, key: str, timeout: float = 30.0, make_http: Callable[[dict[str, str], float], Any] | None = None):
        self.url, self._key, self.timeout, self._make_http = url, key, float(timeout), make_http

    def _http(self, headers: dict[str, str]) -> Any:
        if self._make_http is not None:
            return self._make_http(headers, self.timeout)
        import httpx2                    # the HTTP library `mcp` itself uses
        return httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(self.timeout))

    def call(self, tool: str, arguments: Mapping[str, Any]) -> Answer:
        return asyncio.run(self._call(tool, dict(arguments)))

    async def _call(self, tool: str, arguments: dict[str, Any]) -> Answer:
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        headers = {"Authorization": f"Bearer {self._key}"}
        sent, result, seen, mark = False, None, [], 0

        async def note(response: Any) -> None:
            seen.append((response.request.method, response.status_code))

        try:
            async with self._http(headers) as http:
                http.event_hooks = {"request": list(http.event_hooks.get("request", [])),
                                    "response": [*http.event_hooks.get("response", []), note]}
                async with Client(streamable_http_client(self.url, http_client=http, terminate_on_close=False),
                                  read_timeout_seconds=self.timeout) as client:
                    sent, mark = True, len(seen)     # the session is up: from here the request can reach the tool
                    result = await asyncio.wait_for(client.call_tool(tool, arguments, read_timeout_seconds=self.timeout), self.timeout + 5)
        except Exception as e:  # noqa: BLE001  -- the client raises its own, httpx's and exception groups
            if result is None:
                log.warning("gateway call to %s failed (%s)", tool, type(e).__name__)
                raise _classify(sent, [code for method, code in seen[mark:] if method == "POST"]) from None
            # the answer was in hand and only closing the connection failed: that is no reason to doubt it
        return _answer(result)


def _answer(result: Any) -> Answer:
    text = " ".join(getattr(c, "text", "") for c in (getattr(result, "content", None) or []) if getattr(c, "text", None)).strip()
    data = getattr(result, "structured_content", None)
    if data is None and text:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    return Answer(bool(getattr(result, "is_error", False)), data, text)


_TITLE_LINE = re.compile(r"^## (?P<title>.+) \(chatID: (?P<id>[^)]+)\)\s*$", re.M)
_NETWORK_LINE = re.compile(r"^Chat on (?P<network>.+?) \((?P<account>\S+)\)", re.M)
_TYPE_LINE = re.compile(r"^\*\*Type\*\*: (?P<type>single|group)\s*$", re.M)


def _chat_from_text(text: str, chat_id: str) -> Chat | None:
    """Beeper's MCP answers get_chat as text: `## <title> (chatID: <id>)`, `Chat on <network> (<account>)`, `**Type**: single|group`.
    With no participants asked for there is no count. The id in the answer must be the one asked for."""
    title, kind = _TITLE_LINE.search(text), _TYPE_LINE.search(text)
    if title is None or kind is None or title["id"].strip() != chat_id.strip():
        return None
    net = _NETWORK_LINE.search(text)
    return Chat(" ".join(title["title"].split())[:300], " ".join(net["network"].split())[:100] if net else "", kind["type"] == "group", None)


def _chat_of(answer: Answer, chat_id: str = "") -> Chat | None:
    """The chat as Beeper describes it: from its text answer, or from structured data (title, type 'single' or 'group', network,
    participants.total) when a gateway passes that on. None when the answer is not a chat."""
    if answer.is_error:
        return None
    d = answer.data
    if isinstance(d, Mapping) and isinstance(d.get("chat"), Mapping):
        d = d["chat"]
    if not isinstance(d, Mapping) or not isinstance(d.get("title"), str) or d.get("type") not in ("single", "group"):
        # not structured chat data. A text answer may still come wrapped as {"result": "<text>"} (how a string result is passed on)
        text = answer.text or (d.get("result") if isinstance(d, Mapping) and isinstance(d.get("result"), str) else "")
        return _chat_from_text(text, chat_id) if text else None
    parts = d.get("participants")
    total = parts.get("total") if isinstance(parts, Mapping) else None
    if not isinstance(total, int) or isinstance(total, bool):
        items = parts.get("items") if isinstance(parts, Mapping) else None
        total = len(items) if isinstance(items, list) and items else None
    return Chat(" ".join(d["title"].split())[:300], " ".join(str(d.get("network") or "").split())[:100], d["type"] == "group", total)


class BeeperError(Exception):
    """A request that cannot be made; the message is written for the agent."""


class Beeper:
    """What the tool and the executor share: the gateway and the names of the two tools it calls."""

    def __init__(self, gateway: Gateway, chat_tool: str, send_tool: str):
        self.gateway, self.chat_tool, self.send_tool = gateway, chat_tool, send_tool

    def chat(self, chat_id: str) -> Chat | None:
        """The chat as Beeper has it now (names of people are not asked for), or None when Beeper does not know it or answers with
        something that is no chat. GatewayError when the gateway gives no answer."""
        return _chat_of(self.gateway.call(self.chat_tool, {"chatID": chat_id, "maxParticipantCount": 0}), chat_id)

    def send(self, chat_id: str, text: str, reply_to: str | None) -> Answer:
        return self.gateway.call(self.send_tool, {"chatID": chat_id, "text": text, **({"replyToMessageID": reply_to} if reply_to else {})})


# -- the action type ----------------------------------------------------------------------------------------------------------------
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "chat_id": {"type": "string", "minLength": 1, "maxLength": MAX_ID},
        "text": {"type": "string", "minLength": 1, "maxLength": MAX_TEXT},
        "reply_to": {"type": "string", "minLength": 1, "maxLength": MAX_ID},
        # filled in by this server from Beeper, never by the agent: what the card shows about the chat
        "chat_title": {"type": "string", "maxLength": 300},
        "network": {"type": "string", "maxLength": 100},
        "is_group": {"type": "boolean"},
        "participants": {"type": "integer", "minimum": 0},
    },
    "required": ["chat_id", "text", "chat_title", "network", "is_group"],
    "additionalProperties": False,
}


def _warnings(p: Mapping[str, Any]) -> list[str]:
    out = []
    if p.get("is_group"):
        n = p.get("participants")
        out.append(f"A group chat with {n} people: all of them read this." if isinstance(n, int) else "A group chat: everyone in it reads this.")
    return out


ACTION_TYPE = A.ActionType(
    id=ACTION_ID, title="Beeper message", badge="Beeper", approve_label="Send",
    params_schema=SCHEMA,
    rows=(("Chat", "chat_title"), ("Network", "network"), ("People", "participants"), ("Replying to", "reply_to")),   # People only when known
    text_field="text",
    recipients=lambda p: [p["chat_id"]],
    warnings=_warnings,
)


class BeeperExecutor:
    """Sends an approved request. Called from a worker thread, once per approval."""

    def __init__(self, beeper: Beeper):
        self.beeper = beeper

    def execute(self, request: A.ActionRequest) -> A.Result:
        p = request.params
        try:                                    # nothing has been sent yet: any trouble here leaves the entry waiting
            now = self.beeper.chat(p["chat_id"])
        except GatewayError as e:
            return A.Result("failed", f"{str(e).capitalize()}. Nothing was sent; try again in a moment.")
        if now is None:
            return A.Result("failed", "Beeper gives no details for this chat any more. Nothing was sent; discard this request.")
        if (now.title, now.network, now.is_group) != (p.get("chat_title"), p.get("network"), p.get("is_group")) \
                or (now.people is not None and p.get("participants") is not None and now.people != p["participants"]):
            return A.Result("failed", "The chat is not the one you looked at (name, network or number of people changed). "
                                      "Nothing was sent; discard this request and ask again.")
        try:
            answer = self.beeper.send(p["chat_id"], p["text"], p.get("reply_to"))
        except GatewayError as e:
            if e.before_send:
                return A.Result("failed", f"{str(e).capitalize()}. Nothing was sent; try again in a moment.")
            return A.Result("unclear", "Not sure it went through: no answer came back from Beeper. Look in the chat before anything is sent again.")
        if answer.is_error:
            return A.Result("unclear", "Not sure it went through: Beeper answered with an error. Look in the chat before anything is sent again.")
        return A.Result("sent", "Sent. Beeper accepted it.")


# -- wiring ---------------------------------------------------------------------------------------------------------------------------
def configured(s: Settings) -> bool:
    """Beeper messages are on when the gateway address and the key are set. Without them the server has no such tool."""
    return bool(s.beeper_gateway_url and s.beeper_exec_key)


def connect(s: Settings, make_http: Callable[[dict[str, str], float], Any] | None = None) -> Beeper:
    return Beeper(Gateway(s.beeper_gateway_url, s.beeper_exec_key, s.beeper_timeout_seconds, make_http), s.beeper_chat_tool, s.beeper_send_tool)


def register_type(registry: A.ActionRegistry, beeper: Beeper, s: Settings) -> None:
    """The action type, with the owner's list read from the settings every time it is asked."""
    registry.register(ACTION_TYPE, BeeperExecutor(beeper), lambda: s.beeper_send_allowlist)


class BeeperSender:
    """The messages hub's sender for chats whose id starts with `beeper:`. It asks Beeper about the chat, then queues the request as
    an action of type `beeper.message.send` in `external`, the approval source, and sends nothing. `chat_id` is the id as Beeper gives
    it (the prefix is already off); Beeper never sees the prefix."""
    name = "beeper"
    takes_handle = False

    def __init__(self, beeper: Beeper, external: Any, s: Settings):
        self.beeper, self.external, self.s = beeper, external, s

    def send_request(self, req: SendRequest) -> dict[str, Any]:
        chat_id, text = (req.chat_id or "").strip(), req.text or ""
        reply_to, request_id = req.reply_to, req.request_id
        if not chat_id or len(chat_id) > MAX_ID:
            raise ToolError("The id after 'beeper:' is the id Beeper gives a chat (search_chats on the Beeper server).")
        if not text.strip():
            raise ToolError("text is empty.")
        if len(text) > MAX_TEXT:
            raise ToolError(f"text is longer than {MAX_TEXT:,} characters.")
        try:
            chat = self.beeper.chat(chat_id)
        except GatewayError as e:
            raise ToolError(f"Could not read that chat from Beeper ({e}). Nothing was queued; try again in a moment.") from None
        if chat is None:
            raise ToolError("Beeper does not know that chat_id, or cannot read it. Nothing was queued: look it up again with search_chats.")
        params: dict[str, Any] = {"chat_id": chat_id, "text": text, "chat_title": chat.title, "network": chat.network, "is_group": chat.is_group,
                                  **({"participants": chat.people} if chat.people is not None else {}), **({"reply_to": reply_to} if reply_to else {})}
        waiting = {q.id for q in self.external.box.pending()}
        try:
            q = self.external.queue_external(ACTION_ID, params, None, key=request_id)
        except A.ExternalError as e:
            raise ToolError(str(e)) from None
        return {"status": "queued_for_owner_approval", "sent": False, "outbox_id": q.id,
                "chat": {"title": chat.title, "network": chat.network, "group": chat.is_group, **({"people": chat.people} if chat.people is not None else {})},
                "approve_at": self.s.approval_link("external", q.id), "expires_in_seconds": max(0, int(q.expires_at - time.time())),
                **({"already_queued": True} if q.id in waiting else {}),
                "notice": "Queued, NOT sent: it goes out only when the owner approves it on the approval page (approve_at). Say so; do not send it "
                          "again: approval_status with the outbox_id says what became of it."}
