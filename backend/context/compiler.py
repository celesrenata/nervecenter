"""Context compiler for assembling inference context packages.

This module implements the ``ContextCompiler`` that turns engineering
reality into bounded task context according to Section 31 (Context
Compiler) of the NerveCenter specification.

The context compiler:
- Assembles context from knowledge graph records
- Enforces token budgets and content limits
- Prioritizes content based on relevance
- Produces bounded ContextPackage for inference
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from schemas.context import ContextPackage, ContextPurpose, ContextSection, ContextSectionKind

#: Partition discriminator (R107 / §35). Context sources are tagged with the
#: plane they belong to; the compiler enforces the personal/engineering
#: firewall so the two planes do not leak into each other's corpora.
PARTITION_ENGINEERING = "engineering"
PARTITION_PERSONAL = "personal"



class ContextSource(BaseModel):
    """Source of context for compilation."""

    model_config = {"extra": "forbid"}

    id: str = Field(..., description="Unique identifier for the source.")
    kind: ContextSectionKind = Field(..., description="Kind of records in this source.")
    query: dict[str, Any] = Field(
        default_factory=dict,
        description="Query parameters to fetch records.",
    )
    relevance_score: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Relevance score for prioritization.",
    )
    partition: Annotated[
        Literal["engineering", "personal"],
        Field(
            default=PARTITION_ENGINEERING,
            description=(
                "Plane this source belongs to (R107 / §35 firewall). "
                "Engineering compilation excludes ``personal`` sources by "
                "default; personal compilation excludes ``engineering`` "
                "sources by default. Cross-plane access requires an explicit "
                "scoped grant."
            ),
        ),
    ]
    summary: str | None = Field(
        default=None,
        description=(
            "Optional bounded, remote-safe summary for this source (R107 "
            "firewall / §35): when set, only this summary — never the raw "
            "record payloads — may be carried into a remote inference "
            "payload. Compiled into section metadata for audit."
        ),
    )


class BudgetConfig(BaseModel):
    """Token budget configuration."""

    model_config = {"extra": "forbid"}

    total_tokens: int = Field(
        ...,
        ge=1,
        description="Total token budget for the context package.",
    )
    section_limits: dict[ContextSectionKind, int] = Field(
        default_factory=dict,
        description="Per-section token limits.",
    )
    max_sections: int = Field(
        default=10,
        ge=1,
        description="Maximum number of sections to include.",
    )


class CompiledSection(BaseModel):
    """A section that has been compiled with estimated tokens."""

    model_config = {"extra": "forbid"}

    kind: ContextSectionKind = Field(..., description="Kind of records in this section.")
    record_ids: list[str] = Field(
        default_factory=list,
        description="Ids of records included.",
    )
    tokens: int = Field(..., ge=0, description="Estimated token count.")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Compilation metadata.",
    )


class ContextCompiler(BaseModel):
    """Assembles bounded context packages from knowledge graph records.

    The compiler takes high-level context needs and produces a
    ContextPackage with bounded token usage. It handles:

    - Record selection and deduplication
    - Token budget enforcement
    - Content prioritization
    - Metadata tracking

    Usage
    -----
    >>> compiler = ContextCompiler()
    >>> package = compiler.compile(
    ...     purpose=ContextPurpose.INFERENCE,
    ...     goal_id="goal-123",
    ...     sources=[...],
    ...     budget=BudgetConfig(total_tokens=50000)
    ... )
    """

    model_config = {"extra": "forbid"}

    max_records_per_section: int = Field(
        default=1000,
        ge=1,
        description="Maximum records per section.",
    )

    def compile(
        self,
        purpose: ContextPurpose,
        goal_id: str | None = None,
        task_id: str | None = None,
        sources: list[ContextSource] | None = None,
        budget: BudgetConfig | None = None,
        *,
        personal: bool = False,
        granted_communication_ids: list[str] | None = None,
    ) -> ContextPackage:
        """Compile a context package from sources within budget.

        Args:
            purpose: The intended use of the context package.
            goal_id: The goal driving this context, if any.
            task_id: The task this context is for, if any.
            sources: List of context sources to compile.
            budget: Token budget configuration.
            personal: Compile for the *personal* plane. When ``False``
                (default) the package is assembled for engineering use and
                the firewall excludes every ``personal`` source (R107 / §35).
                When ``True`` the package is assembled for personal use and
                the firewall excludes every ``engineering`` source unless it
                is explicitly granted via ``granted_communication_ids``.
            granted_communication_ids: Explicit, scoped grants crossing the
                partition firewall. An engineering compile may name specific
                personal communication ids (an explicit relevant
                relationship, §35); a personal compile may name specific
                engineering record ids. Only *those* ids from the cross-plane
                source are included — the rest of the other plane stays
                out.

        Returns:
            A ContextPackage with prioritized, bounded content. Excluded
            source ids (firewall decisions) are recorded in
            ``package.metadata["firewall_excluded"]`` for audit.
        """
        sources = sources or []
        budget = budget or BudgetConfig(total_tokens=50000)

        # R107 / §35 personal/engineering firewall (default: no cross-plane
        # leakage; cross-plane access requires explicit scoped grants).
        granted = set(granted_communication_ids or [])
        included, excluded_ids = self._partition_gate(sources, personal, granted)
        excluded = [
            {"source_id": s.id, "partition": s.partition} for s in excluded_ids
        ]

        sections = self._compile_sections(included, budget)
        tokens_used = sum(s.tokens for s in sections)

        package = ContextPackage(
            id=self._generate_id(purpose, goal_id, task_id),
            purpose=purpose,
            goal_id=goal_id,
            task_id=task_id,
            token_budget=budget.total_tokens,
            tokens_used=tokens_used,
            sections=[self._to_context_section(s) for s in sections],
            context_hash=None,  # Will be computed by consumer
        )
        if excluded:
            package.metadata["firewall_excluded"] = excluded
        return package

    def _partition_gate(
        self,
        sources: list[ContextSource],
        personal: bool,
        granted: set[str],
    ) -> tuple[list[ContextSource], list[ContextSource]]:
        """Apply the personal/engineering partition firewall (R107 / §35).

        Engineering compilation excludes personal sources by default, and
        personal compilation excludes engineering sources by default. A
        source crosses the partition only when it is explicitly granted via
        ``granted_communication_ids`` (a scoped relationship, not a blanket
        cross-plane read). Returns ``(included, excluded)``.
        """
        included: list[ContextSource] = []
        excluded: list[ContextSource] = []
        for source in sources:
            if personal:
                cross = source.partition == PARTITION_ENGINEERING
            else:
                cross = source.partition == PARTITION_PERSONAL
            if cross and source.id not in granted:
                excluded.append(source)
            else:
                included.append(source)
        return included, excluded

    def _generate_id(
        self,
        purpose: ContextPurpose,
        goal_id: str | None,
        task_id: str | None,
    ) -> str:
        """Generate a unique ID for the context package."""
        suffix = f"-{task_id}" if task_id else f"-{goal_id}" if goal_id else "-default"
        return f"context-{purpose}{suffix}"

    def _compile_sections(
        self, sources: list[ContextSource], budget: BudgetConfig
    ) -> list[CompiledSection]:
        """Compile sources into sections within budget."""
        sections = []
        remaining_budget = budget.total_tokens

        # Sort by relevance score (highest first)
        sorted_sources = sorted(
            sources, key=lambda s: s.relevance_score, reverse=True
        )[: budget.max_sections]

        for source in sorted_sources:
            if remaining_budget <= 0:
                break

            section = self._compile_section(source, budget, remaining_budget)
            if section.tokens > 0:
                sections.append(section)
                remaining_budget -= section.tokens

        return sections

    def _compile_section(
        self,
        source: ContextSource,
        budget: BudgetConfig,
        available_budget: int,
    ) -> CompiledSection:
        """Compile a single source into a section."""
        section_limit = budget.section_limits.get(source.kind, available_budget)
        tokens_budget = min(section_limit, available_budget)

        # Estimate records that fit in budget (assuming ~4 tokens per record)
        max_records = min(
            self.max_records_per_section,
            tokens_budget // 4,
        )

        # For now, generate synthetic record IDs based on query
        record_ids = self._fetch_records(source, max_records)

        # Estimate token count
        tokens = self._estimate_tokens(record_ids)

        metadata: dict[str, Any] = {
            "source_id": source.id,
            "query": source.query,
            "relevance_score": source.relevance_score,
        }
        if source.summary:
            # R107 firewall: the summary is the only remote-safe projection
            # of this source's content; raw payloads never enter the payload.
            metadata["summary"] = source.summary
        return CompiledSection(
            kind=source.kind,
            record_ids=record_ids,
            tokens=tokens,
            metadata=metadata,
        )

    def _fetch_records(self, source: ContextSource, limit: int) -> list[str]:
        """Fetch record IDs from a source.

        In production, this would query the knowledge graph store. A source
        query may carry explicit ``refs`` (record ids already known to be
        relevant, e.g. from the ask grounding context) — those are returned
        verbatim (deduped, bounded) so grounding is deterministic and the
        compiled package references exactly those records (R107 / §35:
        references, not raw content).
        """
        refs = source.query.get("refs") if isinstance(source.query, dict) else None
        if isinstance(refs, list) and refs:
            seen: set[str] = set()
            out: list[str] = []
            for ref in refs:
                if not isinstance(ref, str) or not ref:
                    continue
                if ref not in seen:
                    seen.add(ref)
                    out.append(ref)
            return out[: max(limit, 1)]
        # For testing, generate synthetic IDs
        return [f"{source.kind}-{i:04d}" for i in range(min(limit, 100))]

    def _estimate_tokens(self, record_ids: list[str]) -> int:
        """Estimate token count for a set of record IDs."""
        # Each record ID represents ~4 tokens of content
        return len(record_ids) * 4

    def _to_context_section(self, compiled: CompiledSection) -> ContextSection:
        """Convert a compiled section to a ContextSection."""
        return ContextSection(
            id=f"section-{compiled.kind}",
            kind=compiled.kind,
            record_ids=compiled.record_ids,
            metadata=compiled.metadata,
            budget_tokens=None,  # Not specified per-section
        )

    def estimate_tokens(self, content: str) -> int:
        """Estimate token count for content."""
        # Rough estimate: 4 characters per token
        return len(content) // 4
