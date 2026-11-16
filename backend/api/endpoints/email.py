"""Email endpoints (prompt2 Phase P0 — Personal Operations Plane).

Contract:
- ``GET  /api/email``                          → reconstructed threads
- ``POST /api/email/ingest``                   → ingest one normalized
                                                  email from the Personal
                                                  Relay (201 new / 200 dup)
- ``GET  /api/email/{thread_id}``              → one thread (the thread
                                                  summary + its messages,
                                                  chronological)
- ``POST /api/email/{thread_id}/acknowledge``  → mark read + resolve its
                                                  AttentionItem
- ``POST /api/email/{thread_id}/draft``        → suggested reply
                                                  (§13: AI drafts, human sends)

The store lives in the process-wide :func:`backend.email.get_email_service`
singleton (bound onto ``app.state.email_service`` at startup). It is
*always* available — an empty mailbox returns an empty list. Ingestion
produces AttentionItems through the shared P0 :class:`AttentionService`
scorer, so the Email→Attention link is deterministic and explainable
(§21/§22/§75).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from backend.api.models import (
    ApiResponseMeta,
    AttentionItem,
    EmailDirection,
    EmailMessage,
    EmailThread,
    ListResponse,
)
from backend.email.service import EmailService, get_email_service

router = APIRouter(prefix="/api/email", tags=["email"])


def _meta(**pagination: int | None) -> ApiResponseMeta:
    return ApiResponseMeta(
        version="1.0.0",
        request_id=f"req-{uuid4().hex[:12]}",
        timestamp=datetime.now(UTC),
        pagination={k: v for k, v in pagination.items() if v is not None} or None,
    )


def _service(request: Request) -> EmailService:
    svc = getattr(request.app.state, "email_service", None)
    if svc is None:
        # Defensive: the singleton is always resolvable even if startup wiring
        # has not run yet (e.g. unit tests hitting the router directly).
        svc = get_email_service()
        request.app.state.email_service = svc
    return svc


async def _refresh_from_durable(request: Request) -> None:
    """Pull newly stored mail into the screen's service (throttled, idempotent)."""
    state = request.app.state
    feed = getattr(state, "email_durable_feed", None)
    if feed is None:
        registry = getattr(state, "repository_registry", None)
        if registry is None:
            return
        from backend.email.durable_feed import DurableEmailFeed
        from backend.ingest.supervisor import _object_store_from_env

        feed = DurableEmailFeed(registry, _object_store_from_env(), _service(request))
        state.email_durable_feed = feed
    await feed.refresh()


def _thread_not_found(thread_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Thread {thread_id} not found.")


# -- request models ----------------------------------------------------------
class IngestEmailRequest(BaseModel):
    """One normalized email as posted by the macOS Personal Relay (§75)."""

    message_id: str
    direction: EmailDirection = EmailDirection.RECEIVED
    from_address: str
    to_address: str = ""
    sender_name: str | None = None
    subject: str = ""
    body: str = ""
    timestamp: datetime | None = None
    source: str = "personal-relay"


# -- response models ---------------------------------------------------------
class IngestEmailResponse(BaseModel):
    message: EmailMessage
    thread: EmailThread
    new_attention: AttentionItem | None = None
    duplicate: bool = False


class EmailThreadResponse(BaseModel):
    thread: EmailThread
    messages: list[EmailMessage]


class AcknowledgeEmailResponse(BaseModel):
    thread: EmailThread
    attention_id: str | None = None
    resolved: bool = False


class DraftEmailResponse(BaseModel):
    thread_id: str
    to: str | None = None
    draft: str | None = None
    basis: str


# -- routes -----------------------------------------------------------------
@router.get("", response_model=ListResponse)
@router.get("/", response_model=ListResponse)
async def list_threads(request: Request) -> ListResponse:
    """List reconstructed threads, most recently active first (§75)."""
    await _refresh_from_durable(request)
    svc = _service(request)
    threads = svc.list_threads()
    total = len(threads)
    return ListResponse(
        data=[t.model_dump(mode="json") for t in threads],
        meta=_meta(total=total, page=1, per_page=total or 0),
    )


@router.post("/ingest", response_model=IngestEmailResponse)
async def ingest_email(
    request: Request, payload: IngestEmailRequest, response: Response
) -> IngestEmailResponse:
    """Ingest one normalized email. 201 on a new email, 200 on a replayed
    duplicate id (the relay replays queued events on reconnect — §48)."""
    svc = _service(request)
    result = svc.ingest(payload.model_dump())
    response.status_code = 200 if result["duplicate"] else 201
    return IngestEmailResponse(
        message=result["message"],
        thread=result["thread"],
        new_attention=result["new_attention"],
        duplicate=result["duplicate"],
    )


@router.get("/{thread_id}", response_model=EmailThreadResponse)
async def get_thread(request: Request, thread_id: str) -> EmailThreadResponse:
    """One thread: the thread summary plus its messages, chronological."""
    svc = _service(request)
    thread = svc.get_thread(thread_id)
    if thread is None:
        raise _thread_not_found(thread_id)
    return EmailThreadResponse(thread=thread, messages=svc.get_messages(thread_id))


@router.post("/{thread_id}/acknowledge", response_model=AcknowledgeEmailResponse)
async def acknowledge_thread(request: Request, thread_id: str) -> AcknowledgeEmailResponse:
    """Mark the thread read; resolves its AttentionItem if one is live."""
    svc = _service(request)
    thread = svc.acknowledge(thread_id)
    if thread is None:
        raise _thread_not_found(thread_id)
    return AcknowledgeEmailResponse(
        thread=thread,
        attention_id=thread.attention_id,
        resolved=thread.attention_id is not None,
    )


@router.post("/{thread_id}/draft", response_model=DraftEmailResponse)
async def draft_reply_endpoint(request: Request, thread_id: str) -> DraftEmailResponse:
    """Suggested reply for the thread's latest inbound (§13: AI drafts, human
    sends). Nothing is sent from here."""
    svc = _service(request)
    if svc.get_thread(thread_id) is None:
        raise _thread_not_found(thread_id)
    result: dict[str, Any] = svc.draft_reply(thread_id)
    return DraftEmailResponse(**result)
