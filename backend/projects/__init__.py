"""Shared project context store (NS-ADR-104)."""

from backend.projects.service import ProjectService, get_service

__all__ = ["ProjectService", "get_service"]
