"""Financial helper endpoints (NS-ADR-102). Behind SSO.

Read-only by design: accounts are typed in by the owner and statements are
uploaded files. There is no route that pays, transfers or contacts a bank.
"""
from __future__ import annotations

import datetime as dt
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.finance import parsers, planner, reminders, service
from backend.finance.service import INSTITUTIONS, KINDS

router = APIRouter(prefix="/api/finance", tags=["finance"])

Institution = Literal["BECU", "KeyBank", "Chase", "Apple Card", "PayPal", "other"]
Kind = Literal["credit_card", "loan", "line_of_credit", "other"]
assert set(INSTITUTIONS) == set(Institution.__args__) and set(KINDS) == set(Kind.__args__)


class AccountIn(BaseModel):
    institution: Institution
    kind: Kind = "credit_card"
    name: str = Field(min_length=1, max_length=120)
    balance_cents: int = Field(ge=0, le=10**12)
    apr_pct: str = Field(default="0", max_length=16)
    min_payment_cents: int = Field(default=0, ge=0, le=10**12)
    due_day: int | None = Field(default=None, ge=1, le=31)
    as_of: dt.date | None = None


class AccountPatch(BaseModel):
    institution: Institution | None = None
    kind: Kind | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    balance_cents: int | None = Field(default=None, ge=0, le=10**12)
    apr_pct: str | None = Field(default=None, max_length=16)
    min_payment_cents: int | None = Field(default=None, ge=0, le=10**12)
    due_day: int | None = Field(default=None, ge=1, le=31)
    as_of: dt.date | None = None


def _engine(request: Request) -> Any:
    engine = getattr(request.app.state, "db_engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="durable storage is not configured")
    return engine


@router.get("/accounts")
async def list_accounts(request: Request) -> dict[str, Any]:
    return {"accounts": await service.list_accounts(_engine(request))}


@router.post("/accounts", status_code=201)
async def create_account(body: AccountIn, request: Request) -> dict[str, Any]:
    try:
        return await service.create_account(
            _engine(request), **body.model_dump(exclude_none=True)
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.patch("/accounts/{account_id}")
async def update_account(account_id: str, body: AccountPatch, request: Request) -> dict[str, Any]:
    fields = body.model_dump(exclude_unset=True)
    try:
        row = await service.update_account(_engine(request), account_id, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if row is None:
        raise HTTPException(status_code=404, detail="account not found")
    return row


@router.delete("/accounts/{account_id}", status_code=204)
async def delete_account(account_id: str, request: Request) -> None:
    if not await service.delete_account(_engine(request), account_id):
        raise HTTPException(status_code=404, detail="account not found")


@router.post("/imports")
async def import_statement(
    request: Request,
    institution: Institution = Query(...),  # noqa: B008
    account_id: str | None = Query(default=None, max_length=64),
) -> dict[str, Any]:
    """Raw file body (CSV, OFX or QFX). The name and content are never logged."""
    engine = _engine(request)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > parsers.MAX_BYTES:
        raise HTTPException(status_code=413, detail="file is too large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > parsers.MAX_BYTES:
            raise HTTPException(status_code=413, detail="file is too large")
        chunks.append(chunk)
    try:
        return await service.import_statement(
            engine, b"".join(chunks), institution=institution, account_id=account_id
        )
    except OverflowError:
        raise HTTPException(status_code=413, detail="file is too large") from None
    except parsers.StatementError as exc:
        raise HTTPException(status_code=422, detail=f"not a readable statement: {exc}") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/imports")
async def list_imports(request: Request) -> dict[str, Any]:
    return {"imports": await service.list_imports(_engine(request))}


@router.get("/ledger")
async def ledger(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    return await service.list_ledger(_engine(request), limit=limit, offset=offset)


@router.get("/plan")
async def plan(
    request: Request, extra_cents: int = Query(default=0, ge=0, le=10**9)
) -> dict[str, Any]:
    debts = await service.debts(_engine(request))
    today = datetime.now(UTC).date()
    return planner.plan(debts, extra_cents=extra_cents, start=service.next_month_start(today))


@router.get("/reminders")
async def get_reminders(request: Request) -> dict[str, Any]:
    now = datetime.now(UTC)
    accounts = await service.list_accounts(_engine(request))
    out = reminders.card_reminders(accounts, now.date())
    registry = getattr(request.app.state, "repository_registry", None)
    if registry is not None:
        try:
            bills = await registry.bills.list_bills(limit=500)
            out += reminders.bill_reminders(list(bills), now)
        except Exception:  # noqa: BLE001 - house degradation: reminders without bills
            pass
        await reminders.mirror_card_attention(registry, out)
    out.sort(key=lambda r: (r["due_date"], -r["level"]))
    return {"reminders": out}
