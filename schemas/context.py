"""Context package records (NerveCenter spec Section 10-16).

A *ContextPackage* is an assembled, budgeted bundle of knowledge-graph
records (entities, relationships, claims, evidence, tasks, goals) that a
subsystem hands to a model call. It is the unit the context-assembly and
budgeting layer produces — deliberately small, deterministic, and
re-verifiable so that model calls are reproducible from stored state.

Assumptions (spec not present in the repository):
    - ``token_budget`` bounds the assembled budget; the package records the
      ``tokens_used`` estimate at assembly time.
    - ``sections`` is a typed list of :class:`ContextSection` entries that
      each reference stored records by id (no inlined payloads, keeping the
      package compact and hash-stable).
    - ``context_hash`` is the digest of the assembled content via
      :func:`schemas.base.content_hash`.
"""

from typing import Any, Literal

from pydantic import Field

from .base import Record

#: Classification of the consumer a context package is assembled for.
ContextPurpose = Literal[
    "planning",
    "task_execution",
    "inference",
    "review",
    "decision",
    "summarization",
    "other",
]

#: Which knowledge records a context section references.
ContextSectionKind = Literal[
    "entities",
    "relationships",
    "evidence",
    "claims",
    "decisions",
    "tasks",
    "goals",
    "agent_results",
    "notes",
]


class ContextSection(Record):
    """One section of a :class:`ContextPackage`: a curated, ordered view."""

    kind: ContextSectionKind = Field(
        ...,
        description="Which record kind this section references.",
    )
    title: str | None = Field(
        default=None,
        description="Human-readable label for this section.",
    )
    record_ids: list[str] = Field(
        default_factory=list,
        description="Ids of the stored records, in presentation order.",
    )
    order: int = Field(
        default=1,
        ge=0,
        description="Section ordering within the package.",
    )
    budget_tokens: int | None = Field(
        default=None,
        ge=0,
        description="Token budget allocated to this section, if any.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Section assembly metadata (retrieval query, score, ...).",
    )


class ContextPackage(Record):
    """An assembled, token-budgeted bundle of records for a single use."""

    purpose: ContextPurpose = Field(
        ...,
        description="What the assembled context is being consumed by.",
    )
    goal_id: str | None = Field(
        default=None,
        description="Id of the :class:`Goal` driving this assembly, if any.",
    )
    task_id: str | None = Field(
        default=None,
        description="Id of the :class:`Task` this context is attached to, if any.",
    )
    token_budget: int = Field(
        ...,
        ge=1,
        description="Total token budget allocated for the assembled context.",
    )
    tokens_used: int = Field(
        default=0,
        ge=0,
        description="Estimated tokens actually used by the assembled sections.",
    )
    sections: list[ContextSection] = Field(
        default_factory=list,
        description="Ordered sections composing the package.",
    )
    redacted: bool = Field(
        default=False,
        description="Whether policy redaction was applied before assembly.",
    )
    context_hash: str | None = Field(
        default=None,
        description="Digest of the assembled content for reproducibility.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Assembly metadata (R107 / §35): e.g. ``firewall_excluded`` lists "
            "the sources the personal/engineering partition firewall kept out "
            "of this package, for audit and re-verification."
        ),
    )
