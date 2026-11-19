"""Run every source adapter, rank, and return records Ask can cite."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import structlog

from backend.context.retrieval.aggregate import Computation, StructuredRow, compute, user_zone
from backend.context.retrieval.followup import ResolvedQuestion, resolve_followup
from backend.context.retrieval.intent import AGGREGATE, CONVERSATIONAL, Intent, parse_intent
from backend.context.retrieval.ranker import CHAR_BUDGET, MAX_RECORDS, rank
from backend.context.retrieval.sources import ADAPTERS
from backend.context.retrieval.types import Record, SearchDetail

log = structlog.get_logger(__name__)

ADAPTER_TIMEOUT_S = 3.0
PER_ADAPTER_LIMIT = 300
#: hard safety cap on rows an adapter's structured entry point may return for an aggregate
STRUCTURED_ROW_CAP = 5000


@dataclass(frozen=True)
class RetrievalResult:
    intent: Intent
    records: list[Record] = field(default_factory=list)
    #: domains that were available and searched
    searched: list[str] = field(default_factory=list)
    #: domains that failed or timed out (never raised to the caller)
    skipped: list[str] = field(default_factory=list)
    #: result computed in code for an aggregate question (``COMPUTED:`` evidence)
    computation: Computation | None = None
    #: one line naming the record a follow-up refers to (built from re-read records)
    previous_subject: str | None = None
    #: domain -> rows not visible to the model (cut by a cap or the evidence budget)
    truncated: dict[str, int] = field(default_factory=dict, hash=False)
    #: domains whose service is absent (distinct from ``skipped``, which failed)
    unavailable: list[str] = field(default_factory=list)
    #: domain -> what it contributed
    searched_detail: dict[str, SearchDetail] = field(default_factory=dict, hash=False)

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


async def _run_structured(
    domain: str, structured: Any, state: Any, intent: Intent, timeout: float
) -> tuple[str, list[StructuredRow] | None, bool]:
    """Run an adapter's structured entry point ``(state, intent, cap)``."""
    try:
        rows = await asyncio.wait_for(structured(state, intent, STRUCTURED_ROW_CAP), timeout)
    except Exception as exc:  # noqa: BLE001 - one source failing never fails Ask
        log.warning("ask.retrieval.adapter_failed", domain=domain, error=type(exc).__name__)
        return domain, None, True
    return domain, rows, False


def _structured_entry(domain: str, fn: Any, registry: dict[str, Any] | None) -> Any | None:
    """An adapter's optional structured entry point: ``registry[domain]`` or ``fn.structured``."""
    if registry and domain in registry:
        return registry[domain]
    return getattr(fn, "structured", None)


def _user_now(moment: datetime) -> datetime:
    """``moment`` in the user's zone (``NERVE_USER_TIMEZONE``) so "today" and
    "tonight" mean the user's day, not the UTC day. Unset or invalid: unchanged."""
    zone = user_zone()
    return moment.astimezone(zone) if zone else moment


