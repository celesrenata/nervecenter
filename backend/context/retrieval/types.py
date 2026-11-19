"""Shared types for Ask retrieval."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Record:
    """One retrievable record. ``id`` is citation-safe: ``<prefix>-<10 hex>``."""

    id: str
    domain: str
    title: str
    snippet: str = ""
    when: str | None = None
    link: str | None = None
    #: Moment the record is about (for date windows and recency); not sent to the model.
    ts: datetime | None = field(default=None, compare=False)
    score: float = 0.0

    def with_score(self, score: float) -> Record:
        return replace(self, score=score)

    def public(self) -> dict[str, Any]:
        """What the model and the browser see."""
        return {
            "id": self.id,
            "domain": self.domain,
            "title": self.title,
            "when": self.when,
            "snippet": self.snippet,
            "link": self.link,
        }


@dataclass(frozen=True)
class SearchDetail:
    """What one domain contributed to a retrieval (``RetrievalResult.searched_detail``)."""

    domain: str
    #: ``searched``, ``unavailable`` (service absent) or ``skipped`` (raised or timed out)
    state: str = "searched"
    #: rows the adapter returned
    rows_read: int = 0
    #: records from this domain left in the model-visible evidence
    rows_shown: int = 0
    #: the adapter hit the hard safety cap, so ``rows_read`` is a lower bound
    capped: bool = False
    #: the adapter was run through its structured entry point
    structured: bool = False
