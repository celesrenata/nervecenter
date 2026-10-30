"""Unified obligation plane (P2-A — Personal Reliability).

Public API:

- :class:`backend.obligations.service.ObligationsService` — orchestration
  over the durable obligation plane.
- :class:`backend.obligations.service.ObligationsTodayProvider` — Today
  section contributor (``needs_you`` / ``coming_up`` / ``handled``).
- :func:`backend.obligations.service.dedup_key_for` — the frozen dedup-key
  builder (contract §3).
- :func:`backend.obligations.service.register_obligations_today_provider` —
  app-startup registration.
"""

from backend.obligations.service import (
    ObligationsService,
    ObligationsTodayProvider,
    dedup_key_for,
    register_obligations_today_provider,
)

__all__ = [
    "ObligationsService",
    "ObligationsTodayProvider",
    "dedup_key_for",
    "register_obligations_today_provider",
]
