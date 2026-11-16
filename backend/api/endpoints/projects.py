"""Shared project context endpoints (NS-ADR-104). Behind SSO.

Without a DB engine every route answers ``503``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend.inference.openai_engine import OpenAIEngine, OpenAIError, OpenAINotConfigured
from backend.projects import get_service
from backend.projects.service import (
    ProjectExistsError,
    ProjectInputError,
    ProjectNotFoundError,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectIn(BaseModel):
    model_config = {"extra": "forbid"}
    slug: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=20_000)


class ProjectPatch(BaseModel):
    model_config = {"extra": "forbid"}
    title: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=20_000)


class NoteIn(BaseModel):
    model_config = {"extra": "forbid"}
    body: str = Field(min_length=1, max_length=8000)


class AskIn(BaseModel):
    model_config = {"extra": "forbid"}
    prompt: str = Field(min_length=1, max_length=8000)


def _service(request: Request):
    service = get_service(request.app)
    if service is None:
        raise HTTPException(status_code=503, detail="durable storage is not configured")
    return service


async def _guard(coro: Any) -> Any:
    try:
        return await coro
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail="Project not found") from None
    except ProjectExistsError:
        raise HTTPException(
            status_code=409, detail="A project with that slug already exists"
        ) from None
    except ProjectInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/openai/status")
async def openai_status() -> dict[str, Any]:
    return OpenAIEngine().status()


@router.post("", status_code=201)
async def create_project(body: ProjectIn, request: Request) -> dict[str, Any]:
    return await _guard(_service(request).create(body.slug, body.title, body.summary))


@router.get("")
async def list_projects(request: Request) -> dict[str, Any]:
    return {"projects": await _service(request).list_projects()}


@router.get("/{slug}")
async def project_detail(slug: str, request: Request) -> dict[str, Any]:
    return await _guard(_service(request).get(slug))


@router.patch("/{slug}")
async def update_project(slug: str, body: ProjectPatch, request: Request) -> dict[str, Any]:
    return await _guard(_service(request).update(slug, body.title, body.summary))


@router.post("/{slug}/notes", status_code=201)
async def add_note(slug: str, body: NoteIn, request: Request) -> dict[str, Any]:
    return await _guard(_service(request).add_note(slug, "owner", body.body))


@router.post("/{slug}/ask")
async def ask_openai(slug: str, body: AskIn, request: Request) -> dict[str, Any]:
    service = _service(request)
    engine = getattr(request.app.state, "openai_engine", None) or OpenAIEngine(service)
    try:
        return await _guard(engine.generate(body.prompt, slug))
    except OpenAINotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except OpenAIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
