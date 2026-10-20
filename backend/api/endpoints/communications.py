"""Communications endpoints (gaps4 R101 — normalized cross-channel records).

Contract:
- ``GET  /api/communications``                  → normalized communications
                                                   (``?person_id=`` / ``?channel=``
                                                   filters, chronological)
- ``POST /api/communications/ingest``           → upsert one normalized
                                                   communication in the §7
                                                   shape (201 new / 200
                                                   update); folds the record
                                                   into its per-(person,
                                                   channel) cross-channel
                                                   conversation (§32)
- ``GET  /api/communications/{communication_id}`` → one normalized
                                                   communication

Communications never carry raw content: the record keeps only a
``content_ref`` pointer (raw content lives encrypted outside the knowledge
graph, §7) and a §36 ``sensitivity`` label whose routing metadata defaults
to ``LOCAL_ONLY`` (§36/§37). An unknown ``person_id`` is a 404; an
uncertain counterpart resolution leaves the communication unlinked rather
than guessing (§6 conservative resolution).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from backend.api.models import ApiResponseMeta, ListResponse
from backend.communications import (
    CommunicationChannel,
    CommunicationDirection,
    SensitivityLabel,
    get_communications_service,
)
from backend.communications.registry import CommunicationsService

router = APIRouter(prefix="/api/communications", tags=["communications"])


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


def _communication_not_found(communication_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Communication {communication_id} not found.")


# -- request models ----------------------------------------------------------


class IngestCommunicationRequest(BaseModel):
    """One normalized communication in the §7 shape.

    ``channel`` ∈ email|imessage|sms|rcs|voicemail and ``sensitivity`` is a
    §36 label; both are typed enums so an unknown value is a Pydantic 422,
    not a stored error. ``content_ref`` is a pointer to the encrypted
    payload — never the payload itself.
    """

    communication_id: str | None = None
    person_id: str | None = None
    channel: CommunicationChannel
    direction: CommunicationDirection
    sender: str
    recipients: list[str] = Field(default_factory=list)
    timestamp: datetime | None = None
    thread_id: str | None = None
    content_ref: str
    attachments: list[str] = Field(default_factory=list)
    classification: dict[str, Any] = Field(default_factory=dict)
    sensitivity: SensitivityLabel = SensitivityLabel.PERSONAL
    source: str = "personal-relay"


# -- endpoints -----------------------------------------------------------------


@router.get("")
async def list_communications(
    request: Request,
    person_id: str | None = None,
    channel: CommunicationChannel | None = None,
):
    """List normalized communications, optionally filtered by person and/or
    channel (chronological order)."""
    svc = _service(request)
    if person_id is not None and svc.get_person(person_id) is None:
        raise HTTPException(status_code=404, detail=f"Person {person_id} not found.")
    communications = svc.list_communications(person_id=person_id, channel=channel)
    data = [c.model_dump(mode="json") for c in communications]
    return ListResponse(data=data, meta=_meta(total=len(data)))


@router.post("/ingest", status_code=201, response_model=None)
async def ingest_communication(
    request: Request, payload: IngestCommunicationRequest, response: Response
):
    """Upsert one normalized communication and fold it into its conversation.

    201 for a new ``communication_id`` (or a fresh derived id), 200 when the
    same id is re-ingested (idempotent update). When ``person_id`` is
    omitted the counterpart value is resolved against the identity index —
    only an exact canonical match links the record; uncertain or unknown
    counterpart values leave ``person_id`` null (conservative, §6).
    """
    svc = _service(request)
    try:
        result = svc.ingest_communication(payload.model_dump(mode="json"))
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"Person {exc.args[0]} not found."
        ) from exc

    response.status_code = 201 if result["created"] else 200
    communication = result["communication"]
    return {
        "item": communication.model_dump(mode="json"),
        "conversation": result["conversation"].model_dump(mode="json"),
        "created": result["created"],
        "meta": _meta(),
    }


@router.get("/{communication_id}")
async def get_communication(request: Request, communication_id: str):
    """One normalized communication."""
    svc = _service(request)
    communication = svc.get_communication(communication_id)
    if communication is None:
        raise _communication_not_found(communication_id)
    return {"item": communication.model_dump(mode="json"), "meta": _meta()}
