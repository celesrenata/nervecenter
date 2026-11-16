"""Personal API tokens (NS-ADR-124): list, create and revoke ``nct_`` tokens.

Every route needs a signed-in person.  A personal token cannot manage tokens
(it cannot mint or revoke others), and device credentials are refused.  The
plaintext token appears once, in the create response; only its SHA-256 hash is
stored.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend.api import auth_service, auth_sessions
from backend.api.auth import _get_ctx, current_access
from backend.api.endpoints.mail_security import _eligible_for_server_scopes

router = APIRouter(prefix="/api/auth/tokens", tags=["authentication"])

Claims = Annotated[dict[str, Any], Depends(current_access)]


class TokenCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    expires_in_days: int | None = Field(
        default=90,
        ge=1,
        le=3650,
        description="Days until the token expires; null means it never expires.",
    )


def _owner(claims: dict[str, Any]) -> str:
    """The signed-in person's subject; refuses personal tokens and devices."""
    if claims.get(auth_service.SSO_SOURCE_CLAIM) == "pat":
        raise HTTPException(
            status_code=403, detail="A personal token cannot manage tokens."
        )
    if not _eligible_for_server_scopes(claims):
        raise HTTPException(status_code=403, detail="A signed-in person is required.")
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        raise HTTPException(status_code=403, detail="A signed-in person is required.")
    return sub


def _view(row: Any) -> dict[str, Any]:
    def iso(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    return {
        "id": row.id,
        "name": row.name,
        "prefix": row.prefix,
        "created_at": iso(row.created_at),
        "expires_at": iso(row.expires_at),
        "last_used_at": iso(row.last_used_at),
        "revoked_at": iso(row.revoked_at),
    }


@router.get("")
async def list_tokens(request: Request, claims: Claims) -> dict[str, Any]:
    owner = _owner(claims)
    rows = await auth_sessions.list_pats(_get_ctx(request), owner)
    return {"tokens": [_view(r) for r in rows]}


@router.post("", status_code=201)
async def create_token(
    body: TokenCreateRequest, request: Request, claims: Claims
) -> dict[str, Any]:
    owner = _owner(claims)
    expires_at = (
        datetime.now(UTC) + timedelta(days=body.expires_in_days)
        if body.expires_in_days
        else None
    )
    raw, row = await auth_sessions.create_pat(
        _get_ctx(request), owner_sub=owner, name=body.name.strip(), expires_at=expires_at
    )
    return {**_view(row), "token": raw}


@router.delete("/{token_id}")
async def revoke_token(
    token_id: str, request: Request, claims: Claims
) -> dict[str, Any]:
    owner = _owner(claims)
    if not await auth_sessions.revoke_pat(
        _get_ctx(request), owner_sub=owner, token_id=token_id
    ):
        raise HTTPException(status_code=404, detail="token not found")
    return {"revoked": True, "id": token_id}
