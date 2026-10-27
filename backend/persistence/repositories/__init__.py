"""Repository layer for the durable personal plane (workstream F).

This package is the single seam the later workstreams build on:

- :class:`RepositoryRegistry` — the single object later workstreams obtain
  repositories from. It is constructed from a session factory and does not
  edit app wiring beyond what F owns (see
  :mod:`backend.persistence.repositories.registry`).
- Per-plane repositories, one module each:
  - :mod:`~backend.persistence.repositories.connections_repo` — connections,
    and secrets with an injectable encrypt/decrypt hook (the actual Fernet
    codec lands in workstream B's vault).
  - :mod:`~backend.persistence.repositories.communications_repo` — people,
    identities, communications, conversations.
  - :mod:`~backend.persistence.repositories.commitments_repo` — commitments
    and followups.
  - :mod:`~backend.persistence.repositories.attention_repo` — attention
    items, snoozes, and the notification log (with the
    ``already_notified`` dedup helper).
  - :mod:`~backend.persistence.repositories.import_repo` — ingest cursors and
    import jobs.

All repositories are async and session-scoped: each method opens a session
inside a transaction, committing on success and rolling back on error — the
same transactional semantics as
:func:`backend.persistence.connection.session_scope`. Methods are small and
return domain rows (ORM instances) so workstreams can shape results.
"""

from __future__ import annotations

from .registry import RepositoryRegistry

__all__ = ["RepositoryRegistry"]
