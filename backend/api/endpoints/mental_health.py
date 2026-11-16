"""Mental-health tracker endpoints (NS-ADR-101). All behind SSO; local only."""
from __future__ import annotations

import datetime as dt
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.mental_health import service
from backend.mental_health.crypto import EncryptionUnavailable, load_keyring
from backend.mental_health.instruments import MAX_COMMENT_CHARS, describe
from backend.storage.encrypted import ObjectDecryptionError

router = APIRouter(prefix="/api/mental-health", tags=["mental-health"])


class FormIn(BaseModel):
    instrument: Literal["phq9", "gad7"]
    answers: list[int] = Field(min_length=7, max_length=9)
    comment: str | None = Field(default=None, max_length=MAX_COMMENT_CHARS)
    date: dt.date | None = None


def _engine(request: Request) -> Any:
    engine = getattr(request.app.state, "db_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="durable storage is not configured")
    return engine


def _keyring() -> Any:
    try:
        return load_keyring()
    except EncryptionUnavailable:
        raise HTTPException(
            status_code=503, detail="encryption key is not configured; nothing was stored"
        ) from None


@router.get("/instruments")
async def instruments() -> dict[str, Any]:
    return {"instruments": describe()}


@router.post("/forms")
async def submit_form(body: FormIn, request: Request) -> dict[str, Any]:
    engine = _engine(request)
    keyring = _keyring()
    today = datetime.now(UTC).date()
    day = body.date or today
    if day > today + timedelta(days=1):
        raise HTTPException(status_code=422, detail="date is in the future")
    try:
        return await service.submit_form(
            engine, keyring, day=day, instrument=body.instrument,
            answers=body.answers, comment=body.comment,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/history")
async def history(request: Request, days: int = Query(default=30, ge=1, le=730)) -> dict[str, Any]:
    engine = _engine(request)
    keyring = _keyring()
    try:
        return {"days": days, "entries": await service.history(engine, keyring, days=days)}
    except ObjectDecryptionError:
        raise HTTPException(status_code=500, detail="stored entries could not be decrypted") from None  # noqa: E501


@router.get("/trends")
async def trends(request: Request, days: int = Query(default=90, ge=7, le=730)) -> dict[str, Any]:
    return await service.trends(_engine(request), days=days)


@router.get("/correlations")
async def correlations(
    request: Request, days: int = Query(default=90, ge=14, le=730)
) -> dict[str, Any]:
    return await service.correlations(_engine(request), days=days)
