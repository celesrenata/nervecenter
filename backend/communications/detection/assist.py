"""Model-assist hook for detection (AC-R04 / R107 / R103 step 3).

An :class:`InferenceAssist` is an *optional refiner*: it may adjust the
confidence / model tag / explanation of an already-deterministically-detected
finding, but it can **never create a finding on its own**. The deterministic
extractors are the sole detectors; assist is off by default (AC-R14 policy
gate) and, when on, only refines.

The deterministic path is fully green with assist disabled, so
``detect_communication(...)`` with no assist yields identical output for
identical input — the §21 deterministic + explainable contract holds with
assist off, and ``model_used`` stays ``deterministic-extractor``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, Sequence

from .findings import (
    DETERMINISTIC_DETECTOR,
    Commitment,
    Deadline,
    DueInfo,
    Finding,
    FindingStatus,
    Request,
)


@dataclass(frozen=True)
class AssistContext:
    """Everything an assist implementation may inspect to refine a finding.

    Carries only metadata (never the raw plaintext content) plus the
    already-parsed due info, so an assist can't reach the payload through
    this surface.
    """

    finding_id: str
    kind: str
    created_from: str
    content_ref: str
    confidence: float
    due: datetime | None
    due_info: DueInfo | None
    matched: str
    why: tuple[str, ...]
    now: datetime


@dataclass(frozen=True)
class AssistRefinement:
    """A refinement an assist may propose for a single finding.

    All fields are optional; ``None`` means "keep the deterministic value".
    ``reason`` (when refining at all) is appended to ``why`` for
    explainability and the model is re-stamped as ``model_used``.
    """

    reason: str
    confidence: float | None = None
    model_used: str | None = None
    status: FindingStatus | None = None
    due: datetime | None = None


class InferenceAssist(Protocol):
    """Protocol for an assist implementation (never the sole detector).

    ``model_name`` is the deterministic name stamped into ``model_used`` on
    any finding this assist refines. Implementations must be deterministic:
    identical input → identical refinement (or ``None``).
    """

    @property
    def model_name(self) -> str: ...

    def refine(
        self,
        finding: Finding,
        context: AssistContext,
    ) -> AssistRefinement | None: ...


class DisabledAssist:
    """The default: assist is off, so nothing is ever refined.

    This is what :class:`DetectionService` uses when no assist is supplied
    (AC-R04: "off by default"). It is also the *only* assist the
    deterministic test-suite relies on, which is what makes the
    deterministic path fully green with assist disabled.
    """

    @property
    def enabled(self) -> bool:
        return False

    @property
    def model_name(self) -> str:
        return DETERMINISTIC_DETECTOR

    def refine(self, finding: Finding, context: AssistContext) -> None:  # type: ignore[override]
        return None


def assist_context_for(finding: Finding, *, now: datetime) -> AssistContext:
    """Build the metadata-only :class:`AssistContext` for a finding."""
    return AssistContext(
        finding_id=finding.finding_id,
        kind=finding.kind,
        created_from=finding.created_from,
        content_ref=finding.content_ref,
        confidence=finding.confidence,
        due=finding.due,
        due_info=finding.due_info,
        matched=finding.matched,
        why=tuple(finding.why),
        now=now,
    )


def apply_refinement(finding: Finding, ref: AssistRefinement) -> Finding:
    """Apply an :class:`AssistRefinement` to a finding, re-stamping model.

    ``model_used`` is set to the refinement's model tag (falling back to the
    deterministic tag only if the refiner did not name one — which a real
    assist always does). The refine ``reason`` is appended to ``why``.
    """
    out = finding
    if ref is None:
        return out
    new_why = list(out.why)
    if ref.reason and ref.reason not in new_why:
        new_why.append(ref.reason)
    update: dict[str, object] = {"why": new_why}
    model_used = ref.model_used or out.model_used
    update["model_used"] = model_used
    if ref.confidence is not None:
        update["confidence"] = min(1.0, max(0.0, float(ref.confidence)))
    if ref.status is not None:
        update["status"] = ref.status
    if ref.due is not None:
        update["due"] = ref.due
    return out.model_copy(update=update)


__all__ = [
    "AssistContext",
    "AssistRefinement",
    "DisabledAssist",
    "InferenceAssist",
    "assist_context_for",
    "apply_refinement",
]
