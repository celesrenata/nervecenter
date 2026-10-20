"""Personal Operations Plane — Attention Engine (prompt2 Phase P0).

P0: the in-memory, deterministic, explainable attention queue.
AC-R15 (R108): the local, user-controlled attention policy layer — daily
budget with bundling (§58), quiet periods (§59), deadline escalation
(§28), and adaptive escalation (§29) — in :mod:`backend.attention.policy`.
"""

# NOTE: the ``service`` import must come first (not alphabetical):
# ``backend.attention.policy`` imports ``backend.api.models``, whose package
# re-enters ``backend.attention``; importing the service first binds the
# partial module's names before the re-entrant import runs.
from backend.attention.service import (  # noqa: I001
    AttentionService,
    get_attention_service,
    reset_attention_service,
)
from backend.attention.policy import (  # noqa: I001
    AdaptiveDecision,
    ActionRecord,
    AttentionAudit,
    AttentionBudget,
    AttentionPolicyService,
    BudgetDecision,
    BudgetEntry,
    EscalationInfo,
    EscalationMode,
    EscalationPreference,
    EscalationRecord,
    NotificationState,
    QuietPeriod,
    QuietPeriodKind,
    get_attention_policy,
    reset_attention_policy,
)

__all__ = [
    "ActionRecord",
    "AdaptiveDecision",
    "AttentionAudit",
    "AttentionBudget",
    "AttentionPolicyService",
    "AttentionService",
    "BudgetDecision",
    "BudgetEntry",
    "EscalationInfo",
    "EscalationMode",
    "EscalationPreference",
    "EscalationRecord",
    "NotificationState",
    "QuietPeriod",
    "QuietPeriodKind",
    "get_attention_policy",
    "get_attention_service",
    "reset_attention_policy",
    "reset_attention_service",
]
