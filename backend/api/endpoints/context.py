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

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog
from fastapi import Request
from starlette.responses import JSONResponse, StreamingResponse

from backend.agents.registry import get_agent_registry
from backend.api.endpoints._helpers import make_read_router
from backend.context.ask import AskInference, AskRequest, AskService, get_ask_service
from backend.context.ask_actions import build_calendar_context, looks_like_calendar_create
from backend.context.ask_delete import looks_like_calendar_delete
from backend.context.retrieval import RetrievalResult, retrieve

log = structlog.get_logger(__name__)

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
        audit = _resolve_audit(request, body.agent, body.inference)
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
                "inference": audit,
                "result": None,
            },
        )


    svc = _ask_service(request)

    # Contract §2: when the request names an agent, hydrate the process-wide
    # registry's in-process binding store from the persisted instance's
    # ``inference`` jsonb column so the (synchronous) ``ask`` resolution can
    # read it. ``AskService.ask`` resolves in this order: default → agent
    # binding → request override, and echoes the winner as the ``inference``
    # audit key in every response shape.
    if body.agent is not None and body.agent != "":
        await _hydrate_agent_binding(request, body.agent)

    # The pipeline is synchronous (15-25 s of inference); run it off the event
    # loop so the rest of the API keeps serving while it works.
    calendar = await _calendar_context(request, question, body.account_id)
    retrieval = await _retrieval(request, svc, question, body, calendar)
    outcome = await asyncio.to_thread(
        svc.ask,
        AskRequest(
            question=question,
            context=body.context,
            agent=body.agent,
            inference=body.inference,
            account_id=body.account_id,
            session_id=body.session_id,
        ),
        calendar=calendar,
        retrieval=retrieval,
    )
    return JSONResponse(status_code=outcome.status_code, content=outcome.body)


def _sub(request: Request) -> str:
    claims = getattr(request.state, "principal", None) or {}
    return str(claims.get("sub") or claims.get("sid") or "")


async def _calendar_context(request: Request, question: str, account_id: str | None) -> Any:
    """Calendar-action context, built only when the cheap gate matches so an
    ordinary question reads nothing extra."""
    if not (looks_like_calendar_delete(question) or looks_like_calendar_create(question)):
        return None
    return await build_calendar_context(request.app.state, sub=_sub(request), account_id=account_id)


RETRIEVAL_TIMEOUT_S = 6.0


async def _retrieval(
    request: Request, svc: AskService, question: str, body: AskRequest, calendar: Any
) -> RetrievalResult | None:
    """Search the user's own records for this question.

    Skipped when the caller supplied grounding context and unless the transport is the secure local route (NS-ADR-108):
    personal records never go to any other transport.
    """
    if not svc.personal_allowed or not svc.transport_available:
        return None
    if body.context is not None and body.context.fixture_refs:
        return None
    try:
        history = await _history(request, body.session_id)
        return await asyncio.wait_for(
            retrieve(request.app.state, question, history=history), RETRIEVAL_TIMEOUT_S
        )
    except Exception as exc:  # noqa: BLE001 - Ask still answers, without records
        log.warning("ask.retrieval_failed", error=type(exc).__name__)
        return None


HISTORY_TURNS = 4


async def _history(request: Request, session_id: str | None) -> list[dict[str, Any]]:
    """The last few turns of the caller's saved Ask session (existing store, no new storage).
    Only role, text and cited record ids are kept. Missing account, session or store: standalone."""
    if not session_id:
        return []
    try:
        from backend.api.endpoints.ask_sessions import _key, _repo

        raw = await _repo(request).get_preference(_key(request))
    except Exception:  # noqa: BLE001 - no history is the same as a standalone question
        return []
    sessions = raw.get("sessions") if isinstance(raw, dict) else None
    for saved in sessions or []:
        if isinstance(saved, dict) and saved.get("id") == session_id:
            messages = [m for m in saved.get("messages") or [] if isinstance(m, dict)]
            return [
                {
                    "role": m.get("role"),
                    "content": str(m.get("content") or "")[:2000],
                    "citations": m.get("citations") or [],
                    "records": m.get("records") or [],
                }
                for m in messages[-HISTORY_TURNS:]
            ]
    return []


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


