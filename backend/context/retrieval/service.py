"""Run every source adapter, rank, and return records Ask can cite."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from backend.context.retrieval.intent import Intent, parse_intent
from backend.context.retrieval.ranker import rank
from backend.context.retrieval.sources import ADAPTERS
from backend.context.retrieval.types import Record

log = structlog.get_logger(__name__)

ADAPTER_TIMEOUT_S = 3.0
PER_ADAPTER_LIMIT = 300


@dataclass(frozen=True)
class RetrievalResult:
    intent: Intent
    records: list[Record] = field(default_factory=list)
    #: domains that were available and searched
    searched: list[str] = field(default_factory=list)
    #: domains that failed or timed out (never raised to the caller)
    skipped: list[str] = field(default_factory=list)

    def by_id(self) -> dict[str, Record]:
        return {r.id: r for r in self.records}

    def domains(self) -> list[str]:
        return list(dict.fromkeys(r.domain for r in self.records))


async def _run(
    domain: str, adapter: Any, state: Any, intent: Intent, timeout: float
) -> tuple[str, list[Record] | None, bool]:
    try:
        rows = await asyncio.wait_for(adapter(state, intent, PER_ADAPTER_LIMIT), timeout)
    except Exception as exc:  # noqa: BLE001 - one source failing never fails Ask
        log.warning("ask.retrieval.adapter_failed", domain=domain, error=type(exc).__name__)
        return domain, None, True
    return domain, rows, False


async def retrieve(
    state: Any,
    question: str,
    now: datetime | None = None,
    *,
    adapters: dict[str, Any] | None = None,
    timeout: float = ADAPTER_TIMEOUT_S,
) -> RetrievalResult:
    """Search every allowed domain for ``question``. Deterministic, no model call."""
    moment = now or datetime.now(UTC)
    intent = parse_intent(question, moment)
    table = adapters if adapters is not None else ADAPTERS
    outcomes = await asyncio.gather(
        *(_run(domain, fn, state, intent, timeout) for domain, fn in table.items())
    )
    candidates: list[Record] = []
    searched: list[str] = []
    skipped: list[str] = []
    for domain, rows, failed in outcomes:
        if failed:
            skipped.append(domain)
        elif rows is not None:
            searched.append(domain)
            candidates.extend(rows)
    return RetrievalResult(
        intent=intent,
        records=rank(candidates, intent, moment),
        searched=searched,
        skipped=skipped,
    )
