"""Bills endpoints (Personal-Ops Recovery §7 — workstream G).

**Hard rule (frozen):** NerveCenter never transfers money, never purchases,
and never assumes payment. ``confirmed_paid`` is written ONLY by the
explicit ``POST /{bill_id}/confirm-paid`` user action (which itself merely
records "the user says they paid" — it pays nothing). No code path in the
bills plane derives payment from a passing due date, a scan, or a heuristic.

Contract (frozen routes, ``/api/bills`` — spec §15):

- ``GET    /?status=``               — list bills; optional status filter
  (``pending`` / ``confirmed_paid`` / ``disputed`` / ``stale``).
- ``POST   /``                        — create a bill (user-created: payee,
  amount, currency, due date, recurrence/interval). New bills are always
  ``pending``; ``status`` is not an input.
- ``GET    /upcoming?days=14``        — bills due within the window
  (confirmed_paid excluded by the service).
- ``GET    /calendar?month=YYYY-MM``  — obligations grouped by day.
- ``POST   /{bill_id}/confirm-paid``  — the ONLY paid-state writer; resolves
  the bill's durable attention item (if any) in the same request.
- ``PATCH  /{bill_id}``               — field-level edit; ``status`` may only
  be set to ``pending`` or ``disputed`` here (``confirmed_paid`` / ``stale``
  via PATCH → 422).

Escalation side effect (house pattern, wave K): ``GET /`` and
``GET /upcoming`` first run ``sweep_stale`` (overdue-unconfirmed → ``stale``,
never ``confirmed_paid``), then compute the deterministic
``bill_escalation_level`` per bill and mirror any level ≥ 1 into a durable
attention item via ``BillService.refresh_attention`` (one item per bill,
updated in place).

House degradation: without a durable ``repository_registry`` on
``app.state``, reads return 200 with empty shapes (``{"bills": []}`` etc.)
and writes return 202 ``{"executed": false, "reason":
"persistence_not_configured"}`` — never a 500, never a synthetic record.
"""

from __future__ import annotations

import calendar
import re
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.bills.service import BillService, bill_escalation_level
from backend.persistence.repositories.bills_repo import BillsRepository

router = APIRouter(prefix="/api/bills", tags=["bills"])

