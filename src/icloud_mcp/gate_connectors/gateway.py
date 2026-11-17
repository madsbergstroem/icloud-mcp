"""The client for an MCP gateway (a LiteLLM MCP endpoint): one tool call with a bearer key, and where a failure happened.

Shared by every connector that reaches a service through the gateway: Beeper (plugins/beeper) and the file-defined actions of
gate_connectors. Its rule decides what an executor may answer: `failed` only when the call provably never reached the tool (the
gateway could not be reached, or it refused the key before the call), `unclear` once the call may have left.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping

log = logging.getLogger(__name__)


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

    def list_tools(self) -> list[str]:
        """The names of the tools this key may see. GatewayError when the gateway cannot be asked."""
        return asyncio.run(self._list())

    async def _list(self) -> list[str]:
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        names: list[str] = []
        try:
            async with self._http({"Authorization": f"Bearer {self._key}"}) as http:
                async with Client(streamable_http_client(self.url, http_client=http, terminate_on_close=False),
                                  read_timeout_seconds=self.timeout) as client:
                    cursor = None
                    for _ in range(100):                  # every page: a tool on a later page counts as much as one on the first
                        got = await asyncio.wait_for(client.list_tools(cursor=cursor) if cursor else client.list_tools(), self.timeout + 5)
                        names += [t.name for t in getattr(got, "tools", got)]
                        cursor = getattr(got, "next_cursor", None) or getattr(got, "nextCursor", None)
                        if not cursor:
                            break
        except Exception:  # noqa: BLE001  -- nothing the gateway said is kept
            raise GatewayError("unreachable", True) from None
        return names

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
