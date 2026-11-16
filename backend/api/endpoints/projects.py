"""Shared project context endpoints (NS-ADR-104). Behind SSO.

Without a DB engine every route answers ``503``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend.inference.escalation import get_grants
from backend.inference.local_first import decide, max_local_failures
from backend.inference.openai_engine import (
    OpenAIEngine,
    OpenAIError,
    OpenAINotConfigured,
    get_engine,
)
from backend.inference.openai_limiter import OpenAIQueueTimeout, OpenAIThrottled
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
    # NS-ADR-107 (a): the owner explicitly asks for OpenAI on this question.
    use_openai: bool = False


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


def _local_transport(request: Request):
    existing = getattr(request.app.state, "projects_local_transport", None)
    if existing is None:
        from backend.inference.local_first import LocalFirstTransport
        from backend.inference.omniroute import OmniRouteTransport

        existing = LocalFirstTransport(OmniRouteTransport.from_env(), "projects_ask")
        request.app.state.projects_local_transport = existing
    return existing


async def _ask_openai(engine: OpenAIEngine, prompt: str, slug: str, reason: str) -> dict[str, Any]:
    try:
        out = await _guard(engine.generate(prompt, slug, caller="projects_ask", reason=reason))
    except OpenAINotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except (OpenAIQueueTimeout, OpenAIThrottled) as exc:
        raise HTTPException(status_code=429, detail=str(exc) or "OpenAI is rate limited") from None
    except OpenAIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return {**out, "engine": "openai", "reason": reason}


@router.post("/{slug}/ask")
async def ask(slug: str, body: AskIn, request: Request) -> dict[str, Any]:
    """Local models first (NS-ADR-107); OpenAI when asked for or after local fails."""
    service = _service(request)
    engine = get_engine(request.app)
    grants = get_grants(request.app)
    grant = await grants.get("project", slug) if grants else None
    owner = body.use_openai or grant == "owner_requested"
    first = decide(None, caller="projects_ask", background=False, owner_requested=owner)
    if first.escalated:
        await _guard(service.get(slug))  # 404 before spending a call
        return await _ask_openai(engine, body.prompt, slug, str(first.reason))
    messages = await _guard(engine.build_messages(body.prompt, slug))
    text = "\n\n".join(m["content"] for m in messages)
    transport = _local_transport(request)
    failures = 0
    while True:
        try:
            out = await asyncio.to_thread(transport.generate, first.model, text)
            return {
                "content": out.get("content", ""),
                "model": first.model,
                "project": slug,
                "input_tokens": out.get("input_tokens"),
                "output_tokens": out.get("output_tokens"),
                "engine": "local",
                "reason": None,
            }
        except Exception:  # noqa: BLE001 - counted, then maybe escalated
            failures += 1
        nxt = decide(None, caller="projects_ask", background=False, local_failures=failures)
        if nxt.escalated:
            if not engine.configured:
                raise HTTPException(
                    status_code=502, detail="local models failed and OpenAI is not configured"
                )
            return await _ask_openai(engine, body.prompt, slug, str(nxt.reason))
        if failures >= max_local_failures():
            raise HTTPException(status_code=502, detail="local models failed")
