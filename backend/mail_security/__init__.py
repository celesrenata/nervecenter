"""NerveShield mail-security evidence/analysis plane (Phase 1).

Isolated package for the metadata-only mail security contract. Phase 1 Task
1.1 defines the strict versioned data models only — there is **no** runtime
wiring, API route, migration, event subject, network call, or I/O here, and no
existing runtime path reads :data:`NERVE_MAIL_SECURITY_ENABLED`.

The feature flag defaults OFF (``0``) and SHADOW mode per the NerveShield
steering contract; it is defined here for later phases but intentionally not
consumed by any existing code path yet.
"""

from __future__ import annotations

from backend.mail_security.models import (
    SCHEMA_VERSION,
    AcquisitionMode,
    AssessmentMode,
    Authentication,
    AuthObservation,
    AuthProvenance,
    AuthResult,
    AxisScore,
    ClaimedIdentity,
    ClaimedRole,
    Code,
    Completeness,
    Component,
    ComponentRiskLevel,
    Contradiction,
    Disposition,
    EvidenceFidelity,
    FindingCategory,
    FindingSeverity,
    FindingStatus,
    IdentityConsistency,
    MessageCategory,
    MessageEvidenceManifest,
    ModelRun,
    ModelRunStatus,
    ObservationRequest,
    ObserverRole,
    OpaqueRef,
    PolicyDisposition,
    ProcessingState,
    RequestType,
    RequestUrgency,
    RiskIndicator,
    ScoreSemantics,
    SecurityAssessment,
    SecurityFinding,
    SemanticObservation,
    TrustAxes,
)

#: Default-off feature flag for the NerveShield mail-security plane. Defined
#: here for later phases; NOT read by any existing runtime path (Phase 1).
NERVE_MAIL_SECURITY_ENABLED = 0

__all__ = [
    "NERVE_MAIL_SECURITY_ENABLED",
    "SCHEMA_VERSION",
    "AcquisitionMode",
    "AssessmentMode",
    "AuthObservation",
    "AuthProvenance",
    "AuthResult",
    "Authentication",
    "AxisScore",
    "ClaimedIdentity",
    "ClaimedRole",
    "Code",
    "Completeness",
    "Component",
    "ComponentRiskLevel",
    "Contradiction",
    "Disposition",
    "EvidenceFidelity",
    "FindingCategory",
    "FindingSeverity",
    "FindingStatus",
    "IdentityConsistency",
    "MessageCategory",
    "MessageEvidenceManifest",
    "ModelRun",
    "ModelRunStatus",
    "ObservationRequest",
    "ObserverRole",
    "OpaqueRef",
    "PolicyDisposition",
    "ProcessingState",
    "RequestType",
    "RequestUrgency",
    "RiskIndicator",
    "ScoreSemantics",
    "SecurityAssessment",
    "SecurityFinding",
    "SemanticObservation",
    "TrustAxes",
]
