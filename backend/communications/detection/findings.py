"""Typed findings for commitment / request / deadline detection (R103, AC-R04).

Implements the prompt2 §16–§18 finding shapes with the AC-R04 required fields
on every finding:

- ``created_from`` — the source communication id the finding was extracted
  from (``created_from`` in the §16 example; equals the
  :class:`~backend.communications.models.Communication.communication_id`).
- ``confidence``   — deterministic confidence in [0, 1].
- ``status``       — lifecycle state (:class:`FindingStatus`); findings start
  ``OPEN`` and flip on the overdue sweep (commitments → ``OVERDUE``,
  deadlines → ``PASSING``).
- ``due``          — the parsed due datetime, or ``None`` for open-ended
  findings ("I'll take care of it." carries no time reference).
- ``why[]``        — non-empty list of human-readable explanations
  (prompt2 §22: every item must answer "why are you showing me this?").
- ``model_used``   — deterministic detector id (``DETERMINISTIC_DETECTOR``)
  by default; set to the assist model name only when an
  :class:`~backend.communications.detection.assist.InferenceAssist` refinement
  was applied. Identical input → identical ``model_used`` (deterministic).

**No-plaintext policy (§7 / R107):** findings carry only the
``communication_id`` (``created_from``), the ``content_ref`` pointer, and
short structural metadata (kind, status, confidence, due, a quoted matched
expression capped at 80 characters, ``why``, ``model_used``). The message
body itself is never stored on a finding — the raw payload stays encrypted in
the source plane, outside the knowledge graph.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Deterministic detector provenance stamped on every finding produced by the
#: pure extractors (used as ``model_used`` while no model assist ran).
DETERMINISTIC_DETECTOR = "deterministic-extractor"

FindingKind = Literal["commitment", "request", "deadline"]


class FindingStatus(StrEnum):
    """Lifecycle states for detected findings (AC-R04 ``status`` field).

    ``OPEN`` — freshly detected, not yet overdue.
    ``OVERDUE`` — a commitment whose due time has passed (→ §8
    ``commitment.overdue``).
    ``PASSING`` — a deadline whose due time has passed (→ §8
    ``deadline.passed``).
    """

    OPEN = "OPEN"
    OVERDUE = "OVERDUE"
    PASSING = "PASSING"


class DueInfo(BaseModel):
    """A parsed deadline expression (shared deterministic date parsing, R103).

    ``due`` is always an aware UTC datetime; ``expr`` is the short
    human-readable expression that produced it (the "why" of the due date);
    ``time_stated`` distinguishes an explicitly stated time ("by 5 pm",
    "at 3 pm", "noon") from a day-level default (weekday → end of business
    day; date → end of day; "next week" → Monday business-open);
    ``confidence`` is the parser's confidence in the read.
    """

    model_config = ConfigDict(frozen=True)

    due: datetime
    expr: str
    time_stated: bool = False
    confidence: float = Field(ge=0.0, le=1.0)


class Finding(BaseModel):
    """Base shape shared by the three typed findings (AC-R04 field set).

    ``finding_id`` is deterministic (``<kind>-<communication_id>``) so the
    same communication always yields the same finding id — replay-safe and
    stable for API lookups / event subjects.
    """

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    finding_id: str
    kind: FindingKind
    created_from: str = Field(
        description="Source Communication id (AC-R04 `created_from`)."
    )
    content_ref: str = Field(
        description=(
            "Pointer to the (encrypted, out-of-graph) raw payload — a pointer, "
            "never the plaintext content (§7)."
        )
    )
    status: FindingStatus = FindingStatus.OPEN
    confidence: float = Field(ge=0.0, le=1.0)
    model_used: str = DETERMINISTIC_DETECTOR
    why: list[str] = Field(min_length=1, description="Non-empty §22 explanations.")
    due: datetime | None = None
    due_info: DueInfo | None = None
    matched: str = Field(
        default="",
        description="Short quoted expression that triggered the finding (≤80 chars).",
    )

    @property
    def communication_id(self) -> str:
        """Alias of ``created_from`` — the finding's source communication."""
        return self.created_from

    def with_reason(
        self,
        reason: str,
        *,
        model_used: str | None = None,
        confidence: float | None = None,
    ) -> "Finding":
        """Return a copy with ``reason`` appended to ``why``.

        Used by the assist refinement path (AC-R04: assist may *refine* a
        deterministic finding, never invent one). Also re-stamps
        ``model_used`` and clamps ``confidence`` when the assist provides
        them.
        """
        reason = str(reason or "").strip()
        if not reason:
            raise ValueError("assist reason must be non-empty (explainability)")
        new_why = list(self.why)
        if reason not in new_why:
            new_why.append(reason)
        updates: dict[str, object] = {"why": new_why}
        if model_used:
            updates["model_used"] = model_used
        if confidence is not None:
            updates["confidence"] = min(1.0, max(0.0, float(confidence)))
        return self.model_copy(update=updates)


class Commitment(Finding):
    """A commitment the user made (prompt2 §16)."""

    kind: Literal["commitment"] = "commitment"


class Request(Finding):
    """A request someone made of the user (prompt2 §17)."""

    kind: Literal["request"] = "request"


class Deadline(Finding):
    """A deadline imposed on a task or action (prompt2 §18).

    Deadlines always carry a parsed due date — a ``Deadline`` without a due
    is rejected by validation.
    """

    kind: Literal["deadline"] = "deadline"

    @model_validator(mode="after")
    def _deadline_must_have_due(self) -> "Deadline":
        if self.due is None:
            raise ValueError("a Deadline finding must carry a parsed due date")
        return self


class DetectionReport(BaseModel):
    """Everything produced for one communication in one detection run.

    ``findings`` is empty when the communication was suppressed by the
    noise gate (marketing/newsletter/social) — the suppression reasons are
    recorded in ``suppressed`` so the decision stays auditable without
    storing plaintext.
    """

    model_config = ConfigDict(frozen=True)

    communication_id: str
    content_ref: str
    findings: tuple[Commitment | Request | Deadline, ...] = ()
    events: tuple = ()
    attention_ids: dict[str, str] = Field(default_factory=dict)
    suppressed: tuple[str, ...] = ()
    model_used: str = DETERMINISTIC_DETECTOR
    detected_at: datetime

    @property
    def all_findings(self) -> tuple[Commitment | Request | Deadline, ...]:
        """All findings in canonical order (commitments, requests, deadlines)."""
        out: list[Commitment | Request | Deadline] = []
        out.extend(f for f in self.findings if isinstance(f, Commitment))
        out.extend(f for f in self.findings if isinstance(f, Request))
        out.extend(f for f in self.findings if isinstance(f, Deadline))
        return tuple(out)

    def by_id(self, finding_id: str) -> Commitment | Request | Deadline | None:
        for f in self.findings:
            if f.finding_id == finding_id:
                return f
        return None


__all__ = [
    "DETERMINISTIC_DETECTOR",
    "DueInfo",
    "Finding",
    "FindingStatus",
    "Commitment",
    "Request",
    "Deadline",
    "DetectionReport",
]
