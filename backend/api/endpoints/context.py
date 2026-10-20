"""Context compilation API adapters.

``GET  /api/context`` and ``GET /api/context/{item_id}`` remain thin
read-only adapters. ``POST /api/context/ask`` is the real T003-04 grounded
ask pipeline (T003.AC9): context compiler → R107 PDP pre-flight →
OmniRoute adapter/transport → grounded answer with citations and
provenance. The previous honest 202-ACK stub is gone; the 202
``{accepted, executed:false}`` shape is now reserved for **genuine**
inference-transport unavailability (house degradation style, spec
T003.AC9).

Request body: ``{"question": "..."}`` plus optional ``context``
(partition / sensitivity / fixture grounding — references and summaries
only; R107 firewall: no raw personal plaintext enters the remote payload).

Dependency wiring: the ask service is bound onto
``app.state.ask_service`` at startup (see :mod:`backend.api.app`). The
route resolves it defensively so unit tests hitting the router directly
still degrade honestly instead of 500-ing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import Request
from starlette.responses import JSONResponse

from backend.api.endpoints._helpers import make_read_router
from backend.context.ask import AskRequest, AskService, get_ask_service

router = make_read_router(
    prefix="/api/context",
    tag="context",
    service_name="context",
    method_names=("list", "compile"),
)


def _ask_service(request: Request) -> AskService:
    """Resolve the ask service from ``app.state.ask_service``.

    Defensive fallback (drafts plane convention): if startup wiring did
    not run (unit tests hitting the router directly), resolve the
    process-wide singleton.
    """
    svc = getattr(request.app.state, "ask_service", None)
    if not isinstance(svc, AskService):
        svc = get_ask_service()
        request.app.state.ask_service = svc
    return svc


@router.post("/ask")
async def ask(request: Request, body: AskRequest) -> JSONResponse:
    """Ask the context service a question (§47 ``nerve ask``, T003.AC9).

    Request body: ``{"question": "..."}`` plus optional ``context``
    (partition / sensitivity / fixture grounding).

    Responses:
    - 200 ``status="answered"`` — grounded answer, citations (each pointing
      at a context/evidence reference), model/route metadata, and the
      deterministic fixture marker in fixture mode.
    - 200 ``status="policy_denied"`` — structured R107 PDP denial (reason,
      reason code, policy id); no fabricated answer.
    - 202 ``status="unavailable"`` / ``request_status="pending"`` — genuine
      transport unavailability (reason + request id, no answer).
    """
    question = (body.question or "").strip()
    if not question:
        return JSONResponse(
            status_code=422,
            content={
                "accepted": False,
                "executed": False,
                "service": "context",
                "request_id": str(uuid4()),
                "timestamp": datetime.now(UTC).isoformat(),
                "status": "invalid_request",
                "detail": "'question' is required and must be non-empty.",
                "result": None,
            },
        )

    svc = _ask_service(request)
    outcome = svc.ask(AskRequest(question=question, context=body.context))
    return JSONResponse(status_code=outcome.status_code, content=outcome.body)