async def retrieve(
    state: Any,
    question: str,
    now: datetime | None = None,
    *,
    adapters: dict[str, Any] | None = None,
    timeout: float = ADAPTER_TIMEOUT_S,
    structured: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
    select_offset: int = 0,
) -> RetrievalResult:
    """Search every allowed domain for ``question``. Deterministic, no model call.

    For an aggregate question an adapter with a structured entry point (``structured[domain]``
    or a ``structured`` attribute on the adapter) returns the full domain up to
    ``STRUCTURED_ROW_CAP`` rows; the answer is computed in code before the evidence budget is
    applied. Adapters without one use the ordinary record list."""
    moment = now or datetime.now(UTC)
    intent = parse_intent(question, _user_now(moment))
    if select_offset and intent.operator is not None:
        intent = replace(intent, operator=replace(intent.operator, offset=select_offset))
    if intent.kind == CONVERSATIONAL:
        return RetrievalResult(intent=intent)  # greetings and "what can you do": no source is read
    resolved = resolve_followup(question, history, intent)
    if resolved is not None:
        return await _retrieve_followup(
            state, intent, resolved, moment, adapters=adapters, timeout=timeout, structured=structured,
        )
    table = adapters if adapters is not None else ADAPTERS
    aggregate = intent.kind == AGGREGATE and intent.operator is not None
    jobs = []
    struct_domains: set[str] = set()
    for domain, fn in table.items():
        entry = _structured_entry(domain, fn, structured) if aggregate else None
        if entry is not None:
            struct_domains.add(domain)
            jobs.append(_run_structured(domain, entry, state, intent, timeout))
        else:
            jobs.append(_run(domain, fn, state, intent, timeout))
    outcomes = await asyncio.gather(*jobs)
    candidates: list[Record] = []
    searched: list[str] = []
    skipped: list[str] = []
    unavailable: list[str] = []
    detail: dict[str, SearchDetail] = {}
    rows_by_domain: dict[str, list[StructuredRow]] = {}
    capped: set[str] = set()
    for domain, rows, failed in outcomes:
        if failed:
            skipped.append(domain)
            detail[domain] = SearchDetail(domain, "skipped", structured=domain in struct_domains)
        elif rows is None:
            unavailable.append(domain)
            detail[domain] = SearchDetail(domain, "unavailable", structured=domain in struct_domains)
        elif domain in struct_domains:
            rows = list(rows)
            hit_cap = len(rows) >= STRUCTURED_ROW_CAP
            rows = rows[:STRUCTURED_ROW_CAP]
            if hit_cap:
                capped.add(domain)
            searched.append(domain)
            rows_by_domain[domain] = rows
            candidates.extend(r.record for r in rows)
            detail[domain] = SearchDetail(domain, "searched", len(rows), capped=hit_cap, structured=True)
        else:
            searched.append(domain)
            candidates.extend(rows)
            detail[domain] = SearchDetail(domain, "searched", len(rows))

    computation = _compute(rows_by_domain, intent, moment, capped) if aggregate else None
    records = rank(candidates, intent, moment)
    if computation is not None and computation.record_ids:
        records = _pin_covered(records, candidates, computation, rows_by_domain)

    shown: dict[str, int] = {}
    for rec in records:
        shown[rec.domain] = shown.get(rec.domain, 0) + 1
    truncated: dict[str, int] = {}
    for domain, d in detail.items():
        if d.state != "searched":
            continue
        detail[domain] = d = replace(d, rows_shown=shown.get(domain, 0))
        if d.structured:
            dropped = max(0, d.rows_read - d.rows_shown)
            if d.capped:
                dropped = max(dropped, 1)
            if dropped:
                truncated[domain] = dropped
    if computation is not None and truncated:
        # the scope line describes the domain the computation ran over, not every domain read
        used = set(computation.record_ids)
        target = {r.domain for r in candidates if r.id in used}
        scoped = {d: n for d, n in truncated.items() if d in target} if target else truncated
        if scoped:
            computation = _with_scope_line(computation, scoped, detail)

    return RetrievalResult(
        intent=intent,
        records=records,
        searched=searched,
        skipped=skipped,
        computation=computation,
        truncated=truncated,
        unavailable=unavailable,
        searched_detail=detail,
    )


def _compute(
    rows_by_domain: dict[str, list[StructuredRow]], intent: Intent, moment: datetime, capped: set[str],
) -> Computation | None:
    """Compute over the intent's target domain (a hinted domain with rows; else the only one)."""
    if "security" in intent.domain_hints:
        return None  # held or quarantined counts come from the status summary, not mail rows
    populated = [d for d, rows in rows_by_domain.items() if rows]
    hinted = [d for d in populated if d in intent.domain_hints]
    targets = hinted or (populated if len(populated) == 1 else [])
    for domain in targets:
        try:
            result = compute(rows_by_domain[domain], intent, moment)
        except Exception as exc:  # noqa: BLE001 - a bad row never fails Ask
            log.warning("ask.retrieval.compute_failed", domain=domain, error=type(exc).__name__)
            continue
        if result is not None:
            return replace(result, truncated=domain in capped)
    return None


