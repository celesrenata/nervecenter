"""P2 durable end to end (P2-QA): email -> proposal -> Add bill -> Today ->
restart -> confirm paid -> handled, and an unpaid bill going stale.

Real repositories on a file-backed SQLite database, the real bills and Today
routers, the real mail proposer. A "restart" disposes the engine and builds a
new engine, registry and app over the same file. Today's clock comes from its
``_test_now`` seam, so nothing depends on the real date.
"""
from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from backend.api.models import MailConnector  # noqa: F401 - import-order guard
from backend.bills.mail_candidates import ENV_BILL_CANDIDATE_SCOPES, MailBillProposer
from backend.bills.service import register_bills_today_provider
from backend.mail_security.parser import parse_mime_evidence
from backend.persistence.connection import make_session_factory
from backend.persistence.models.bills import BillCandidateRow, BillRow
from backend.persistence.models.personal import AttentionItemRow
from backend.persistence.repositories import RepositoryRegistry

SCOPE = "conn:11111111-2222-3333-4444-555555555555"
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
TABLES = [BillRow.__table__, BillCandidateRow.__table__, AttentionItemRow.__table__]

BILL_MAIL = (
    b"From: City Power <billing@citypower.example>\r\nTo: me@example.test\r\n"
    b"Subject: Your City Power bill is ready\r\nMIME-Version: 1.0\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
    b"Amount due: $84.20. Payment due by November 20, 2026.\r\n"
)


async def _open(path: pathlib.Path, *, create: bool):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    if create:
        async with engine.begin() as conn:
            await conn.run_sync(BillRow.__table__.metadata.create_all, tables=TABLES)
    return engine, RepositoryRegistry(make_session_factory(engine))


def _app(registry: RepositoryRegistry, now: datetime) -> TestClient:
    from backend.api.endpoints import bills, today

    app = FastAPI()
    app.include_router(bills.router)
    app.include_router(today.router)
    app.state.repository_registry = registry
    app.state._test_now = now
    return TestClient(app)


def _today_bills(client: TestClient, section: str) -> list[dict]:
    body = client.get("/api/today").json()
    return [i for i in body[section] if i.get("kind") == "bill"]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(ENV_BILL_CANDIDATE_SCOPES, SCOPE)
    register_bills_today_provider()


async def test_email_bill_reaches_today_survives_restart_and_is_handled(tmp_path) -> None:
    db = tmp_path / "p2.db"
    engine, registry = await _open(db, create=True)
    # 1. a bill email arrives (LOW_PRIORITY): one proposal, no bill yet
    proposer = MailBillProposer(registry.bills, clock=lambda: NOW)
    status = await proposer.propose(SCOPE, content_ref="content://mail/c1/bill",
                                    evidence=parse_mime_evidence(BILL_MAIL),
                                    disposition="LOW_PRIORITY")
    assert status == "PROPOSED"
    c = _app(registry, NOW)
    assert _today_bills(c, "coming_up") == [], "a proposal is not on Today"
    # 2. the owner adds it
    cid = c.get("/api/bills/candidates").json()["candidates"][0]["id"]
    bill = c.post(f"/api/bills/candidates/{cid}/accept").json()["bill"]
    # 3. Today shows it as coming up
    (item,) = _today_bills(c, "coming_up")
    assert item["id"] == bill["id"] and "City Power" in item["title"]
    await engine.dispose()

    # 4. restart: a new engine, registry and app over the same database
    engine, registry = await _open(db, create=False)
    c = _app(registry, NOW)
    (item,) = _today_bills(c, "coming_up")
    assert item["id"] == bill["id"], "the bill survives a restart"
    assert c.get("/api/bills/candidates").json()["count"] == 0, "the proposal stays accepted"

    # 5. the owner confirms payment: it leaves coming_up and shows as handled
    assert c.post(f"/api/bills/{bill['id']}/confirm-paid").status_code == 200
    assert _today_bills(c, "coming_up") == []
    (done,) = _today_bills(c, "handled")
    assert done["state"] == "confirmed_paid"
    await engine.dispose()


async def test_an_unpaid_bill_past_its_due_date_goes_stale_not_paid(tmp_path) -> None:
    engine, registry = await _open(tmp_path / "p2s.db", create=True)
    proposer = MailBillProposer(registry.bills, clock=lambda: NOW)
    await proposer.propose(SCOPE, content_ref="content://mail/c1/bill2",
                           evidence=parse_mime_evidence(BILL_MAIL), disposition="LOW_PRIORITY")
    c = _app(registry, NOW)
    cid = c.get("/api/bills/candidates").json()["candidates"][0]["id"]
    bill = c.post(f"/api/bills/candidates/{cid}/accept").json()["bill"]
    await engine.dispose()

    # a week after the due date, nobody confirmed it. The HTTP sweep uses the
    # wall clock, so the sweep itself is driven with an explicit time here.
    from backend.bills.service import BillService

    engine, registry = await _open(tmp_path / "p2s.db", create=False)
    swept = await BillService(registry.bills).sweep_stale(now=NOW + timedelta(days=13))
    assert swept == 1
    late = _app(registry, NOW + timedelta(days=13))
    (row,) = [b for b in late.get("/api/bills/").json()["bills"] if b["id"] == bill["id"]]
    assert row["status"] == "stale", "overdue and unconfirmed: stale, never assumed paid"
    assert row["confirmed_at"] is None
    assert _today_bills(late, "coming_up") == [], "stale bills are not shown as coming up"
    await engine.dispose()


async def test_flagged_mail_never_reaches_today(tmp_path) -> None:
    engine, registry = await _open(tmp_path / "p2f.db", create=True)
    proposer = MailBillProposer(registry.bills, clock=lambda: NOW)
    status = await proposer.propose(SCOPE, content_ref="content://mail/c1/phish",
                                    evidence=parse_mime_evidence(BILL_MAIL), disposition="REVIEW")
    assert status == "SECURITY_NOT_LOW_PRIORITY"
    c = _app(registry, NOW)
    assert c.get("/api/bills/candidates").json()["count"] == 0
    assert _today_bills(c, "coming_up") == []
    await engine.dispose()
