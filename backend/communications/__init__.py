"""Unified communications plane (prompt2 §5–§7, §32) — package exports.

The canonical personal graph: :mod:`backend.communications.models` holds the
``Person``/``ContactIdentity``/``Communication``/``Conversation`` domain
models and :mod:`backend.communications.registry` the in-memory singleton
service (idempotent identity upsert, conservative value-first resolution,
explicit merge, cross-channel conversation folding).
"""

from backend.communications.models import (
    DEFAULT_CONTENT_ROUTING,
    Communication,
    CommunicationChannel,
    CommunicationDirection,
    ContactIdentity,
    ContactIdentityKind,
    Conversation,
    IdentityMatchStatus,
    Person,
    SensitivityLabel,
)
from backend.communications.registry import (
    CommunicationsService,
    get_communications_service,
    normalize_identity_value,
    reset_communications_service,
)

__all__ = [
    "DEFAULT_CONTENT_ROUTING",
    "Communication",
    "CommunicationChannel",
    "CommunicationDirection",
    "CommunicationsService",
    "ContactIdentity",
    "ContactIdentityKind",
    "Conversation",
    "IdentityMatchStatus",
    "Person",
    "SensitivityLabel",
    "get_communications_service",
    "normalize_identity_value",
    "reset_communications_service",
]
