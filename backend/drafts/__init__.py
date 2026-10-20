"""Drafts plane (gaps1 G-1.4 — R105, prompt2 §40–§41) — package exports.

Auditable, human-controlled drafts: the deterministic rule-based drafter is
the required fallback, the model-assisted drafter is an injectable interface
gated by the R107 inference policy, and the only lifecycle transition this
plane may perform is ``DRAFT -> DISCARDED`` (sending stays human-gated).
"""

from backend.drafts.drafter import (
    DeterministicDrafter,
    DraftContext,
    ModelAssistedDrafter,
)
from backend.drafts.models import Draft, DraftAction, DraftStatus
from backend.drafts.service import (
    DraftNotFound,
    DraftPolicyDenied,
    DraftsService,
    InvalidDraftTransition,
    default_drafter_principal,
    get_drafts_service,
    reset_drafts_service,
)

__all__ = [
    "DeterministicDrafter",
    "Draft",
    "DraftAction",
    "DraftContext",
    "DraftNotFound",
    "DraftPolicyDenied",
    "DraftsService",
    "DraftStatus",
    "InvalidDraftTransition",
    "ModelAssistedDrafter",
    "default_drafter_principal",
    "get_drafts_service",
    "reset_drafts_service",
]
