"""Saved Ask chats (``/api/ask/sessions``), one document per account.

The web Ask view keeps its tabs in memory and mirrors them here so they
survive closing the browser and follow the account to other devices. The
document lives in the durable ``preferences`` table under
``ask_sessions:{sub}``. Both routes require login (default-deny person gate);
a request without a subject is refused rather than sharing one bucket.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/api/ask", tags=["ask"])

MAX_SESSIONS = 50
MAX_MESSAGES = 400
MAX_CONTENT_CHARS = 50_000
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024


class SavedMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1, max_length=128)
    role: Literal["user", "assistant", "system"]
    content: str = Field(max_length=MAX_CONTENT_CHARS)
    timestamp: str = Field(max_length=64)


class SavedSession(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=128)
    title: str = Field(max_length=200)
    messages: list[SavedMessage] = Field(max_length=MAX_MESSAGES)
    updated_at: str | None = Field(default=None, max_length=64)


class SavedDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sessions: list[SavedSession] = Field(max_length=MAX_SESSIONS)
    active_id: str | None = Field(default=None, max_length=128)


class _MemoryPrefs:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    async def get_preference(self, key: str) -> dict[str, Any] | None:
        return self._data.get(key)

    async def set_preference(self, key: str, value: dict[str, Any]) -> None:
        self._data[key] = value


def _repo(request: Request) -> Any:
    repo = getattr(request.app.state, "ask_sessions_repo", None)
    if repo is None:
        registry = getattr(request.app.state, "repository_registry", None)
        if registry is not None:
            from backend.persistence.repositories.wellness_repo import WellnessRepository

            repo = WellnessRepository(registry.session_factory)
        else:
            repo = _MemoryPrefs()
        request.app.state.ask_sessions_repo = repo
    return repo


def _key(request: Request) -> str:
    claims = getattr(request.state, "principal", None)
    sub = str(claims.get("sub") or "") if isinstance(claims, dict) else ""
    if not sub:
        raise HTTPException(status_code=401, detail="a signed-in account is required")
    return f"ask_sessions:{sub}"


@router.get("/sessions")
async def get_sessions(request: Request) -> dict[str, Any]:
    raw = await _repo(request).get_preference(_key(request))
    if not isinstance(raw, dict):
        return {"sessions": [], "active_id": None, "saved_at": None}
    return {
        "sessions": raw.get("sessions") or [],
        "active_id": raw.get("active_id"),
        "saved_at": raw.get("saved_at"),
    }


@router.put("/sessions")
async def put_sessions(request: Request, body: SavedDocument) -> dict[str, Any]:
    key = _key(request)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_DOCUMENT_BYTES:
        raise HTTPException(status_code=413, detail="saved chats are too large")
    saved_at = datetime.now(UTC).isoformat()
    doc = {**body.model_dump(mode="json"), "saved_at": saved_at}
    await _repo(request).set_preference(key, doc)
    return {"saved_at": saved_at, "sessions": len(body.sessions)}