#: Recurrence values accepted on create (API surface, underscore form).
_RECURRENCES = Literal["one_time", "weekly", "monthly", "yearly", "other"]
#: Status values accepted on the ``?status=`` query filter.
_STATUSES = Literal["pending", "confirmed_paid", "disputed", "stale"]
#: The only statuses PATCH may set (paid state is written exclusively by
#: the confirm-paid action).
_PATCH_STATUSES = {"pending", "disputed"}
#: API recurrence → (stored recurrence, default interval payload).
#: ``yearly`` is carried as ``other`` + ``{"years": 1}`` per the durable
#: contract (``BILL_RECURRENCES`` has no yearly).
_RECURRENCE_MAP: dict[str, tuple[str, dict[str, Any]]] = {
    "one_time": ("one-time", {}),
    "weekly": ("weekly", {}),
    "monthly": ("monthly", {}),
    "yearly": ("other", {"years": 1}),
    "other": ("other", {}),
}
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class BillCreateRequest(BaseModel):
    """A user-created bill. Status is always ``pending`` on create."""

    payee: str = Field(min_length=1, max_length=256)
    amount_cents: int = Field(ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    due_at: datetime
    recurrence: _RECURRENCES = "one_time"
    interval: dict[str, Any] = Field(default_factory=dict)


class BillPatchRequest(BaseModel):
    """Field-level edit. ``None`` = leave as-is (partial patch).

    ``status`` may only be set to ``pending`` or ``disputed`` here;
    ``confirmed_paid`` / ``stale`` are rejected (422) — the paid state is
    written ONLY by ``POST /{id}/confirm-paid``.
    """

    payee: str | None = Field(default=None, min_length=1, max_length=256)
    amount_cents: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    due_at: datetime | None = None
    recurrence: _RECURRENCES | None = None
    interval: dict[str, Any] | None = None
    status: str | None = None


# ---------------------------------------------------------------------------
# House plumbing
# ---------------------------------------------------------------------------


def _registry(request: Request) -> Any:
    """The durable repository registry, or ``None`` (degraded app)."""
    return getattr(request.app.state, "repository_registry", None)


def _service(request: Request) -> BillService | None:
    """Build the bills service from the durable registry, or ``None``."""
    registry = _registry(request)
    if registry is None:
        return None
    return BillService(BillsRepository(registry.session_factory))


def _not_configured() -> dict[str, Any]:
    return {"executed": False, "reason": "persistence_not_configured"}


def _degraded_response() -> JSONResponse:
    """202 + honest "not executed" — writes with no durable registry."""
    return JSONResponse(status_code=202, content=_not_configured())


def _with_level(bill: dict[str, Any]) -> dict[str, Any]:
    """Attach the deterministic escalation level to a bill payload.

    ``None`` = no escalation (confirmed paid, or no due date).
    """
    level = bill_escalation_level(bill)
    bill["escalation_level"] = level
    return bill


async def _escalate(request: Request, svc: BillService, bills: list[dict[str, Any]]) -> None:
    """Mirror level ≥ 1 escalations into durable attention items.

    One item per bill, updated in place (the service dedups by
    ``source_refs``). Silent degrade: attention failures never break the
    read.
    """
    registry = _registry(request)
    for bill in bills:
        level = bill_escalation_level(bill)
        if level is not None and level >= 1:
            try:
                await svc.refresh_attention(bill, level=level, registry=registry)
            except Exception:  # noqa: BLE001 — attention is best-effort
                pass


def _parse_iso(value: str, *, param: str) -> datetime:
    """Parse an ISO-8601 timestamp, normalizing to a tz-aware UTC datetime."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"invalid ISO-8601 datetime for {param}: {value!r}",
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _recurrence_fields(recurrence: str) -> tuple[str, dict[str, Any]]:
    """Map the API recurrence onto the durable contract (validated up front)."""
    return _RECURRENCE_MAP[recurrence]


def _month_bounds(month: str) -> tuple[int, int]:
    """Validate ``YYYY-MM`` and return ``(year, month_num)``."""
    if not _MONTH_RE.match(month):
        raise HTTPException(
            status_code=422,
            detail=f"invalid month (expected YYYY-MM): {month!r}",
        )
    year, month_num = month.split("-")
    return int(year), int(month_num)


async def _require_bill(svc: BillService, bill_id: str) -> dict[str, Any]:
    bill = await svc.get_bill(bill_id)
    if bill is None:
        raise HTTPException(status_code=404, detail="bill not found")
    return bill


async def _resolve_bill_attention(registry: Any, bill_id: str) -> bool:
    """Resolve durable attention items that reference ``bill:{id}``.

    Best-effort: any failure silently degrades (the confirmation itself
    already succeeded). Returns whether at least one item was resolved.
    """
    resolved = False
    try:
        rows = await registry.attention.list_attention_items(state="active")
    except Exception:  # noqa: BLE001 — degrade silently
        return False
    for row in rows:
        try:
            refs = [str(r) for r in (row.source_refs or [])]
        except Exception:  # noqa: BLE001 — degrade silently
            continue
        if f"bill:{bill_id}" in refs:
            try:
                await registry.attention.set_state(row.id, "resolved")
                resolved = True
            except Exception:  # noqa: BLE001 — degrade silently
                continue
    return resolved


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@router.get("/")
async def list_bills(request: Request, status: _STATUSES | None = None) -> dict[str, Any]:
    """Bill list, optionally filtered by status.

    Runs the stale sweep first (overdue-unconfirmed → ``stale`` — never
    ``confirmed_paid``), then mirrors any level ≥ 1 escalation into the
    durable attention plane. Each bill payload carries
    ``escalation_level`` (``None`` when no escalation applies).
    """
    registry = _registry(request)
    if registry is None:
        return {"bills": [], "durable": False}
    svc = _service(request)
    assert svc is not None
    try:
        await svc.sweep_stale()
    except Exception:  # noqa: BLE001 — the sweep is best-effort on reads
        pass
    bills = [_with_level(b) for b in await svc.list_bills(status=status)]
    await _escalate(request, svc, bills)
    return {"bills": bills, "durable": True}


@router.get("/upcoming")
async def upcoming_bills(
    request: Request,
    days: int = Query(default=14, ge=1, le=90, description="Due-within window"),
) -> dict[str, Any]:
    """Bills due within the window (confirmed_paid excluded by the service).

    Same sweep + escalation side effects as ``GET /``; the sweep is what
    makes "upcoming" honest about bills that just passed their due date.
    """
    registry = _registry(request)
    if registry is None:
        return {"bills": [], "days": days, "durable": False}
    svc = _service(request)
    assert svc is not None
    try:
        await svc.sweep_stale()
    except Exception:  # noqa: BLE001 — the sweep is best-effort on reads
        pass
    bills = [_with_level(b) for b in await svc.upcoming(days=days)]
    await _escalate(request, svc, bills)
    return {"bills": bills, "days": days, "durable": True}


@router.get("/calendar")
async def monthly_calendar(
    request: Request,
    month: str = Query(description="The month, ``YYYY-MM``"),
) -> dict[str, Any]:
    """Obligations grouped by day for one month.

    ``days`` lists only days that have at least one obligation, in
    calendar order; the web view renders the rest of the month as empty
    cells.
    """
    registry = _registry(request)
    year, month_num = _month_bounds(month)  # validate before any service call
    if registry is None:
        return {
            "month": month,
            "days": [],
            "num_days": calendar.monthrange(year, month_num)[1],
            "durable": False,
        }
    svc = _service(request)
    assert svc is not None
    result = await svc.monthly_calendar(month)
    result["num_days"] = calendar.monthrange(year, month_num)[1]
    result["durable"] = True
    return result


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


@router.post("/", status_code=201)
async def create_bill(request: Request, body: BillCreateRequest) -> JSONResponse:
    """Create a bill (status always ``pending`` on create).

    NerveCenter never pays and never assumes payment — this merely
    records an obligation the user says they have. 201 on success; 202
    ``executed:false`` when no durable registry is configured.
    """
    registry = _registry(request)
    if registry is None:
        return _degraded_response()
    svc = _service(request)
    assert svc is not None
    stored_recurrence, default_interval = _recurrence_fields(body.recurrence)
    bill = await svc.create_bill(
        payee=body.payee,
        amount_cents=body.amount_cents,
        currency=body.currency,
        due_at=_parse_iso(body.due_at.isoformat(), param="due_at"),
        recurrence=stored_recurrence,
        interval=body.interval or default_interval,
        status="pending",
    )
    return {"bill": _with_level(bill), "executed": True}


@router.post("/{bill_id}/confirm-paid")
async def confirm_paid(request: Request, bill_id: str) -> JSONResponse:
    """The ONLY paid-state writer in the bills plane.

    Records the user's explicit confirmation (``confirmed_paid`` +
    ``confirmed_at``). This does NOT pay anything — there is no payment
    path anywhere in the bills workstream. Resolves the bill's durable
    attention item (if any) in the same request. 202 ``executed:false``
    when no durable registry is configured.
    """
    registry = _registry(request)
    if registry is None:
        return _degraded_response()
    svc = _service(request)
    assert svc is not None
    bill = await svc.confirm_paid(bill_id)
    if bill is None:
        raise HTTPException(status_code=404, detail="bill not found")
    await _resolve_bill_attention(registry, bill_id)
    return {"bill": _with_level(bill), "executed": True}


@router.patch("/{bill_id}")
async def patch_bill(request: Request, bill_id: str, body: BillPatchRequest) -> JSONResponse:
    """Field-level edit of a bill (``None`` = leave as-is).

    Allowed fields: payee, amount_cents, currency, due_at, recurrence,
    interval — plus ``status`` only to ``pending`` or ``disputed``.
    ``status=confirmed_paid`` / ``stale`` here → 422: the paid state is
    written exclusively by ``POST /{id}/confirm-paid``, and ``stale`` is
    owned by the sweep. Unknown fields → 422. 202 ``executed:false``
    when no durable registry is configured.
    """
    registry = _registry(request)
    if registry is None:
        return _degraded_response()

    fields: dict[str, Any] = {}
    for name in (
        "payee",
        "amount_cents",
        "currency",
        "due_at",
        "recurrence",
        "interval",
        "status",
    ):
        value = getattr(body, name)
        if value is None:
            continue
        if name == "due_at":
            value = _parse_iso(value.isoformat(), param="due_at")
        fields[name] = value
    if "recurrence" in fields:
        stored_recurrence, _default_interval = _recurrence_fields(fields["recurrence"])
        fields["recurrence"] = stored_recurrence
    if "status" in fields and fields["status"] not in _PATCH_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=(
                "status can only be set to 'pending' or 'disputed' via PATCH — "
                "confirm-paid is written by POST /{id}/confirm-paid, and "
                "'stale' is owned by the stale sweep"
            ),
        )
    if not fields:
        raise HTTPException(status_code=422, detail="no bill fields provided")

    svc = _service(request)
    assert svc is not None
    bill = await svc.update_bill(bill_id, **fields)
    if bill is None:
        raise HTTPException(status_code=404, detail="bill not found")
    return {"bill": _with_level(bill), "executed": True}
