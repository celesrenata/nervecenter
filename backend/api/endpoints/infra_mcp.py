"""Mount point for the infra MCP endpoint used by the central opencode (NS-ADR-120)."""

from backend.infra.mcp_server import router

__all__ = ["router"]
