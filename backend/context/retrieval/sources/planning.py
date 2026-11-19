"""Tasks, goals, incidents, evidence (through the agent-tuning ``NerveTools``,
so Ask shares its read code and result clipping), plus bills, follow-ups and
wellness entries."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from backend.agents.tools.nerve import NerveToolError, NerveTools
from backend.context.retrieval.aggregate import StructuredRow
from backend.context.retrieval.intent import Intent
from backend.context.retrieval.sources._util import make_all
from backend.context.retrieval.types import Record


async def _tool(state: Any, tool: str, args: dict[str, Any]) -> list[dict[str, Any]] | None:
    app = type("App", (), {"state": state})()
    try:
        result = await NerveTools(app).call(None, tool, args)
    except NerveToolError:
        return None  # service not wired: not available, not an error
    return list((result or {}).get("items") or [])


async def tasks(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    rows = await _tool(state, "tasks_list", {"limit": 50})
    if rows is None:
        return None
    return await make_all([("tasks", r["id"], r.get("title"),
                            f"status {r.get('status')}{' (open)' if r.get('status') in _TASK_STATUS else ''}, "
                            f"priority {r.get('priority')}") for r in rows])


async def goals(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    rows = await _tool(state, "goals_list", {"limit": 50})
    if rows is None:
        return None
    return await make_all([("goals", r["id"], r.get("title"), f"status {r.get('status')}") for r in rows])


_TASK_STATUS = {"pending": "open", "scheduled": "open", "in_progress": "open"}


async def tasks_structured(state: Any, intent: Intent, cap: int) -> list[StructuredRow] | None:
    """Every task (up to ``cap``) with typed ``priority`` (0..10), ``due_at`` and a status where
    pending, scheduled and in_progress read as ``open``, for "open tasks by priority"."""
    services = getattr(state, "services", None)
    svc = services.get("tasks") if isinstance(services, dict) else None
    if svc is None:
        return None
    rows = list(await svc.list_tasks(status=None, goal_id=None, limit=cap))
    specs: list[tuple[Any, ...]] = []
    fields: list[dict[str, Any]] = []
    for t in rows[:cap]:
        status = _TASK_STATUS.get(str(t.status), str(t.status))
        due = getattr(t, "due_at", None)
        specs.append(("tasks", t.id, t.title,
                      f"status {t.status}, priority {t.priority}" + (f", due {str(due)[:10]}" if due else ""),
                      due or getattr(t, "created_at", None)))
        fields.append({"status": status, "priority": t.priority, "due_at": due,
                       "ts": getattr(t, "created_at", None), "payee": t.title})
    records = await make_all(specs)
    return [StructuredRow(r, f) for r, f in zip(records, fields, strict=True)]


tasks.structured = tasks_structured  # type: ignore[attr-defined]  # found by retrieve()


async def goals_structured(state: Any, intent: Intent, cap: int) -> list[StructuredRow] | None:
    """Every goal (up to ``cap``) with typed ``progress``, ``priority`` and ``due_at`` (deadline).
    An active goal past its deadline is ``overdue`` (behind); other active goals are ``open``."""
    services = getattr(state, "services", None)
    svc = services.get("goals") if isinstance(services, dict) else None
    if svc is None:
        return None
    now = intent.now or datetime.now(UTC)
    rows = list(await svc.list_goals(limit=cap))
    specs: list[tuple[Any, ...]] = []
    fields: list[dict[str, Any]] = []
    for g in rows[:cap]:
        raw = str(getattr(g, "status", "") or "")
        deadline = getattr(g, "deadline", None)
        due = deadline.replace(tzinfo=now.tzinfo) if deadline is not None and deadline.tzinfo is None \
            and now.tzinfo else deadline
        status = "open" if raw == "active" else raw
        behind = raw == "active" and due is not None and due < now
        if behind:
            status = "overdue"
        progress = getattr(g, "progress", None)
        bits = [f"status {raw}"]
        if progress is not None:
            bits.append(f"progress {float(progress):.0%}")
        if deadline is not None:
            bits.append(f"deadline {str(deadline)[:10]}")
        if behind:
            bits.append("behind")
        specs.append(("goals", g.id, g.title, ", ".join(bits), deadline or getattr(g, "created_at", None)))
        fields.append({"status": status, "due_at": due, "progress": progress,
                       "priority": getattr(g, "priority", None), "ts": getattr(g, "created_at", None),
                       "payee": g.title})
    records = await make_all(specs)
    return [StructuredRow(r, f) for r, f in zip(records, fields, strict=True)]


goals.structured = goals_structured  # type: ignore[attr-defined]  # found by retrieve()


async def incidents(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    rows = await _tool(state, "incidents_list", {"limit": 50})
    if rows is None:
        return None
    return await make_all([("incidents", r["id"], r.get("title"),
                            f"{r.get('severity')} {r.get('status')}", r.get("updated_at") or r.get("created_at"))
                           for r in rows])


async def evidence(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    terms = intent.search_terms
    if not terms:
        return [] if getattr(state, "services", None) else None
    rows = await _tool(state, "evidence_search", {"contains": terms[0], "limit": 50})
    if rows is None:
        return None
    return await make_all([("evidence", r["id"], r.get("source_ref"), r.get("content"), r.get("captured_at"))
                           for r in rows])


def _money(cents: Any) -> str:
    """Amount in major units; the store may hand the cents back as text."""
    try:
        return f"{int(cents) / 100:.2f}"
    except (TypeError, ValueError):
        return "unknown amount"
def _field(fields: dict[str, Any], key: str) -> Any:
    """Extracted candidate fields are ``{"payee": {"value": ..., "confidence": ...}}``."""
    raw = fields.get(key)
    return raw.get("value") if isinstance(raw, dict) else raw


async def bills(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    registry = getattr(state, "repository_registry", None)
    if registry is None or not hasattr(registry, "session_factory"):
        return None
    from backend.bills.service import BillService
    from backend.persistence.repositories.bills_repo import BillsRepository
    service = BillService(BillsRepository(registry.session_factory))
    rows = await service.list_bills()
    specs: list[tuple[Any, ...]] = [
        ("bills", b["id"], f"Bill: {b['payee']}",
         f"{_money(b['amount_cents'])} {b['currency']}, {b['status']}, due {str(b['due_at'])[:10]}",
         b["due_at"])
        for b in rows[:limit]
    ]
    # Bills found in mail but not yet accepted are still "coming due" to the owner.
    for c in await service.list_candidates(status="proposed"):
        if c.get("duplicate_of") or len(specs) >= limit:
            continue
        fields = c.get("extracted_fields") or {}
        payee = _field(fields, "payee") or "unknown payee"
        due = _field(fields, "due_at") or _field(fields, "due_date")
        amount = _field(fields, "amount_cents")
        detail = f"{_money(amount) if amount is not None else 'unknown amount'}, unconfirmed"
        if due:
            detail += f", due {str(due)[:10]}"
        specs.append(("bills", f"candidate:{c['id']}", f"Possible bill: {payee}", detail, due or c["created_at"]))
    return await make_all(specs)


def _int_cents(value: Any) -> int | None:
    try:
        return None if value is None or isinstance(value, bool) else int(value)
    except (TypeError, ValueError):
        return None


async def bills_structured(state: Any, intent: Intent, cap: int) -> list[StructuredRow] | None:
    """Every bill (up to ``cap``) with typed fields for aggregation. Candidates found in mail but not
    accepted are included as ``unconfirmed``; duplicates are excluded."""
    registry = getattr(state, "repository_registry", None)
    if registry is None or not hasattr(registry, "session_factory"):
        return None
    from backend.bills.service import BillService
    from backend.persistence.repositories.bills_repo import BillsRepository
    service = BillService(BillsRepository(registry.session_factory))
    rows = await service.list_bills()
    candidates = [c for c in await service.list_candidates(status="proposed") if not c.get("duplicate_of")]
    specs: list[tuple[Any, ...]] = []
    fields: list[dict[str, Any]] = []
    for b in rows[:cap]:
        specs.append(("bills", b["id"], f"Bill: {b['payee']}",
                      f"{_money(b['amount_cents'])} {b['currency']}, {b['status']}, "
                      f"due {str(b['due_at'])[:10]}", b["due_at"]))
        fields.append({"amount_cents": _int_cents(b["amount_cents"]), "currency": b["currency"],
                       "status": b["status"], "due_at": b["due_at"], "ts": b["due_at"],
                       "payee": b["payee"]})
    for c in candidates:
        if len(specs) >= cap:
            break
        f = c.get("extracted_fields") or {}
        payee = _field(f, "payee") or "unknown payee"
        due = _field(f, "due_at") or _field(f, "due_date")
        amount = _field(f, "amount_cents")
        detail = f"{_money(amount) if amount is not None else 'unknown amount'}, unconfirmed"
        if due:
            detail += f", due {str(due)[:10]}"
        when = due or c["created_at"]
        specs.append(("bills", f"candidate:{c['id']}", f"Possible bill: {payee}", detail, when))
        fields.append({"amount_cents": _int_cents(amount), "currency": _field(f, "currency") or "USD",
                       "status": "unconfirmed", "due_at": due, "ts": c["created_at"], "payee": payee})
    records = await make_all(specs)
    return [StructuredRow(r, f) for r, f in zip(records, fields, strict=True)]


bills.structured = bills_structured  # type: ignore[attr-defined]  # found by retrieve()


async def followups(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    service = getattr(state, "followup_service", None)
    if service is None:
        return None
    return await make_all([("followups", f.id, f"Follow-up: {f.reason}", f"{f.target_kind}, {f.status}", f.due_at)
                           for f in service.list()[:limit]])


async def wellness(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    registry = getattr(state, "repository_registry", None)
    if registry is None or not hasattr(registry, "session_factory"):
        return None
    from backend.persistence.repositories.wellness_repo import WellnessRepository
    from backend.wellness.service import WellnessService

    svc = WellnessService(WellnessRepository(registry.session_factory))
    rows = await svc.list_entries(from_dt=(intent.now or datetime.now(UTC)) - timedelta(days=120))
    return await make_all([
        ("wellness", e["id"], "Wellness check-in",
         f"mood {e['mood']}, energy {e['energy']}, stress {e['stress']}, "
         f"sleep {e['sleep_minutes']} min. {e['journal_entry'] or ''}", e["at"])
        for e in rows[:limit]
    ])
