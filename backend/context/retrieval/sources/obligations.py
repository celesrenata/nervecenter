"""Obligations for Ask: commitments, what the owner owes others, and what others owe the owner.

Reads the durable obligation plane through ``ObligationsRepository`` (the repository behind the
Obligations screen and ``/api/obligations``). Terminal rows (done, dismissed) are left out.
Inferred obligations the owner has not accepted are included and labelled ``unconfirmed``.
"""
from __future__ import annotations

from typing import Any

from backend.context.retrieval.aggregate import StructuredRow
from backend.context.retrieval.intent import Intent
from backend.context.retrieval.sources._util import make_all
from backend.context.retrieval.types import Record


def _direction(obligation_type: str) -> str:
    """Whose court the ball is in."""
    return "owed to you" if obligation_type == "waiting_on" else "you owe"


def _status_field(status: str) -> str:
    """Map obligation statuses onto the vocabulary ``aggregate`` understands."""
    if status == "done":
        return "completed"
    if status == "dismissed":
        return status
    return "open"


async def _load(state: Any, cap: int) -> list[Any] | None:
    registry = getattr(state, "repository_registry", None)
    if registry is None or not callable(getattr(registry, "session_factory", None)):
        return None
    from backend.persistence.repositories.obligations_repo import ObligationsRepository

    repo = ObligationsRepository(registry.session_factory)
    return list(await repo.list(actionable_only=True, limit=cap))


def _specs(rows: list[Any], cap: int) -> tuple[list[tuple[Any, ...]], list[dict[str, Any]]]:
    specs: list[tuple[Any, ...]] = []
    fields: list[dict[str, Any]] = []
    for r in rows[:cap]:
        status = str(r.status)
        unconfirmed = r.origin == "inferred"
        bits = [_direction(r.obligation_type), r.obligation_type.replace("_", " "), status]
        if unconfirmed:
            bits.append("unconfirmed")
        bits.append(f"due {r.due_at.date().isoformat()}" if r.due_at else "no due date")
        if r.consequence:
            bits.append(f"consequence: {r.consequence}")
        if r.next_action:
            bits.append(f"next: {r.next_action}")
        when = r.due_at or r.created_at
        specs.append(("obligations", r.id, f"Obligation: {r.title}", ", ".join(bits), when))
        fields.append({"status": "unconfirmed" if unconfirmed else _status_field(status),
                       "due_at": r.due_at, "ts": when, "priority": r.priority,
                       "payee": r.title, "obligation_type": r.obligation_type,
                       "direction": _direction(r.obligation_type)})
    return specs, fields


async def obligations(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    rows = await _load(state, limit)
    if rows is None:
        return None
    specs, _ = _specs(rows, limit)
    return await make_all(specs)


async def obligations_structured(
    state: Any, intent: Intent, cap: int,
) -> list[StructuredRow] | None:
    """Every open obligation (up to ``cap``) with typed due date, status, and priority."""
    rows = await _load(state, cap)
    if rows is None:
        return None
    specs, fields = _specs(rows, cap)
    records = await make_all(specs)
    return [StructuredRow(r, f) for r, f in zip(records, fields, strict=True)]


obligations.structured = obligations_structured  # type: ignore[attr-defined]  # found by retrieve()