async def _retrieve_followup(
    state: Any, intent: Intent, resolved: ResolvedQuestion, moment: datetime, *,
    adapters: dict[str, Any] | None, timeout: float, structured: dict[str, Any] | None,
) -> RetrievalResult:
    """Re-run the previous user question through the adapters and keep the record(s) the follow-up
    refers to. Carried ids are only a filter over freshly read, policy-checked records."""
    inner = await retrieve(
        state, resolved.question, moment, adapters=adapters, timeout=timeout, structured=structured,
        history=list(resolved.earlier),
    )
    ids = [r.id for r in inner.records]
    carried = [i for i in resolved.carried_ids if i in ids]
    single = inner.computation is not None and inner.computation.kind in _SINGLE
    if resolved.shift and single and carried:
        # "the one after that" for biggest/next/latest: select one further down the same order
        shifted = await retrieve(
            state, resolved.question, moment, adapters=adapters, timeout=timeout,
            structured=structured, history=list(resolved.earlier), select_offset=resolved.shift,
        )
        used = set(shifted.computation.record_ids) if shifted.computation else set()
        inner = shifted
        picked = [r for r in shifted.records if r.id in used]
    elif resolved.shift and carried:
        pos = ids.index(carried[0]) + resolved.shift
        picked = [inner.records[pos]] if pos < len(ids) else []
    else:
        picked = [r for r in inner.records if r.id in set(carried)]
    subject = None
    if picked:
        subject = f"Previous subject: {picked[0].domain.replace('_', ' ')} \"{picked[0].title}\"".replace("\n", " ")
    return replace(inner, intent=intent, records=picked, computation=None, previous_subject=subject)


#: operators that select exactly one record
_SINGLE = frozenset({"max", "min", "earliest", "latest", "next"})
#: operators whose evidence is exactly the records they selected
_SELECTING = frozenset({"overdue", "due_soon", "max", "min", "top_n"})


def _pin_covered(
    ranked: list[Record], candidates: list[Record], computation: Computation,
    rows_by_domain: dict[str, list[StructuredRow]],
) -> list[Record]:
    """Records the computation used come first so its citations resolve. For selecting operators
    (overdue, due soon, max, min, top N) they are the only evidence, plus unconfirmed proposals; other operators keep the
    remaining ranked records after them. The evidence budget applies to the model-visible list
    only (the computed result is never cut)."""
    by_id = {r.id: r for r in candidates}
    pinned = [by_id[i] for i in computation.record_ids if i in by_id]
    if computation.kind in _SELECTING:
        # Unconfirmed proposals (not yet real bills) stay visible after the selected records.
        unconfirmed = {
            row.record.id for rows in rows_by_domain.values() for row in rows
            if row.fields.get("status") == "unconfirmed"
        }
        seen = {r.id for r in pinned}
        ordered = pinned + [r for r in ranked if r.id in unconfirmed and r.id not in seen]
    else:
        seen = {r.id for r in pinned}
        ordered = pinned + [r for r in ranked if r.id not in seen]
    out: list[Record] = []
    used = 0
    for rec in ordered:
        size = len(rec.title) + len(rec.snippet) + 60
        if out and (len(out) >= MAX_RECORDS or used + size > CHAR_BUDGET):
            break
        out.append(rec if rec.score > 0 else rec.with_score(0.5))
        used += size
    return out


def _with_scope_line(
    computation: Computation, truncated: dict[str, int], detail: dict[str, SearchDetail],
) -> Computation:
    """Add "Showing 12 of 47 bills as sources" (and the safety-cap note) to the COMPUTED text."""
    parts = []
    for domain, dropped in truncated.items():
        d = detail[domain]
        if d.capped:
            parts.append(f"Only the first {d.rows_read} {domain} rows were read (safety cap)")
        else:
            parts.append(f"Showing {d.rows_shown} of {d.rows_read} {domain} as sources")
    return replace(computation, text=f"{computation.text} {'. '.join(parts)}.")
