"""Read-only OmniRoute Skill Hub client (NS-ADR-099).

Talks to the streamable-HTTP MCP endpoint (initialize, then ``tools/call``)
and uses only the read-only catalog tools. The key comes from
``NERVE_SKILLHUB_KEY`` when set, else NerveCenter's existing OmniRoute key
``NERVE_OMNIROUTE_API_KEY`` (owner decision: one key; it needs the scopes
``read:skills`` + ``read:catalog``), and is never logged. Without a key
every call returns nothing, so agent runs degrade honestly to "no skills".
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

DEFAULT_URL = "https://omniroute.celestium.life/api/mcp/stream"
KEY_ENV = "NERVE_SKILLHUB_KEY"
FALLBACK_KEY_ENV = "NERVE_OMNIROUTE_API_KEY"
URL_ENV = "NERVE_SKILLHUB_URL"

READ_ONLY_TOOLS = frozenset(
    {
        "omniroute_skills_list",
        "omniroute_skills_executions",
        "omniroute_agent_skills_list",
        "omniroute_agent_skills_get",
        "omniroute_agent_skills_coverage",
        "omniroute_github_skills_search",
        "omniroute_github_skills_scan",
    }
)


def _last_sse_json(text: str) -> dict[str, Any] | None:
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


class SkillHubClient:
    """Minimal MCP client for the Skill Hub read-only tools."""

    def __init__(
        self,
        *,
        key: str | None = None,
        url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        if key is None:
            key = os.environ.get(KEY_ENV, "").strip() or os.environ.get(FALLBACK_KEY_ENV, "")
        self._key = key.strip() or None
        self._url = (url or os.environ.get(URL_ENV, "") or DEFAULT_URL).strip()
        self._transport = transport
        self._timeout = timeout_s
        self._session_id: str | None = None
        self._catalog: list[dict[str, Any]] | None = None

    @property
    def configured(self) -> bool:
        return self._key is not None

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    async def _post(self, client: httpx.AsyncClient, body: dict[str, Any]) -> httpx.Response:
        return await client.post(self._url, json=body, headers=self._headers())

    async def _initialize(self, client: httpx.AsyncClient) -> None:
        resp = await self._post(
            client,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "nervcenter-runner", "version": "1"},
                },
            },
        )
        resp.raise_for_status()
        self._session_id = resp.headers.get("mcp-session-id")

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call one read-only tool; returns the parsed result or ``None``."""
        if not self.configured or name not in READ_ONLY_TOOLS:
            return None
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
                if self._session_id is None:
                    await self._initialize(client)
                resp = await self._post(client, body)
                if resp.status_code in (400, 404):
                    # Session invalidated by a rollout: re-initialize once.
                    self._session_id = None
                    await self._initialize(client)
                    resp = await self._post(client, body)
                resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - degrade honestly, never raise
            log.warning("skillhub.call_failed", tool=name, error=type(exc).__name__)
            return None
        envelope = _last_sse_json(resp.text)
        if not envelope or "error" in envelope:
            return None
        result = envelope.get("result") or {}
        if result.get("isError"):
            return None
        content = result.get("content") or []
        if content and isinstance(content[0], dict) and "text" in content[0]:
            try:
                return json.loads(content[0]["text"])
            except (ValueError, TypeError):
                return content[0]["text"]
        return result

    async def catalog(self) -> list[dict[str, Any]]:
        """The live agent skill catalog (cached after the first success)."""
        if self._catalog is not None:
            return self._catalog
        data = await self.call_tool("omniroute_agent_skills_list")
        if isinstance(data, dict):
            data = data.get("skills") or data.get("data")
        if isinstance(data, list):
            self._catalog = [s for s in data if isinstance(s, dict)]
            return self._catalog
        return []

    async def skills_context(self, text: str, *, limit: int = 3) -> str:
        """A short prompt block listing catalog skills relevant to ``text``."""
        if not self.configured:
            return ""
        words = {w for w in re.findall(r"[a-z]{4,}", text.lower())}
        if not words:
            return ""
        scored: list[tuple[int, dict[str, Any]]] = []
        for skill in await self.catalog():
            haystack = f"{skill.get('name', '')} {skill.get('description', '')}".lower()
            score = sum(1 for w in words if w in haystack)
            if score:
                scored.append((score, skill))
        scored.sort(key=lambda pair: -pair[0])
        lines = [
            f"- {s.get('id', s.get('name', '?'))}: {str(s.get('description', ''))[:160]}"
            for _, s in scored[:limit]
        ]
        if not lines:
            return ""
        return "Skills available from the Skill Hub:\n" + "\n".join(lines)
