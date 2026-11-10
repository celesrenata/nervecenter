"""Versioned escalation policy for NerveShield Task 4.5 (NS-ADR-031).

A pure, deterministic function over typed summaries that decides which stage runs
next and what the disposition is so far:

    deterministic inspections + PDP -> Arc classifier -> Granite -> optional Qwen

The module performs no I/O, holds no state, imports no model client and sees no
mail text: inputs are enums, counts and a raw (uncalibrated) Arc score. A caller
runs the stage named in ``next_stage``, records the outcome in the inputs and calls
:func:`decide_escalation` again until ``next_stage`` is ``None``.

Invariants (each has table-driven tests):

- A later stage never lowers a disposition. If the deterministic/PDP floor is not
  ``SAFE_TO_PROCESS`` or ``LOW_PRIORITY`` the result is that floor, whatever any
  model says, and no model call is requested. Models can only raise a SAFE/LOW
  floor to ``REVIEW`` or ``INCOMPLETE``; they can never impose ``RESTRICTED``.
- Model failure, timeout, outage, rate limit, denied route or exhausted budget when
  escalation was triggered fails closed to ``REVIEW`` (high-impact request or a high
  Arc score) or ``INCOMPLETE``. It is never an allow.
- Qwen is optional: a Qwen stage that is disabled, not run, unavailable, failed,
  denied, rate-limited or over budget yields exactly the result computed without it.
- Escalation happens only on stated triggers (see :class:`EscalationReason`).
- Budgets: ``max_model_calls`` per message (Granite plus Qwen attempts), an optional
  rolling-window allowance, and ``max_evidence_chars`` for what may be sent.
- Every decision carries ``ESCALATION_POLICY_VERSION`` in :meth:`log_fields`.

Arc scores are RAW and uncalibrated (R-007.2); the band edges are unmeasured
defaults and a high or low score is never a probability.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum

from backend.mail_security.models import Disposition

ESCALATION_POLICY_VERSION = "mail-escalation-policy@1"

_RANK: dict[Disposition, int] = {
    Disposition.SAFE_TO_PROCESS: 0,
    Disposition.LOW_PRIORITY: 1,
    Disposition.INCOMPLETE: 2,
    Disposition.REVIEW: 3,
    Disposition.RESTRICTED: 4,
}
_MODEL_RAISABLE = frozenset({Disposition.SAFE_TO_PROCESS, Disposition.LOW_PRIORITY})


class Stage(StrEnum):
    ARC = "ARC"
    GRANITE = "GRANITE"
    QWEN = "QWEN"


class StageStatus(StrEnum):
    NOT_RUN = "NOT_RUN"
    COMPLETED = "COMPLETED"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"
    DENIED = "DENIED"
    RATE_LIMITED = "RATE_LIMITED"


class HighImpactFlag(StrEnum):
    """Requests whose consequence is high regardless of how benign the mail looks."""

    CREDENTIAL_REQUEST = "CREDENTIAL_REQUEST"
    PAYMENT_REQUEST = "PAYMENT_REQUEST"
    ACCOUNT_CHANGE_REQUEST = "ACCOUNT_CHANGE_REQUEST"
    EXECUTABLE_ATTACHMENT = "EXECUTABLE_ATTACHMENT"
    DATA_EXPORT_REQUEST = "DATA_EXPORT_REQUEST"


class GraniteOpinion(StrEnum):
    """Caller-mapped summary of a validated Granite observation."""

    CONSISTENT = "CONSISTENT"
    CONCERN = "CONCERN"
    UNCLEAR = "UNCLEAR"


class QwenOpinion(StrEnum):
    AGREE = "AGREE"
    DISAGREE = "DISAGREE"
    INSUFFICIENT = "INSUFFICIENT"


class EscalationReason(StrEnum):
    INPUT_INVALID = "INPUT_INVALID"
    FLOOR_ALREADY_CAUTIOUS = "FLOOR_ALREADY_CAUTIOUS"
    DETERMINISTIC_INCOMPLETE = "DETERMINISTIC_INCOMPLETE"
    NO_TRIGGER = "NO_TRIGGER"
    # triggers
    HIGH_IMPACT_REQUEST = "HIGH_IMPACT_REQUEST"
    ARC_HIGH_SCORE = "ARC_HIGH_SCORE"
    ARC_UNCERTAIN_BAND = "ARC_UNCERTAIN_BAND"
    ARC_UNAVAILABLE = "ARC_UNAVAILABLE"
    # stage scheduling
    ARC_STAGE_DUE = "ARC_STAGE_DUE"
    GRANITE_STAGE_DUE = "GRANITE_STAGE_DUE"
    QWEN_STAGE_DUE = "QWEN_STAGE_DUE"
    # fail-closed blockers
    GRANITE_DISABLED = "GRANITE_DISABLED"
    GRANITE_FAILED = "GRANITE_FAILED"
    EVIDENCE_OVER_BUDGET = "EVIDENCE_OVER_BUDGET"
    MODEL_BUDGET_EXHAUSTED = "MODEL_BUDGET_EXHAUSTED"
    RATE_BUDGET_EXHAUSTED = "RATE_BUDGET_EXHAUSTED"
    # model contributions
    GRANITE_CONSISTENT = "GRANITE_CONSISTENT"
    GRANITE_CONCERN = "GRANITE_CONCERN"
    GRANITE_UNCLEAR = "GRANITE_UNCLEAR"
    CONFLICTING_SIGNALS = "CONFLICTING_SIGNALS"
    QWEN_NOT_NEEDED = "QWEN_NOT_NEEDED"
    QWEN_DISABLED = "QWEN_DISABLED"
    QWEN_SKIPPED_BUDGET = "QWEN_SKIPPED_BUDGET"
    QWEN_NO_CONTRIBUTION = "QWEN_NO_CONTRIBUTION"
    QWEN_AGREE = "QWEN_AGREE"
    QWEN_DISAGREE = "QWEN_DISAGREE"
    QWEN_INSUFFICIENT = "QWEN_INSUFFICIENT"


@dataclass(frozen=True)
class EscalationConfig:
    """Tunable, versioned-with-the-policy knobs. Defaults are unmeasured."""

    arc_enabled: bool = True
    granite_enabled: bool = True
    qwen_enabled: bool = False
    arc_low: float = 0.2
    arc_high: float = 0.8
    max_model_calls: int = 4
    max_evidence_chars: int = 8192

    def __post_init__(self) -> None:
        for name in ("arc_low", "arc_high"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number")
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be within [0, 1]")
        if self.arc_low >= self.arc_high:
            raise ValueError("arc_low must be below arc_high")
        for name in ("max_model_calls", "max_evidence_chars"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative int")


DEFAULT_CONFIG = EscalationConfig()


@dataclass(frozen=True)
class DeterministicSummary:
    """Phase 1-3 inspections plus the policy decision point (PDP) disposition."""

    floor: Disposition
    inspection_complete: bool = True
    high_impact: frozenset[HighImpactFlag] = field(default_factory=frozenset)


@dataclass(frozen=True)
class ArcOutcome:
    status: StageStatus = StageStatus.NOT_RUN
    score: float | None = None  # raw, uncalibrated


@dataclass(frozen=True)
class GraniteOutcome:
    status: StageStatus = StageStatus.NOT_RUN
    opinion: GraniteOpinion | None = None
    attempts: int = 0


@dataclass(frozen=True)
class QwenOutcome:
    status: StageStatus = StageStatus.NOT_RUN
    opinion: QwenOpinion | None = None
    attempts: int = 0


@dataclass(frozen=True)
class EscalationInputs:
    deterministic: DeterministicSummary
    arc: ArcOutcome = field(default_factory=ArcOutcome)
    granite: GraniteOutcome = field(default_factory=GraniteOutcome)
    qwen: QwenOutcome = field(default_factory=QwenOutcome)
    evidence_chars: int = 0
    window_calls_remaining: int | None = None  # None = no rolling-window limit


@dataclass(frozen=True)
class EscalationDecision:
    """Disposition so far and, if not final, the single stage to run next."""

    disposition: Disposition
    next_stage: Stage | None
    reasons: tuple[EscalationReason, ...]
    model_calls_used: int = 0
    policy_version: str = ESCALATION_POLICY_VERSION

    @property
    def final(self) -> bool:
        return self.next_stage is None

    def log_fields(self) -> dict[str, object]:
        """Flat enum/number fields only; no content, scores or free text."""
        return {
            "escalation_policy_version": self.policy_version,
            "escalation_disposition": self.disposition.value,
            "escalation_next_stage": self.next_stage.value if self.next_stage else "NONE",
            "escalation_reasons": ",".join(r.value for r in self.reasons),
            "escalation_model_calls": self.model_calls_used,
        }


_FAILURE_STATUSES = frozenset(
    {
        StageStatus.UNAVAILABLE,
        StageStatus.FAILED,
        StageStatus.DENIED,
        StageStatus.RATE_LIMITED,
    }
)


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _inputs_valid(inputs: object) -> bool:
    if not isinstance(inputs, EscalationInputs):
        return False
    det = inputs.deterministic
    if not isinstance(det, DeterministicSummary):
        return False
    if not isinstance(det.floor, Disposition) or not isinstance(det.inspection_complete, bool):
        return False
    if not isinstance(det.high_impact, frozenset) or not all(
        isinstance(f, HighImpactFlag) for f in det.high_impact
    ):
        return False
    if not isinstance(inputs.arc, ArcOutcome) or not isinstance(inputs.arc.status, StageStatus):
        return False
    for outcome in (inputs.granite, inputs.qwen):
        if not isinstance(outcome, (GraniteOutcome, QwenOutcome)):
            return False
        if not isinstance(outcome.status, StageStatus) or not _is_count(outcome.attempts):
            return False
    if not _is_count(inputs.evidence_chars):
        return False
    window = inputs.window_calls_remaining
    return window is None or _is_count(window)


def _raise_to(current: Disposition, candidate: Disposition) -> Disposition:
    return candidate if _RANK[candidate] > _RANK[current] else current


def _fail_closed(triggers: list[EscalationReason]) -> Disposition:
    """Disposition when a triggered escalation cannot get a usable model answer."""
    if (
        EscalationReason.HIGH_IMPACT_REQUEST in triggers
        or EscalationReason.ARC_HIGH_SCORE in triggers
    ):
        return Disposition.REVIEW
    return Disposition.INCOMPLETE


def _call_blocker(
    inputs: EscalationInputs, config: EscalationConfig, calls_used: int
) -> EscalationReason | None:
    if inputs.evidence_chars > config.max_evidence_chars:
        return EscalationReason.EVIDENCE_OVER_BUDGET
    if calls_used >= config.max_model_calls:
        return EscalationReason.MODEL_BUDGET_EXHAUSTED
    if inputs.window_calls_remaining is not None and inputs.window_calls_remaining < 1:
        return EscalationReason.RATE_BUDGET_EXHAUSTED
    return None


def _arc_triggers(arc: ArcOutcome, config: EscalationConfig) -> list[EscalationReason]:
    if arc.status in _FAILURE_STATUSES:
        return [EscalationReason.ARC_UNAVAILABLE]
    if arc.status is not StageStatus.COMPLETED:
        return []
    score = arc.score
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return [EscalationReason.ARC_UNAVAILABLE]
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        return [EscalationReason.ARC_UNAVAILABLE]
    if score >= config.arc_high:
        return [EscalationReason.ARC_HIGH_SCORE]
    if score > config.arc_low:
        return [EscalationReason.ARC_UNCERTAIN_BAND]
    return []


def decide_escalation(
    inputs: EscalationInputs, config: EscalationConfig = DEFAULT_CONFIG
) -> EscalationDecision:
    """Return the disposition so far and the next stage to run (``None`` when final)."""
    if not _inputs_valid(inputs) or not isinstance(config, EscalationConfig):
        return EscalationDecision(Disposition.INCOMPLETE, None, (EscalationReason.INPUT_INVALID,))

    floor = inputs.deterministic.floor
    calls = inputs.granite.attempts + inputs.qwen.attempts

    def done(disposition: Disposition, *reasons: EscalationReason) -> EscalationDecision:
        return EscalationDecision(disposition, None, reasons, calls)

    def run(
        stage: Stage, disposition: Disposition, *reasons: EscalationReason
    ) -> EscalationDecision:
        return EscalationDecision(disposition, stage, reasons, calls)

    if floor not in _MODEL_RAISABLE:
        return done(floor, EscalationReason.FLOOR_ALREADY_CAUTIOUS)
    if not inputs.deterministic.inspection_complete:
        return done(Disposition.INCOMPLETE, EscalationReason.DETERMINISTIC_INCOMPLETE)

    triggers: list[EscalationReason] = []
    if inputs.deterministic.high_impact:
        triggers.append(EscalationReason.HIGH_IMPACT_REQUEST)
    if inputs.arc.status is StageStatus.NOT_RUN and config.arc_enabled:
        return run(Stage.ARC, floor, EscalationReason.ARC_STAGE_DUE)
    triggers += _arc_triggers(inputs.arc, config)
    if not triggers:
        return done(floor, EscalationReason.NO_TRIGGER)

    current = floor
    if (
        EscalationReason.HIGH_IMPACT_REQUEST in triggers
        or EscalationReason.ARC_HIGH_SCORE in triggers
    ):
        current = _raise_to(current, Disposition.REVIEW)
    reasons = list(triggers)

    granite = inputs.granite
    if granite.status is StageStatus.NOT_RUN:
        blocker = (
            EscalationReason.GRANITE_DISABLED
            if not config.granite_enabled
            else _call_blocker(inputs, config, calls)
        )
        if blocker is not None:
            return done(_fail_closed(triggers), *reasons, blocker)
        return run(Stage.GRANITE, current, *reasons, EscalationReason.GRANITE_STAGE_DUE)
    opinion = granite.opinion
    if granite.status is not StageStatus.COMPLETED or not isinstance(opinion, GraniteOpinion):
        return done(_fail_closed(triggers), *reasons, EscalationReason.GRANITE_FAILED)
    if opinion is GraniteOpinion.CONCERN:
        current = _raise_to(current, Disposition.REVIEW)
        reasons.append(EscalationReason.GRANITE_CONCERN)
    elif opinion is GraniteOpinion.UNCLEAR:
        current = _raise_to(current, Disposition.INCOMPLETE)
        reasons.append(EscalationReason.GRANITE_UNCLEAR)
    else:
        reasons.append(EscalationReason.GRANITE_CONSISTENT)
        if EscalationReason.ARC_HIGH_SCORE in triggers:
            reasons.append(EscalationReason.CONFLICTING_SIGNALS)

    # Qwen is selective: only when it could still change the outcome.
    if _RANK[current] >= _RANK[Disposition.REVIEW]:
        return done(current, *reasons, EscalationReason.QWEN_NOT_NEEDED)
    qwen = inputs.qwen
    if qwen.status is StageStatus.NOT_RUN:
        if not config.qwen_enabled:
            return done(current, *reasons, EscalationReason.QWEN_DISABLED)
        if _call_blocker(inputs, config, calls) is not None:
            return done(current, *reasons, EscalationReason.QWEN_SKIPPED_BUDGET)
        return run(Stage.QWEN, current, *reasons, EscalationReason.QWEN_STAGE_DUE)
    if qwen.status is not StageStatus.COMPLETED or not isinstance(qwen.opinion, QwenOpinion):
        return done(current, *reasons, EscalationReason.QWEN_NO_CONTRIBUTION)
    if qwen.opinion is QwenOpinion.DISAGREE:
        return done(
            _raise_to(current, Disposition.REVIEW), *reasons, EscalationReason.QWEN_DISAGREE
        )
    if qwen.opinion is QwenOpinion.INSUFFICIENT:
        return done(
            _raise_to(current, Disposition.INCOMPLETE), *reasons, EscalationReason.QWEN_INSUFFICIENT
        )
    return done(current, *reasons, EscalationReason.QWEN_AGREE)


__all__ = [
    "DEFAULT_CONFIG",
    "ESCALATION_POLICY_VERSION",
    "ArcOutcome",
    "DeterministicSummary",
    "EscalationConfig",
    "EscalationDecision",
    "EscalationInputs",
    "EscalationReason",
    "GraniteOpinion",
    "GraniteOutcome",
    "HighImpactFlag",
    "QwenOpinion",
    "QwenOutcome",
    "Stage",
    "StageStatus",
    "decide_escalation",
]
