"""Policy: guardrails, permissions, and redaction.

This package implements policy enforcement for inference requests:
- Guardrails: Input/output validation, redaction, and constraint enforcement
- Permissions: Access control and capability-based authorization

Modules
-------
- ``guardrails`` — InferenceGuardrails for policy constraints
- ``permissions`` — PermissionEnforcer for access control
"""

from .guardrails import GuardConfig, InferenceGuardrails, PolicyGuard
from .pdp import (
    INFER_ROUTE_ACTION,
    SENDING_CAPABILITY,
    InferRouteDecision,
    PolicyDecisionPoint,
)
from .permissions import PermissionCheck, PermissionEnforcer, PermissionLevel
from .sensitivity import (
    ModelTier,
    RoutingDecision,
    SensitivityClass,
    SensitivityClassification,
    classify_sensitivity,
)

__all__ = [
    "GuardConfig",
    "InferenceGuardrails",
    "PolicyGuard",
    "PermissionCheck",
    "PermissionEnforcer",
    "PermissionLevel",
    "INFER_ROUTE_ACTION",
    "InferRouteDecision",
    "PolicyDecisionPoint",
    "SENDING_CAPABILITY",
    "ModelTier",
    "RoutingDecision",
    "SensitivityClass",
    "SensitivityClassification",
    "classify_sensitivity",
]
