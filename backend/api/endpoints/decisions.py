"""Decisions endpoints for the NerveCenter API.

Wired to the read-only :class:`backend.services.decisions_service.DecisionsService`
through ``app.state.services`` — the same pattern as the evidence router. The
service is *durable*: it lists and fetches rows from the ``decisions`` table,
so the response is honest (an empty table yields an empty list, never a
fabricated record; a missing id is a ``404``).

Contract (web/src/api/client.ts):
- ``GET /api/decisions``            → list decisions (``ListResponse`` envelope)
- ``GET /api/decisions/{id}``       → a single decision (JSON object)

Service resolution
------------------
The read-only service is resolved in two steps:

1. ``app.state.services["decisions"]`` when a test or operator has registered
   one explicitly; otherwise
2. a cached :class:`DecisionsService` built over ``app.state.db_engine`` —
   the durable engine populated by
   :func:`backend.api.wiring.install_dependency_wiring`.

Step 2 is what makes the endpoint durable in a real deployment: the decisions
table is read through the same engine every other DB-backed router uses. When
no engine exists (local dev without ``NERVE_DB_URL``) both steps yield no
service and the endpoints keep their honest degradation (empty list / ``404``)
— never a fabricated record.

``request`` is an *optional* parameter (``Request = None``). FastAPI still
injects the ``Request`` when the route is hit through the router, but the
canonical alias router in :mod:`backend.api.app` also serves
``GET /api/decisions`` and ``GET /api/decisions/{id}`` through closures that
call ``list_decisions()`` / ``get_decision(id)`` **without** forwarding a
``Request``. Making the parameter optional (with a ``None`` default) keeps
both invocation shapes working, and avoids a circular import that importing
the module-level ``app`` here would create (this module is imported before
``app = create_app()``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request

from backend.api.models import ApiResponseMeta, ListResponse
from backend.services.decisions_service import DecisionsService

router = APIRouter(prefix="/api/decisions", tags=["decisions"])

#: Cached engine-keyed read-only services (see :func:`_service`). Kept in a
#: module-level dict so the same engine resolves to the same service instance
#: across requests without rebuilding the session factory each call.
_engine_keyed_services: dict[int, DecisionsService] = {}


def _meta(**pagination: int | None) -> ApiResponseMeta:
    return ApiResponseMeta(
        version="1.0.0",
        request_id=f"req-{uuid4().hex[:12]}",
        timestamp=datetime.now(UTC),
        pagination={k: v for k, v in pagination.items() if v is not None} or None,
    )


def _service(request: Request | None) -> DecisionsService | None:
    """Return the durable decisions service, or ``None`` when unconfigured.

    Resolution order:

    1. ``request.app.state.services["decisions"]`` — an explicitly registered
       service (used by tests and by any future wiring that binds it directly).
    2. A cached :class:`DecisionsService` over ``request.app.state.db_engine``
       — the durable engine populated by
       :func:`backend.api.wiring.install_dependency_wiring`. This is the path
       that keeps the endpoint durable in a real deployment.

    ``request`` is optional: the canonical alias closures in
    :mod:`backend.api.app` call these handlers without forwarding one, so when
    it is absent (or no engine is configured) the service degrades to "not
    configured" (honest empty list / 404) instead of raising.
    """
    if request is None:
        return None
    app_state = request.app.state
    services = getattr(app_state, "services", None) or {}
    if isinstance(services, dict):
        explicit = services.get("decisions")
        if explicit is not None:
            return explicit
    engine = getattr(app_state, "db_engine", None)
    if engine is None:
        return None
    cached = _engine_keyed_services.get(id(engine))
    if cached is not None:
        return cached
    service = DecisionsService(engine)
    _engine_keyed_services[id(engine)] = service
    return service


@router.get("", response_model=ListResponse)
@router.get("/", response_model=ListResponse)
async def list_decisions(
    limit: int = 100,
    offset: int = 0,
    request: Request = None,
) -> ListResponse:
    """List durable decisions, newest first.

    Returns an empty ``ListResponse`` when the decisions service is not
    configured (honest degradation — no fabricated records).
    """
    service = _service(request)
    if service is None:
        return ListResponse(
            data=[], meta=_meta(total=0, page=1, per_page=max(limit, 1))
        )
    rows = await service.list_decisions(limit=limit, offset=offset)
    data: list[dict[str, Any]] = [row.model_dump(mode="json") for row in rows]
    return ListResponse(
        data=data,
        meta=_meta(
            total=len(data),
            page=(offset // max(limit, 1)) + 1,
            per_page=limit,
        ),
    )


@router.get("/{decision_id}")
async def get_decision(decision_id: str, request: Request = None) -> dict[str, Any]:
    """Return a single durable decision by its stable id.

    Returns ``404`` when the id is unknown, malformed, or the decisions
    service is not configured — never a fabricated record.
    """
    service = _service(request)
    if service is None:
        raise HTTPException(
            status_code=404, detail=f"Decision {decision_id} not found"
        )
    record = await service.get(decision_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail=f"Decision {decision_id} not found"
        )
    return record.model_dump(mode="json")
