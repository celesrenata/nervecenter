"""Voicemail endpoints (prompt2 Phase P2 — Personal Operations Plane).

Contract:
- ``GET  /api/voicemail``                       → reconstructed threads
- ``POST /api/voicemail/ingest``                → ingest one normalized
                                                   voicemail from the Personal
                                                   Relay (201 new / 200 dup)
- ``GET  /api/voicemail/{thread_id}``          → one thread (the thread
                                                   summary + its voicemails,
                                                   chronological)
- ``POST /api/voicemail/{thread_id}/acknowledge`` → mark read + resolve its
                                                   AttentionItem

There is deliberately **no** draft/reply endpoint: spec §77 has no reply
drafting for voicemail — the callback-request detection only flags the thread
and escalates its AttentionItem.

The store lives in the process-wide :func:`backend.voicemail.get_voicemail_service`
singleton (bound onto ``app.state.voicemail_service`` at startup). It is
*always* available — an empty inbox returns an empty list. Ingestion produces
AttentionItems through the shared P0 :class:`AttentionService` scorer, so the
Voicemail→Attention link is deterministic and explainable
(§21/§22/§77). The service only ever sees *normalized* voicemails — the
transcript and provenance are computed by the relay (out of scope, §46/§47).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from backend.api.models import (
    ApiResponseMeta,
    AttentionItem,
    ListResponse,
    Voicemail,
    VoicemailSource,
    VoicemailThread,
)
from backend.voicemail.service import VoicemailService, get_voicemail_service

router = APIRouter(prefix="/api/voicemail", tags=["voicemail"])


def _meta(**pagination: int | None) -> ApiResponseMeta:
    return ApiResponseMeta(
        version="1.0.0",
        request_id=f"req-{uuid4().hex[:12]}",
        timestamp=datetime.now(UTC),
        pagination={k: v for k, v in pagination.items() if v is not None} or None,
    )


def _service(request: Request) -> VoicemailService:
    svc = getattr(request.app.state, "voicemail_service", None)
    if svc is None:
        # Defensive: the singleton is always resolvable even if startup wiring
        # has not run yet (e.g. unit tests hitting the router directly).
        svc = get_voicemail_service()
        request.app.state.voicemail_service = svc
    return svc


def _thread_not_found(thread_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Thread {thread_id} not found.")


# -- request models ----------------------------------------------------------
class IngestVoicemailRequest(BaseModel):
    """One normalized voicemail as posted by the macOS Personal Relay (§77).

    The transcript and provenance are already computed by the relay; this is
    the NerveCenter-side contract only (§46/§47 out of scope).
    """

    voicemail_id: str
    caller_phone: str
    caller_name: str | None = None
    transcript: str = ""
    duration_seconds: int = 0
    received_at: datetime | None = None
    source: VoicemailSource = VoicemailSource.SHARE_SHEET
    # Read state as reported by the phone (mac-phone-store source).
    read: bool = False
    # Retraction: the voicemail was deleted on the phone; remove it here.
    deleted: bool = False


# -- response models ---------------------------------------------------------
class IngestVoicemailResponse(BaseModel):
    voicemail: Voicemail
    thread: VoicemailThread
    new_attention: AttentionItem | None = None
    duplicate: bool = False


class DeleteVoicemailResponse(BaseModel):
    voicemail_id: str
    deleted: bool = True
    # False when the id was unknown (replayed retraction): a harmless no-op.
    found: bool = False
    thread: VoicemailThread | None = None


class VoicemailThreadResponse(BaseModel):
    thread: VoicemailThread
    voicemails: list[Voicemail]


class AcknowledgeVoicemailResponse(BaseModel):
    thread: VoicemailThread
    attention_id: str | None = None
    resolved: bool = False


# -- routes -----------------------------------------------------------------
@router.get("", response_model=ListResponse)
@router.get("/", response_model=ListResponse)
async def list_threads(request: Request) -> ListResponse:
    """List reconstructed threads, most recently active first (§77)."""
    svc = _service(request)
    threads = svc.list_threads()
    total = len(threads)
    return ListResponse(
        data=[t.model_dump(mode="json") for t in threads],
        meta=_meta(total=total, page=1, per_page=total or 0),
    )


@router.post("/ingest", response_model=IngestVoicemailResponse | DeleteVoicemailResponse)
async def ingest_voicemail(
    request: Request, payload: IngestVoicemailRequest, response: Response
) -> IngestVoicemailResponse | DeleteVoicemailResponse:
    """Ingest one normalized voicemail. 201 on a new voicemail, 200 on a
    replayed duplicate id (the relay replays queued events on reconnect — §48).
    With ``deleted: true`` the voicemail is removed instead (200, also when the
    id is unknown, so a replayed retraction is harmless)."""
    svc = _service(request)
    if payload.deleted:
        outcome = svc.delete(payload.voicemail_id)
        response.status_code = 200
        return DeleteVoicemailResponse(
            voicemail_id=payload.voicemail_id,
            found=outcome["found"],
            thread=outcome["thread"],
        )
    result = svc.ingest(payload.model_dump())
    response.status_code = 200 if result["duplicate"] else 201
    return IngestVoicemailResponse(
        voicemail=result["voicemail"],
        thread=result["thread"],
        new_attention=result["new_attention"],
        duplicate=result["duplicate"],
    )


@router.get("/{thread_id}", response_model=VoicemailThreadResponse)
async def get_thread(request: Request, thread_id: str) -> VoicemailThreadResponse:
    """One thread: the thread summary plus its voicemails, chronological."""
    svc = _service(request)
    thread = svc.get_thread(thread_id)
    if thread is None:
        raise _thread_not_found(thread_id)
    return VoicemailThreadResponse(thread=thread, voicemails=svc.get_voicemails(thread_id))


@router.post("/{thread_id}/acknowledge", response_model=AcknowledgeVoicemailResponse)
async def acknowledge_thread(request: Request, thread_id: str) -> AcknowledgeVoicemailResponse:
    """Mark the thread read; resolves its AttentionItem if one is live."""
    svc = _service(request)
    thread = svc.acknowledge(thread_id)
    if thread is None:
        raise _thread_not_found(thread_id)
    return AcknowledgeVoicemailResponse(
        thread=thread,
        attention_id=thread.attention_id,
        resolved=thread.attention_id is not None,
    )
