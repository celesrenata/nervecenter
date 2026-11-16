"""Coding handoffs to central opencode or the owner's Mac (NS-ADR-106)."""

from backend.handoffs.service import HandoffService, get_service, route

__all__ = ["HandoffService", "get_service", "route"]
