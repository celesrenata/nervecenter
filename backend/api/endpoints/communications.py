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

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from backend.api.models import ApiResponseMeta, ListResponse
from backend.communications import (
    CommunicationChannel,
    CommunicationDirection,
    SensitivityLabel,
    get_communications_service,
)
from backend.communications.detection.wiring import get_pipeline
from backend.communications.registry import CommunicationsService
from backend.communications.search import search_communications
from backend.ingest.mail_worker import get_communications_store

router = APIRouter(prefix="/api/communications", tags=["communications"])

log = structlog.get_logger(__name__)


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


def _durable_endpoint_communication_id(communication_id: str) -> str:
    """Deterministic durable ``communications.id`` for an endpoint-ingested
    record (P1-107) so a re-ingest is an idempotent get-or-create."""
    digest = hashlib.sha256(
        f"endpoint-comm:{communication_id}".encode("utf-8")
    ).hexdigest()[:24]
    return f"dcomm-ep-{digest}"


async def _persist_communication_durably(communication: Any) -> None:
    """Best-effort durable persist of one endpoint-ingested communication
    (P1-107 step 2).

    No-op when no durable store is wired, or when the record did not resolve
    to a person (the durable FK is ``NOT NULL`` — an unlinked record has no
    durable home, matching the conservative §6 contract). The in-memory
    person id is pinned as the durable ``people.id`` so the two planes agree.
    A durable failure is logged, never raised: the request contract (the
    in-memory upsert) is unchanged, mirroring the commitment seam.
    """
    store = get_communications_store()
    if store is None or communication.person_id is None:
        return
    try:
        from backend.ingest.mail_worker import persist_communication_durably

        svc = get_communications_service()
        person = svc.get_person(communication.person_id)
        display_name = person.display_name if person is not None else None
        # Derive the identity handle from the counterpart value so the durable
        # identity row mirrors the in-memory link.
        handle = (
            communication.sender
            if communication.direction is CommunicationDirection.INCOMING
            else (communication.recipients[0] if communication.recipients else communication.sender)
        )
        await persist_communication_durably(
            store,
            person_id=communication.person_id,
            display_name=display_name,
            identity_kind=communication.channel.value,
            identity_handle=str(handle or ""),
            channel=communication.channel.value,
            direction=communication.direction.value,
            content_ref=communication.content_ref,
            subject=str((communication.classification or {}).get("subject") or "") or None,
            received_at=communication.timestamp,
            communication_id=_durable_endpoint_communication_id(
                communication.communication_id
            ),
        )
    except Exception as exc:  # noqa: BLE001 - best-effort durable mirror
        log.warning("communications.durable_persist_failed", error=str(exc))


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


def _durable_row_to_communication(row: Any) -> dict[str, Any]:
    """Map a durable :class:`CommunicationRow` into the frozen
    :class:`Communication` response shape (P1-107 step 4 — restart reads).

    The durable row keeps only the ref-shaped subset (§7). Fields the durable
    plane does not carry (``sender`` / ``recipients`` / ``thread_id`` /
    ``attachments``) are reconstructed conservatively — ``sender`` is not
    stored durably, so the ``subject`` is surfaced in ``classification`` and
    the counterpart fields default to empty. ``communication_id`` is the
    durable row id so the shape stays a valid :class:`Communication`.
    """
    from backend.communications.models import Communication

    comm = Communication(
        communication_id=str(row.id),
        person_id=row.person_id,
        channel=CommunicationChannel(str(row.channel)),
        direction=CommunicationDirection(str(row.direction)),
        sender="",
        recipients=[],
        timestamp=row.received_at,
        thread_id=None,
        content_ref=row.content_ref,
        classification={"subject": row.subject} if row.subject else {},
        sensitivity=(
            SensitivityLabel(str(row.sensitivity))
            if row.sensitivity
            else SensitivityLabel.PERSONAL
        ),
    )
    return comm.model_dump(mode="json")


