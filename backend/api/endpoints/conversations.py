"""Conversations endpoints (gaps4 R101 — cross-channel threads, prompt2 §32).

Contract:
- ``GET  /api/conversations``                     → cross-channel
                                                    conversations
                                                    (``?channel=`` filter)
- ``GET  /api/conversations/{conversation_id}``   → one conversation with
                                                    its communications in
                                                    chronological order

A conversation is one per (canonical person, channel): the normalized
communications of the same human on the same channel form a single evolving
interaction rather than unrelated inbox items (§32). Communications are
attached by the ingest plane (R102) and re-attributed on explicit person
merges (R101 ``/api/people/{id}/merge``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request

from backend.api.models import ApiResponseMeta, ListResponse
from backend.communications import (
    CommunicationChannel,
    get_communications_service,
)
from backend.communications.registry import CommunicationsService

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


def _meta(**pagination: int | None) -> ApiResponseMeta:
    return ApiResponseMeta(
        version="1.0.0",
        request_id=f"req-{uuid4().hex[:12]}",
        timestamp=datetime.now(UTC),
        pagination={k: v for k, v in pagination.items() if v is not None} or None,
    )


def _service(request: Request) -> CommunicationsService:
    svc = getattr(request.app.state, "communications_service", None)
    if svc is None:
        # Defensive: the singleton is always resolvable even if startup
        # wiring has not run yet (e.g. unit tests hitting the router
        # directly).
        svc = get_communications_service()
        request.app.state.communications_service = svc
    return svc


def _conversation_not_found(conversation_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Conversation {conversation_id} not found.")


# -- endpoints -----------------------------------------------------------------


@router.get("")
async def list_conversations(request: Request, channel: CommunicationChannel | None = None):
    """List cross-channel conversations, optionally filtered by channel."""
    svc = _service(request)
    conversations = svc.list_conversations(channel=channel)
    data = [c.model_dump(mode="json") for c in conversations]
    return ListResponse(data=data, meta=_meta(total=len(data)))


@router.get("/{conversation_id}")
async def get_conversation(request: Request, conversation_id: str):
    """One conversation: its thread metadata plus its communications in
    chronological order."""
    svc = _service(request)
    conversation = svc.get_conversation(conversation_id)
    if conversation is None:
        raise _conversation_not_found(conversation_id)
    communications = svc.get_conversation_communications(conversation_id)
    return {
        "item": conversation.model_dump(mode="json"),
        "communications": [c.model_dump(mode="json") for c in communications],
        "meta": _meta(),
    }
