"""Minimal streamable-HTTP MCP client (initialize, ``Mcp-Session-Id``, tools/call).

One client per endpoint. The session is created lazily, re-created once when the
server forgets it (rollout, hub child restart), results are size-capped and
every failure surfaces as :class:`McpToolError` carrying a reason code only
(never the body or the credential).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

MAX_RESULT_CHARS = 20_000


class McpToolError(Exception):
    """A tool call failed; ``reason`` is a short code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def last_sse_json(text: str) -> dict[str, Any] | None:
    """Parse the last ``data:`` JSON line of an SSE body (or a plain JSON body)."""
    last: str | None = None
    for line in text.splitlines():
        if line.startswith("data:"):
            last = line[5:].strip()
    if last is None:
        last = text.strip()
    try:
        parsed = json.loads(last)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


class McpClient:
    def __init__(
        self,
        url: str,
        *,
        token: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 20.0,
        max_result_chars: int = MAX_RESULT_CHARS,
    ) -> None:
        self._url = url
        self._token = token
        self._transport = transport
        self._timeout = timeout_s
        self._max = max_result_chars
        self._session_id: str | None = None
        self._lock = asyncio.Lock()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    async def _post(self, client: httpx.AsyncClient, body: dict[str, Any]) -> httpx.Response:
        return await client.post(self._url, json=body, headers=self._headers())

    async def _initialize(self, client: httpx.AsyncClient) -> None:
        self._session_id = None
        resp = await self._post(
            client,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "nervcenter-agents", "version": "1"},
                },
            },
        )
        resp.raise_for_status()
        self._session_id = resp.headers.get("mcp-session-id")
        try:  # the spec's follow-up notification; servers may ignore it
            await self._post(client, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        except httpx.HTTPError:
            pass

    async def ping(self) -> None:
        """Open a fresh MCP session (``initialize``); raises :class:`McpToolError` with a code."""
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout
            ) as client:
                async with self._lock:
                    await self._initialize(client)
        except httpx.TimeoutException:
            raise McpToolError("timeout") from None
        except httpx.HTTPStatusError as exc:
            raise McpToolError(f"http_{exc.response.status_code}") from None
        except httpx.HTTPError:
            raise McpToolError("unreachable") from None

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        body = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout
            ) as client:
                async with self._lock:
                    if self._session_id is None:
                        await self._initialize(client)
                    resp = await self._post(client, body)
                    if resp.status_code in (400, 404):
                        await self._initialize(client)
                        resp = await self._post(client, body)
                resp.raise_for_status()
        except httpx.TimeoutException:
            raise McpToolError("timeout") from None
        except httpx.HTTPStatusError as exc:
            raise McpToolError(f"http_{exc.response.status_code}") from None
        except httpx.HTTPError:
            raise McpToolError("unreachable") from None
        envelope = last_sse_json(resp.text)
        if not envelope or "error" in envelope:
            raise McpToolError("protocol_error")
        result = envelope.get("result") or {}
        if result.get("isError"):
            raise McpToolError("tool_error")
        texts = [
            c["text"]
            for c in result.get("content") or []
            if isinstance(c, dict) and isinstance(c.get("text"), str)
        ]
        if not texts:
            return result
        text = "\n".join(texts)
        if len(text) > self._max:
            return text[: self._max] + "\n[truncated]"
        try:
            return json.loads(text)
        except ValueError:
            return text
