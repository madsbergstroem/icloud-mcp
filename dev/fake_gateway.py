"""A made-up MCP gateway for tests and for writing a connector file: tools you define, keys with grants, and the ways a real
gateway fails. Nothing leaves the process.

    from fake_gateway import FakeGateway
    gw = FakeGateway(grants={"exec-key": {"market-send"}, "agent-key": {"market-read"}})

    @gw.tool("market-send")
    def send(site: str, op: str, args: dict, idempotencyKey: str = "") -> dict:
        return {"write": "applied"}

    from icloud_mcp.gate_connectors.actions import Connection
    conn = Connection("http://gateway.test/mcp", "exec-key", make_http=gw.make_http)

`calls` records every tool call as (key, tool, arguments). `mode` makes it fail: "down" (no connection: nothing reached a tool),
"refuse" (401 for every key), "lose" (the call is carried out, the answer is lost), "slow" (the answer comes after `delay`
seconds); `only` limits "lose" and "slow" to the tools named in it. A key without a grant for a tool gets 403 for calling it and does not see it in tools/list.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings


class FakeGateway:
    def __init__(self, grants: dict[str, set[str]]):
        self.grants = {f"Bearer {k}": set(v) for k, v in grants.items()}
        self.tools: dict[str, Callable[..., Any]] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.lists: list[str] = []
        self.mode: str | None = None
        self.delay = 0.0
        self.only: set[str] | None = None        # with a mode: only calls of these tools fail; None: every call

    def tool(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def add(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name] = fn
            return fn
        return add

    def _app(self, key: str) -> Any:
        server = MCPServer("fake-gateway")
        for name, fn in self.tools.items():
            if name in self.grants.get(key, set()):
                server.tool(name=name)(self._recorded(key, name, fn))
        return server.streamable_http_app(stateless_http=True, json_response=True,
                                          transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    def _recorded(self, key: str, name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        import functools

        @functools.wraps(fn)
        def call(*a: Any, **kw: Any) -> Any:
            self.calls.append((key.removeprefix("Bearer "), name, dict(kw)))
            return fn(*a, **kw)
        return call

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        key = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
        body, more = b"", True
        while more:
            m = await receive()
            body, more = body + m.get("body", b""), m.get("more_body", False)
        msg = {}
        try:
            msg = json.loads(body or b"{}")
        except ValueError:
            pass
        method = msg.get("method") if isinstance(msg, dict) else None
        tool = ((msg.get("params") or {}).get("name") if method == "tools/call" else None) if isinstance(msg, dict) else None
        if method == "tools/list":
            self.lists.append(key.removeprefix("Bearer "))
        if self.mode == "refuse" or key not in self.grants or (tool and tool not in self.grants[key]):
            status = 401 if self.mode == "refuse" or key not in self.grants else 403
            await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"error":"no"}'})
            return
        app = self._app(key)
        replay = [{"type": "http.request", "body": body, "more_body": False}]

        async def again() -> dict:
            return replay.pop(0) if replay else {"type": "http.disconnect"}

        async def drop(_: dict) -> None:
            pass
        hit = tool is not None and (self.only is None or tool in self.only)
        lose = self.mode == "lose" and hit
        if self.mode == "slow" and hit:
            await asyncio.sleep(self.delay)
        async with app.router.lifespan_context(app):
            await app(scope, again, drop if lose else send)
        if lose:
            await send({"type": "http.response.start", "status": 502, "headers": []})
            await send({"type": "http.response.body", "body": b""})

    def make_http(self, headers: dict[str, str], timeout: float) -> Any:
        class Down(httpx2.AsyncBaseTransport):
            async def handle_async_request(self, request: Any) -> Any:
                raise httpx2.ConnectError("no route")
        transport = Down() if self.mode == "down" else httpx2.ASGITransport(app=self)
        return httpx2.AsyncClient(transport=transport, base_url="http://gateway.test", headers=headers, timeout=timeout)