async def _durable_first_communications(
    request: Request,
    svc: CommunicationsService,
    *,
    person_id: str | None,
    channel: CommunicationChannel | None,
) -> list[dict[str, Any]] | None:
    """Durable-first merge of communications (P1-107 decision (a)).

    Precedence: the durable store is authoritative for existence — every
    durable row is returned, reconstructed into the frozen shape, correlated
    to the in-memory record by ``content_ref`` so a record live in BOTH
    planes is returned once, using the richer in-memory representation
    (sender/recipients/thread_id intact). In-memory-only records (not yet
    settled durably) are merged in afterward. A restarted pod — empty
    in-memory plane — therefore serves exactly the durably-persisted rows.

    Returns ``None`` when no durable registry is wired, so the caller falls
    back to the pure in-memory read (unchanged no-DB / unit-test contract).
    """
    registry = getattr(request.app.state, "repository_registry", None)
    store = get_communications_store()
    if registry is None or store is None:
        return None

    durable_rows = await store.list_communications(person_id=person_id)
    in_memory = svc.list_communications(person_id=person_id, channel=channel)
    by_ref = {c.content_ref: c for c in in_memory}

    merged: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    for row in durable_rows:
        if channel is not None and str(row.channel) != channel.value:
            continue
        live = by_ref.get(row.content_ref)
        if live is not None:
            merged.append(live.model_dump(mode="json"))
            seen_refs.add(row.content_ref)
        else:
            merged.append(_durable_row_to_communication(row))
            seen_refs.add(row.content_ref)
    # In-memory-only records (not yet durably settled) keep showing up.
    for comm in in_memory:
        if comm.content_ref not in seen_refs:
            merged.append(comm.model_dump(mode="json"))

    merged.sort(key=lambda c: (str(c.get("timestamp") or ""), str(c.get("communication_id") or "")))
    return merged


@router.get("")
async def list_communications(
    request: Request,
    person_id: str | None = None,
    channel: CommunicationChannel | None = None,
):
    """List normalized communications, optionally filtered by person and/or
    channel (chronological order).

    Durable-first (P1-107 step 4): when the durable plane is wired the
    response is served from the durable store merged with the in-memory
    registry (durable authoritative for existence; in-memory representation
    preferred when a record is live in both). A restarted pod serves exactly
    what it persisted. Without a durable plane this is the pure in-memory
    read (unchanged).
    """
    svc = _service(request)
    if person_id is not None and svc.get_person(person_id) is None:
        # Fall back to the durable person existence check when the in-memory
        # plane is cold (restart): an unknown durable person is still a 404.
        store = get_communications_store()
        durable_person = (
            await store.get_person(person_id) if store is not None else None
        )
        if durable_person is None:
            raise HTTPException(status_code=404, detail=f"Person {person_id} not found.")
    data = await _durable_first_communications(
        request, svc, person_id=person_id, channel=channel
    )
    if data is None:
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
    # P1-107 step 2 — persist the communication durably when a durable store
    # is wired (startup binds ``RepositoryRegistry.communications`` via
    # ``set_communications_store``) AND the record resolved to a person. The
    # in-memory person id is pinned as the durable ``people.id`` so both
    # planes name the same person; the durable communication id is derived
    # from the normalized communication id so a re-ingest is an idempotent
    # get-or-create (no duplicate row). No durable store (no-DB) → in-memory
    # only, unchanged. A durable failure must not fail the request contract
    # (best-effort mirror, same as the commitment seam), so it is logged.
    await _persist_communication_durably(communication)
    # R106 end-to-end seam (§61): after persist, run the detection →
    # follow-up → attention pass over the normalized record. Pure
    # orchestration on the process-wide singletons; a missing transient
    # content registration degrades to empty text, never an error.
    get_pipeline().ingest(communication, now=communication.timestamp)
    return {
        "item": communication.model_dump(mode="json"),
        "conversation": result["conversation"].model_dump(mode="json"),
        "created": result["created"],
        "meta": _meta(),
    }


@router.get("/search")
async def search_endpoint(
    request: Request,
    q: str,
    person_id: str | None = None,
    channel: CommunicationChannel | None = None,
    within_week: bool = True,
):
    """R110 deterministic cross-channel search (P1-107 step 5, §50).

    Delegates to :func:`backend.communications.search.search_communications`
    over the canonical communications registry (durable-backed after step 4 —
    the ingested records live in the in-memory plane the search reads, and the
    durable store is their authoritative home). Declared before the
    ``/{communication_id}`` route so the literal path is not captured by the
    path parameter. Query-param names mirror the other communications routes
    (``person_id`` / ``channel``). The result is the frozen
    :class:`~backend.communications.search.CommunicationSearchResult` shape
    (metadata + ``content_ref`` only — never raw content, §7).
    """
    # Ensure the registry is resolvable (same defensive wiring as the other
    # routes) so the module-level singleton the search service reads matches.
    _service(request)
    result = search_communications(
        q, person_id=person_id, channel=channel, within_week=within_week
    )
    return {"item": result.model_dump(mode="json"), "meta": _meta(total=result.total)}


@router.get("/{communication_id}")
async def get_communication(request: Request, communication_id: str):
    """One normalized communication."""
    svc = _service(request)
    communication = svc.get_communication(communication_id)
    if communication is None:
        raise _communication_not_found(communication_id)
    return {"item": communication.model_dump(mode="json"), "meta": _meta()}
