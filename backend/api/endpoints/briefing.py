"""Briefing endpoints (prompt2 §30 — Briefings; §52 — Operational State).

Contract:
- ``GET /api/briefing``  → one composed briefing; query ``?kind=
                           morning|midday|evening`` (default ``morning``).
- ``GET /api/briefing/operational`` → the §52 Daily Operational State rollup
  (AC-R17) as a bare object (not the §46 ``ListResponse`` envelope),
  consumed by the ``nerve today`` CLI (AC-R11).

A read-only, deterministic aggregation across the personal-operations planes
(attention, waiting-on, calendar, email, messages, voicemail). The store is
the process-wide :func:`backend.briefing.get_briefing_service` singleton
(bound onto ``app.state.briefing_service`` at startup). It is *always*
available — an empty store returns an ``empty: true`` brief. There is no
ingest, no write, and no ``now`` query param: the clock is pinned by the
compose call (defaults to the wall clock) so the four §30 sections render
exactly and the screen shows the honest closing line when nothing needs
attention.

The operational-state rollup (R109) projects the same planes plus the
persistence-backed services (goals / incidents / approvals) from
``app.state.services`` — sections whose owning service is not wired render
as honest empty lists, never invented data.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from backend.api.models import Briefing, BriefingKind, OperationalState
from backend.briefing.operational import compose_operational
from backend.briefing.service import BriefingService, get_briefing_service

router = APIRouter(prefix="/api/briefing", tags=["briefing"])


def _service(request: Request) -> BriefingService:
    svc = getattr(request.app.state, "briefing_service", None)
    if svc is None:
        # Defensive: the singleton is always resolvable even if startup wiring
        # has not run yet (e.g. unit tests hitting the router directly).
        svc = get_briefing_service()
        request.app.state.briefing_service = svc
    return svc


# -- routes -----------------------------------------------------------------
@router.get("", response_model=Briefing)
@router.get("/", response_model=Briefing)
async def get_briefing(
    request: Request,
    kind: str = Query(
        default="morning",
        description=(
            "Which slot of the day to render: 'morning' (the full daily "
            "brief, §30 example) | 'midday' | 'evening'. The four sections are "
            "identical for all three; the kind is echoed back as metadata."
        ),
    ),
) -> Briefing:
    """Compose one briefing from the live plane state (§30)."""
    kind_key = (kind or "").strip() or BriefingKind.MORNING.value
    try:
        kind_enum = BriefingKind(kind_key)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid kind '{kind}'. Use morning|midday|evening.",
        )
    svc = _service(request)
    return svc.compose(kind=kind_enum)


def _services(request: Request) -> dict[str, Any]:
    """The persistence-backed service map on ``app.state`` (may be empty).

    ``wire_dependencies`` populates this at startup from the durable engine;
    when no ``NERVE_DB_URL`` is configured it is an empty dict, and the
    operational-state composer renders those sections as honest empties.
    """
    services = getattr(request.app.state, "services", None)
    return services if isinstance(services, dict) else {}


@router.get("/operational", response_model=OperationalState)
async def get_operational(
    request: Request,
    now: str | None = Query(
        default=None,
        description=(
            "Optional ISO-8601 UTC instant that pins the rollup clock. When "
            "omitted the wall clock is used (the live CLI / MCP surfaces never "
            "pass it). Exposed so the time-relative sections (calendar "
            "today, blocked goals, waiting-on day counts) can be asserted "
            "deterministically without a clock time-bomb."
        ),
    ),
) -> OperationalState:
    """The §52 Daily Operational State rollup (AC-R17).

    A bare object — *not* wrapped in the §46 ``ListResponse`` envelope —
    projecting the eight §52 sections from their owning services at compose
    time. The clock defaults to the wall clock; the in-memory planes are
    always present, and any persistence-backed service that is not wired
    renders its section as an honest empty list.
    """
    return await compose_operational(
        _services(request),
        now=now,
        detection=None,
        waiting_on=None,
        calendar=None,
    )
