"""Local-first policy, OpenAI usage and escalation grants (NS-ADR-107). Behind SSO."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend.inference.escalation import GrantInputError, get_grants
from backend.inference.local_first import local_default, max_local_failures
from backend.inference.openai_engine import get_engine
from backend.inference.openai_limiter import get_limiter

router = APIRouter(prefix="/api/model-policy", tags=["model-policy"])


class GrantIn(BaseModel):
    model_config = {"extra": "forbid"}
    subject_kind: str = Field(pattern="^(agent|project|handoff)$")
    subject_id: str = Field(min_length=1, max_length=128)
    mode: str = Field(pattern="^(owner_requested|high_stakes)$")


@router.get("")
async def policy_status(request: Request) -> dict[str, Any]:
    grants = get_grants(request.app)
    return {
        "local_model": local_default(),
        "max_local_failures": max_local_failures(),
        "openai": {**get_engine(request.app).status(), **await get_limiter(request.app).usage()},
        "grants": await grants.list() if grants else [],
    }


def _grants(request: Request):
    grants = get_grants(request.app)
    if grants is None:
        raise HTTPException(status_code=503, detail="durable storage is not configured")
    return grants


@router.put("/grants")
async def put_grant(body: GrantIn, request: Request) -> dict[str, Any]:
    try:
        return await _grants(request).set(body.subject_kind, body.subject_id, body.mode)
    except GrantInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.delete("/grants/{subject_kind}/{subject_id}", status_code=204)
async def delete_grant(subject_kind: str, subject_id: str, request: Request) -> None:
    await _grants(request).remove(subject_kind, subject_id)
