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
