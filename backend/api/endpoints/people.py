"""People endpoints (gaps4 R101 — canonical unified person model, prompt2 §5–§7).

Contract:
- ``GET  /api/people``                          → all canonical persons
- ``POST /api/people/identities/ingest``        → link one normalized identity
                                                   value to its canonical person
                                                   (201 new person / 200 upsert)
- ``GET  /api/people/{person_id}``              → one canonical person
- ``POST /api/people/{person_id}/vip``          → set the VIP flag (§12); the
                                                   existing VIP contacts state
                                                   delegates here (R101 step 5)
- ``POST /api/people/{person_id}/merge``        → explicit merge of
                                                   ``merge_person_id`` into
                                                   ``person_id`` (§6: merges
                                                   require an explicit action;
                                                   uncertain same-name /
                                                   different-value matches are
                                                   *never* joined implicitly)

The store lives in the process-wide
:func:`backend.communications.get_communications_service` singleton (bound
onto ``app.state.communications_service`` at startup). Identity records are
JSON-safe by construction: they carry only the normalized value, the trimmed
raw value, an optional display name, provenance, and timestamps — never raw
message content or credentials (§6/§7).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from backend.api.models import ApiResponseMeta, ListResponse
from backend.communications import (
    ContactIdentityKind,
    get_communications_service,
)
from backend.communications.models import IdentityMatchStatus
from backend.communications.registry import CommunicationsService
from backend.persistence.repositories.registry import RepositoryRegistry

router = APIRouter(prefix="/api/people", tags=["people"])


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


def _person_not_found(person_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Person {person_id} not found.")


def _relationships_service(request: Request):
    """The durable People & Relationships service (spec §11, workstream J),
    lazily built from the durable repository registry and cached on
    ``app.state``.

    The relationships plane is durable-backed (workstream-F repositories).
    Without ``NERVE_DB_URL`` there is no registry, so the routes answer an
    honest ``503 relationships_unavailable`` — never fabricated data.
    """
    cached = getattr(request.app.state, "relationships_service", None)
    if cached is not None:
        return cached
    registry = getattr(request.app.state, "repository_registry", None)
    if not isinstance(registry, RepositoryRegistry):
        raise HTTPException(
            status_code=503,
            detail="relationships_unavailable: the durable database is not configured",
        )
    from backend.communications.relationships_service import RelationshipsService

    svc = RelationshipsService(
        communications=registry.communications,
        commitments=registry.commitments,
        calendar=registry.calendar,
        session_factory=registry.session_factory,
        in_memory=get_communications_service(),
    )
    request.app.state.relationships_service = svc
    return svc


# -- request models ----------------------------------------------------------


class IngestIdentityRequest(BaseModel):
    """One normalized identity value to link to its canonical person (§6).

    ``kind`` is typed as :class:`ContactIdentityKind` and the canonical
    value is value-first (AC-R02): an equal canonical value resolves to the
    same person, while a same-name / different-value result is a separate
    person flagged ``PROPOSED``.
    """

    kind: ContactIdentityKind
    value: str
    person_id: str | None = None
    display_name: str | None = None
    source: str = "personal-relay"
    timestamp: datetime | None = None


class VipRequest(BaseModel):
    """VIP flag update (§12 — same contract as ``/api/contacts/{id}/vip``)."""

    is_vip: bool


class MergePeopleRequest(BaseModel):
    """Explicit merge: ``merge_person_id`` is folded into the path person."""

    merge_person_id: str


class PersonPatchRequest(BaseModel):
    """Editable durable relationship fields (spec §11 — frozen PATCH).

    Every field is optional; ``None`` means "leave as-is". ``notes`` /
    ``birthdays`` / ``boundaries`` are full *replaces* of the stored bag
    (the surface is an editor, not an event log). No scoring fields
    exist — §11 assistance reflects recorded facts, it does not evaluate.
    """

    is_vip: bool | None = None
    notes: list[Any] | None = None
    boundaries: list[Any] | None = None
    birthdays: list[dict[str, Any]] | None = None


class ReminderRequest(BaseModel):
    """One follow-up reminder linked to the person (spec §11)."""

    text: str
    due_at: datetime | None = None
    source_ref: str | None = None


# -- endpoints -----------------------------------------------------------------


@router.get("")
async def list_people(request: Request):
    """List all canonical persons (stable, display-name ordering)."""
    svc = _service(request)
    people = [p.model_dump(mode="json") for p in svc.list_people()]
    return ListResponse(data=people, meta=_meta(total=len(people)))


@router.post("/identities/ingest", status_code=201, response_model=None)
async def ingest_identity(request: Request, payload: IngestIdentityRequest, response: Response):
    """Link one identity value to its canonical person (idempotent upsert).

    201 when a new person was created (fresh value, or a *proposed*
    separate person), 200 when the equal canonical value already linked a
    person (AC-R01 — no duplicate). The response ``status`` explains the
    resolution (RESOLVED / PROPOSED / UNLINKED) and any uncertain
    ``proposed_candidates`` that remain separate until an explicit merge.
    """
    svc = _service(request)
    try:
        result = svc.ingest_identity(payload.model_dump(mode="json"))
    except KeyError as exc:
        raise _person_not_found(str(exc.args[0])) from exc
    except ValueError as exc:  # bad kind/value → 422 boundary
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    response.status_code = 201 if result["created"] else 200
    person = result["person"]
    return {
        "person": person.model_dump(mode="json"),
        "identity": result["identity"].model_dump(mode="json"),
        "created": result["created"],
        "status": result["status"].value if isinstance(result["status"], IdentityMatchStatus) else result["status"],
        "proposed_candidates": result["proposed_candidates"],
        "meta": _meta(),
    }


@router.get("/{person_id}")
async def get_person(request: Request, person_id: str):
    """One canonical person with its normalized identity links.

    Durable augmentation (spec §11): when the relationships plane is
    configured (``NERVE_DB_URL`` set), the response also carries the
    factual ``relationships`` record — notes, boundaries, birthdays (with
    next occurrence), open commitments, promises made, unanswered
    conversations, follow-ups, and social plans. Unknown person → 404;
    no durable plane → the in-memory record only (``relationships``
    omitted), never fabricated data.
    """
    svc = _service(request)
    person = svc.get_person(person_id)
    if person is None:
        raise _person_not_found(person_id)
    payload: dict[str, Any] = {"item": person.model_dump(mode="json"), "meta": _meta()}
    registry = getattr(request.app.state, "repository_registry", None)
    if isinstance(registry, RepositoryRegistry):
        profile = await _relationships_service(request).person_profile(person_id)
        if profile.get("person") is not None:
            payload["relationships"] = profile
    return payload


@router.post("/{person_id}/vip")
async def set_person_vip(request: Request, person_id: str, payload: VipRequest):
    """Set the VIP flag on the canonical person (§12, R101 step 5)."""
    svc = _service(request)
    person = svc.get_person(person_id)
    if person is None:
        raise _person_not_found(person_id)
    person.is_vip = payload.is_vip
    person.updated_at = datetime.now(UTC)
    return {"item": person.model_dump(mode="json"), "meta": _meta()}


@router.post("/{person_id}/merge")
async def merge_people(request: Request, person_id: str, payload: MergePeopleRequest):
    """Explicitly merge ``merge_person_id`` into ``person_id`` (§6).

    The *only* way two uncertain same-name / different-value persons become
    one: identities, communications, and per-channel conversations fold into
    the surviving person; the source person is removed.
    """
    svc = _service(request)
    if person_id == payload.merge_person_id:
        raise HTTPException(status_code=422, detail="A person cannot be merged into itself.")
    if svc.get_person(person_id) is None:
        raise _person_not_found(person_id)
    if svc.get_person(payload.merge_person_id) is None:
        raise _person_not_found(payload.merge_person_id)
    merged = svc.merge_people(person_id, payload.merge_person_id)
    return {
        "item": merged.model_dump(mode="json"),
        "merged_from": payload.merge_person_id,
        "remaining_persons": len(svc.list_people()),
        "meta": _meta(),
    }


@router.patch("/{person_id}")
async def patch_person(
    request: Request, person_id: str, payload: PersonPatchRequest
) -> dict[str, Any]:
    """Edit the durable relationship fields for one person (spec §11).

    ``is_vip`` sets the VIP flag; ``notes`` / ``birthdays`` /
    ``boundaries`` are full replaces of the stored bag (editor semantics).
    Unknown person → 404; in-memory-only person → promoted to durable
    before the write. 503 when the durable plane is not configured.
    """
    svc = _relationships_service(request)
    try:
        if payload.is_vip is not None:
            await svc.set_vip(person_id, payload.is_vip)
        if payload.notes is not None:
            await svc.set_notes(person_id, payload.notes, mode="replace")
        if payload.boundaries is not None:
            await svc.set_boundaries(person_id, payload.boundaries)
        if payload.birthdays is not None:
            await svc.set_birthdays(person_id, payload.birthdays)
    except KeyError:
        raise _person_not_found(person_id) from None
    profile = await svc.person_profile(person_id)
    return {
        "item": profile["person"],
        "notes": profile["notes"],
        "boundaries": profile["boundaries"],
        "birthdays": profile["birthdays"],
        "meta": _meta(),
    }


@router.post("/{person_id}/reminders", status_code=201)
async def create_person_reminder(
    request: Request, person_id: str, payload: ReminderRequest
) -> dict[str, Any]:
    """Create a follow-up reminder linked to the person (spec §11).

    The reminder is a durable ``followups`` row; it carries the person FK
    and the optional source ref — no raw content, no diagnosis.
    """
    svc = _relationships_service(request)
    if not payload.text.strip():
        raise HTTPException(status_code=422, detail="reminder text must not be empty")
    try:
        reminder = await svc.create_reminder(
            person_id,
            payload.text,
            due_at=payload.due_at,
            source_ref=payload.source_ref,
        )
    except KeyError:
        raise _person_not_found(person_id) from None
    return {"item": reminder, "meta": _meta()}