#: Seconds of silence before a keep-alive comment. A model can take minutes to
#: load; a comment line keeps every hop (and the browser) from treating the
#: open stream as dead. SSE clients ignore lines starting with ":".
KEEPALIVE_S = 5.0
_STREAM_END = object()


async def _with_keepalive(
    make: Any, interval: float | None = None
) -> AsyncIterator[str]:
    """Run the blocking ask generator in a worker thread; yield its events as
    SSE frames and a ``: keep-alive`` comment whenever it is quiet."""
    wait = KEEPALIVE_S if interval is None else interval
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue()
    stop = threading.Event()

    def produce() -> None:
        try:
            for name, data in make():
                if stop.is_set():
                    return
                loop.call_soon_threadsafe(queue.put_nowait, _sse(name, data))
        except Exception as exc:  # noqa: BLE001 - one safe error event, no detail
            log.warning("ask.stream_producer_failed", error=type(exc).__name__)
            frame = _sse(
                "error", {"code": "ask_failed", "message": "The answer could not be produced."}
            )
            loop.call_soon_threadsafe(queue.put_nowait, frame)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _STREAM_END)

    worker = threading.Thread(target=produce, name="ask-stream", daemon=True)
    worker.start()
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=wait)
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if item is _STREAM_END:
                return
            yield item
    finally:
        stop.set()  # client gone: the worker stops at its next event


@router.post("/ask/stream")
async def ask_stream(request: Request, body: AskRequest) -> Any:
    """Streaming ask (server-sent events).

    Events: ``meta`` {request_id}, ``delta`` {text}, ``proposal`` {...},
    ``done`` {the body ``POST /api/context/ask`` returns} and ``error``
    {code, message}. An empty question is the same 422 as ``/ask``.
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
                "inference": _resolve_audit(request, body.agent, body.inference),
                "result": None,
            },
        )
    svc = _ask_service(request)
    if body.agent is not None and body.agent != "":
        await _hydrate_agent_binding(request, body.agent)
    calendar = await _calendar_context(request, question, body.account_id)
    retrieval = await _retrieval(request, svc, question, body, calendar)
    ask_request = AskRequest(
        question=question,
        context=body.context,
        agent=body.agent,
        inference=body.inference,
        account_id=body.account_id,
        session_id=body.session_id,
    )

    return StreamingResponse(
        _with_keepalive(lambda: svc.ask_stream(ask_request, calendar=calendar, retrieval=retrieval)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _resolve_audit(
    request: Request, agent: str | None, inference: AskInference | None
) -> dict[str, str]:
    """Echo the resolved inference audit for the invalid_request 422 shape."""
    svc = _ask_service(request)
    return svc._resolve_inference(agent, inference)


async def _hydrate_agent_binding(request: Request, agent_id: str) -> None:
    """Mirror a persisted agent's ``inference`` jsonb into the process-wide
    registry's in-process binding store (contract §2 step 2).

    Only native agent *type* manifests are mirrored (their binding lives on the
    manifest via ``AgentRegistry.set_inference``); a persisted *instance* id is
    mirrored via ``AgentRegistry.set_agent_binding``. When no DB engine is wired
    (``NERVE_DB_URL`` unset) the agent is assumed to be a native type and the
    resolution falls through to the manifest binding — no error is raised for a
    missing engine, keeping the endpoint functional in fixture-only / no-DB
    deployments.
    """
    from backend.api.endpoints.agents import _agent_service

    svc = _agent_service(request)
    registry = get_agent_registry()
    if registry.get(agent_id) is not None:
        # ``agent_id`` is a native type — its binding lives on the manifest
        # (set via ``AgentRegistry.set_inference``); nothing to hydrate.
        return
    if svc is None:
        # No DB engine — assume native type, fall through to manifest binding.
        return
    binding = await svc.get_inference(agent_id)
    if binding is not None:
        registry.set_agent_binding(agent_id, binding)
