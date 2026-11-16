"""Long-term researcher endpoints (NS-ADR-103). Behind SSO.

Without a DB engine every route answers ``503``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend.research import get_engine
from backend.research.engine import ResearchNotFoundError, ResearchStateError

router = APIRouter(prefix="/api/research", tags=["research"])


class ProjectIn(BaseModel):
    model_config = {"extra": "forbid"}
    title: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=4000)
    budget_hours: float = Field(default=4.0, gt=0, le=720)
    cadence_seconds: int = Field(default=300, ge=30, le=86_400)


class NotesIn(BaseModel):
    model_config = {"extra": "forbid"}
    notes: str = Field(max_length=20_000)


def _engine(request: Request):
    engine = get_engine(request.app)
    if engine is None:
        raise HTTPException(status_code=503, detail="durable storage is not configured")
    return engine


async def _guard(coro: Any) -> Any:
    try:
        return await coro
    except ResearchNotFoundError:
        raise HTTPException(status_code=404, detail="Research project not found") from None
    except ResearchStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("", status_code=201)
async def create_project(body: ProjectIn, request: Request) -> dict[str, Any]:
    return await _engine(request).create(
        body.title, body.question, body.budget_hours, body.cadence_seconds
    )


@router.get("")
async def list_projects(request: Request) -> dict[str, Any]:
    return {"projects": await _engine(request).list_projects()}


@router.get("/{project_id}")
async def project_detail(project_id: str, request: Request) -> dict[str, Any]:
    return await _guard(_engine(request).detail(project_id))


@router.post("/{project_id}/pause")
async def pause_project(project_id: str, request: Request) -> dict[str, Any]:
    return await _guard(_engine(request).pause(project_id))


@router.post("/{project_id}/resume")
async def resume_project(project_id: str, request: Request) -> dict[str, Any]:
    return await _guard(_engine(request).resume(project_id))


@router.put("/{project_id}/notes")
async def put_notes(project_id: str, body: NotesIn, request: Request) -> dict[str, Any]:
    return await _guard(_engine(request).set_notes(project_id, body.notes))


@router.get("/{project_id}/report")
async def project_report(project_id: str, request: Request) -> dict[str, Any]:
    return await _guard(_engine(request).report(project_id))
