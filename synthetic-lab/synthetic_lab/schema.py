"""Versioned corpus schema for the T003-05 synthetic lab (spec T003.AC10).

The corpus is fixture-only and deterministic:

- ``anchor`` — a fixed clock (ISO-8601 UTC) that every replay date-math
  call is pinned to;
- ``seed``   — recorded so the corpus is reproducible; all inputs are
  literals (no randomness is drawn);
- every case carries explicit expectations. The evaluator compares
  replayed observations against them and reports pass/fail with
  evidence (per-case expected/observed deltas plus aggregate
  precision/recall over the detection plane).

Ask cases are reported ``PENDING`` (never pass, never fail) while the
T003.AC9 ask pipeline and its recorded-fixture interface are absent —
no results are invented. See :mod:`synthetic_lab.replay` for the
interface contract the AC9 work must expose to un-pend the cases.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

#: Corpus schema version (bump when the case/expect shape changes).
CORPUS_SCHEMA_VERSION = 1

#: The deterministic detector id pinned by the corpus (R103 / AC-R04).
DETERMINISTIC_DETECTOR = "deterministic-extractor"


# -- detection ----------------------------------------------------------------


class ExpectDetection(BaseModel):
    """Expected detection-plane observation for one case.

    All fields are optional *except* ``kinds`` — an empty list asserts
    that nothing is detected (the precision cases).
    """

    model_config = ConfigDict(extra="forbid")

    kinds: list[str] = Field(
        description="Finding kinds expected, order-insensitive (sorted-compared)."
    )
    finding_ids: list[str] | None = None
    due: dict[str, str | None] = Field(
        default_factory=dict,
        description="Expected due ISO string per finding kind (null = no due).",
    )
    model_used: str = DETERMINISTIC_DETECTOR
    status: list[str] | None = Field(
        default=None, description="Expected statuses, order-insensitive."
    )
    confidence: dict[str, float] | None = Field(
        default=None, description="Expected confidence per finding kind."
    )
    events: list[str] | None = Field(
        default=None, description="Expected §8 event types, order-insensitive."
    )
    why_contains: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Per finding kind: substrings that must appear (case-insensitive).",
    )
    suppressed: bool = False
    suppression_contains: str | None = None


class DetectionCase(BaseModel):
    """One labeled detection case (commitment / request / deadline / noise)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    plane: Literal["detection"]
    title: str
    direction: Literal["INCOMING", "OUTGOING"]
    channel: str = "sms"
    sender: str = "counterpart"
    text: str
    subject: str | None = None
    expect: ExpectDetection


# -- attention ------------------------------------------------------------------


class AttentionItemSpec(BaseModel):
    """A fixture AttentionItem (all fields literal; clock from the anchor)."""

    model_config = ConfigDict(extra="forbid")

    attention_id: str
    status: str
    priority: float = Field(ge=0.0, le=1.0)
    urgency: float = Field(ge=0.0, le=1.0)
    due: str | None = None
    entities: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    why: list[str] = Field(default_factory=list)


class ExpectAttention(BaseModel):
    """Expected attention-policy observation.

    Fields map 1:1 to observation keys per operation:

    - ``budget_decision``: ``individual`` / ``bundled`` / ``urgent`` /
      ``bundle_count`` / ``reasons_contains``
    - ``notification_flags``: ``notify`` / ``quiet`` / ``status``
    - ``deadline_escalation``: ``promoted`` / ``not_promoted`` /
      ``status_after`` / ``reason_contains``
    - ``adaptive_escalation``: ``mode`` / ``preference`` / ``reason_contains``

    ``None`` means "not asserted for this case".
    """

    model_config = ConfigDict(extra="forbid")

    individual: list[str] | None = None
    bundled: list[str] | None = None
    urgent: list[str] | None = None
    bundle_count: int | None = None
    notify: dict[str, bool] | None = None
    quiet: dict[str, str | None] | None = None
    status: dict[str, str] | None = None
    promoted: list[str] | None = None
    not_promoted: list[str] | None = None
    status_after: dict[str, str] | None = None
    mode: dict[str, str] | None = None
    preference: dict[str, str | None] | None = None
    reason_contains: dict[str, str] | None = Field(
        default=None,
        description="Per item id: substring that must appear in the item's reason.",
    )
    reasons_contains: list[str] | None = Field(
        default=None, description="Substrings that must appear in the joined reasons."
    )


class AttentionCase(BaseModel):
    """One labeled attention-policy case (budget / quiet / escalation)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    plane: Literal["attention"]
    title: str
    operation: Literal[
        "budget_decision", "notification_flags", "deadline_escalation", "adaptive_escalation"
    ]
    budget: dict[str, float | int] | None = None
    quiet_period: dict[str, str] | None = Field(
        default=None, description="kind / start / end / reason (ISO-8601)."
    )
    preferences: list[dict[str, str | None]] | None = Field(
        default=None, description="EscalationPreference fields (person/channel/mode/reason)."
    )
    items: list[AttentionItemSpec]
    expect: ExpectAttention


# -- ask ------------------------------------------------------------------------


class AskCase(BaseModel):
    """One ask QA / provenance case (ground truth for T003.AC9).

    ``expect`` carries the ground-truth assertions — e.g.
    ``{"answer_contains": [...], "provenance": [...]}`` — that the
    evaluator applies once the AC9 fixture interface exists. Until then
    the case replays as ``pending`` with ``pending_reason`` as evidence.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    plane: Literal["ask"]
    title: str
    question: str
    context: dict[str, Any] = Field(
        default_factory=dict, description="Seeded-state description for the AC9 pipeline."
    )
    expect: dict[str, Any] = Field(
        default_factory=dict,
        description="Ground truth: e.g. answer_contains / provenance lists.",
    )
    pending_reason: str | None = Field(
        default=None, description="Why the case is pending while the interface is absent."
    )


#: The discriminated union over case planes.
Case = Annotated[
    DetectionCase | AttentionCase | AskCase, Field(discriminator="plane")
]


class SyntheticCorpus(BaseModel):
    """The versioned, fixture-only evaluation corpus (T003.AC10)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    corpus_id: str
    version: int
    seed: int
    anchor: str
    title: str = ""
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_ids_and_anchor(self) -> SyntheticCorpus:
        seen: set[str] = set()
        for case in self.cases:
            if case.id in seen:
                raise ValueError(f"duplicate case id: {case.id}")
            seen.add(case.id)
        try:
            datetime.fromisoformat(self.anchor)
        except ValueError as exc:
            raise ValueError(f"anchor must be ISO-8601: {self.anchor!r}") from exc
        return self


# -- loading --------------------------------------------------------------------


def default_corpus_path() -> Path:
    """``synthetic-lab/corpus/corpus.json`` relative to this file."""
    return Path(__file__).resolve().parent.parent / "corpus" / "corpus.json"


def parse_corpus(data: dict[str, Any]) -> SyntheticCorpus:
    """Validate a raw corpus dict against the schema (raises ValidationError)."""
    return SyntheticCorpus.model_validate(data)


def load_corpus(path: str | Path | None = None) -> SyntheticCorpus:
    """Load + validate the corpus file (fixture-only, offline)."""
    if path is None:
        path = default_corpus_path()
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    try:
        return parse_corpus(data)
    except ValidationError as exc:
        raise ValidationError(exc.errors(), title=f"corpus at {path}") from exc
