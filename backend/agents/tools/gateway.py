"""The one place agents reach the Gremlin hub and the OmniRoute read-only tools (NS-ADR-113).

``ToolGateway.call`` enforces :mod:`backend.agents.tools.policy` first, then
talks to the hub in-cluster (``NERVE_MCP_HUB_URL``, Bearer
``NERVE_MCP_HUB_TOKEN``) or to the OmniRoute MCP stream with the privileged
key. Credentials are never logged or returned; the audit log carries the agent
id, server, tool and reason code only.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import structlog

from backend.agents.tools import policy
from backend.agents.tools.mcp_client import McpClient, McpToolError

log = structlog.get_logger(__name__)

HUB_URL_ENV = "NERVE_MCP_HUB_URL"
HUB_TOKEN_ENV = "NERVE_MCP_HUB_TOKEN"
OMNIROUTE_URL_ENV = "NERVE_OMNIROUTE_MCP_URL"
OMNIROUTE_KEY_ENV = "NERVE_OMNIROUTE_PRIVILEGED_KEY"
DEFAULT_HUB_URL = "http://mcp-hub.mcp-hub.svc:8080"
DEFAULT_OMNIROUTE_URL = "https://omniroute.celestium.life/api/mcp/stream"


class ToolGateway:
    def __init__(
        self,
        *,
        hub_url: str | None = None,
        hub_token: str | None = None,
        omniroute_url: str | None = None,
        omniroute_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 20.0,
    ) -> None:
        self._hub_url = (hub_url or os.environ.get(HUB_URL_ENV, "") or DEFAULT_HUB_URL).rstrip("/")
        self._hub_token = (
            hub_token if hub_token is not None else os.environ.get(HUB_TOKEN_ENV, "")
        ).strip() or None
        self._omni_url = (
            omniroute_url or os.environ.get(OMNIROUTE_URL_ENV, "") or DEFAULT_OMNIROUTE_URL
        )
        self._omni_key = (
            omniroute_key if omniroute_key is not None else os.environ.get(OMNIROUTE_KEY_ENV, "")
        ).strip() or None
        self._transport = transport
        self._timeout = timeout_s
        self._clients: dict[str, McpClient] = {}

    @property
    def hub_configured(self) -> bool:
        return self._hub_token is not None

    @property
    def omniroute_configured(self) -> bool:
        return self._omni_key is not None

    @property
    def configured(self) -> bool:
        return self.hub_configured or self.omniroute_configured

    def available(self, server: str) -> bool:
        return (
            self.omniroute_configured if server == policy.OMNIROUTE_SERVER else self.hub_configured
        )

    def describe(self) -> dict[str, Any]:
        """The allowlist with availability (no endpoints, no credentials)."""
        return {
            "servers": {
                server: {"tools": tools, "available": self.available(server)}
                for server, tools in policy.listing().items()
            },
            "memory_rule": "refused unless a call is marked non-sensitive by the owner API "
            "or code, the agent is not in a sensitive class, and arguments carry no personal data",
        }

    def _client(self, server: str) -> McpClient:
        client = self._clients.get(server)
        if client is None:
            if server == policy.OMNIROUTE_SERVER:
                client = McpClient(
                    self._omni_url,
                    token=self._omni_key,
                    transport=self._transport,
                    timeout_s=self._timeout,
                )
            else:
                client = McpClient(
                    f"{self._hub_url}/{server}",
                    token=self._hub_token,
                    transport=self._transport,
                    timeout_s=self._timeout,
                )
            self._clients[server] = client
        return client

    async def call(
        self,
        agent: Any,
        server: str,
        tool: str,
        args: dict[str, Any] | None = None,
        *,
        non_sensitive: bool = False,
    ) -> dict[str, Any]:
        """``{"ok": True, "result": ...}`` or ``{"ok": False, "denied_reason": code}``."""
        args = {} if args is None else args
        agent_id = str(getattr(agent, "id", "") or "-")
        reason = policy.check(agent, server, tool, args, non_sensitive=non_sensitive)
        if reason is None and not self.available(server):
            reason = "unconfigured"
        if reason is not None:
            log.info(
                "agent_tools.denied", agent_id=agent_id, server=server, tool=tool, reason=reason
            )
            return {"ok": False, "denied_reason": reason}
        try:
            result = await self._client(server).call_tool(tool, args)
        except McpToolError as exc:
            log.warning(
                "agent_tools.failed", agent_id=agent_id, server=server, tool=tool, reason=exc.reason
            )
            return {"ok": False, "denied_reason": exc.reason}
        log.info("agent_tools.call", agent_id=agent_id, server=server, tool=tool)
        return {"ok": True, "result": result}

    # -- back-off signal ------------------------------------------------------------------

    async def health_signal(self) -> bool | None:
        """Real OmniRoute health as a secondary back-off signal (``None`` = no signal)."""
        if not self.omniroute_configured:
            return None
        outcome = await self.call(None, policy.OMNIROUTE_SERVER, "omniroute_get_health")
        if not outcome["ok"] or not isinstance(outcome["result"], dict):
            return None
        data = outcome["result"]
        for key in ("healthy", "ok"):
            if isinstance(data.get(key), bool):
                return data[key]
        status = str(data.get("status", "")).lower()
        if status in {"healthy", "ok", "up"}:
            return True
        if status in {"down", "unhealthy"}:
            return False
        return None
