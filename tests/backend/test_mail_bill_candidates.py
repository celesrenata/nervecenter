"""Mail -> bill candidates (NS-ADR-095): gates, idempotency, API accept/reject."""
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
from backend.mail_security.parser import parse_mime_evidence
from backend.persistence.connection import make_session_factory
from backend.persistence.models.bills import BillCandidateRow, BillRow
from backend.persistence.models.personal import AttentionItemRow
from backend.persistence.repositories import RepositoryRegistry

SCOPE = "conn:11111111-2222-3333-4444-555555555555"
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def _mail(subject: str, body: str, sender: str = "City Power <billing@citypower.example>") -> bytes:
    return (f"From: {sender}\r\nTo: me@example.test\r\nSubject: {subject}\r\n"
            "MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            f"{body}\r\n").encode()


BILL = _mail("Your City Power bill is ready",
             "Upgrade for $9.99. Amount due: $84.20. Payment due by November 20, 2026.")


@pytest.fixture
async def registry(tmp_path: pathlib.Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'b.db'}", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(BillRow.__table__.metadata.create_all,
                            tables=[BillRow.__table__, BillCandidateRow.__table__,
                                    AttentionItemRow.__table__])
    yield RepositoryRegistry(make_session_factory(engine))
    await engine.dispose()


@pytest.fixture
def proposer(registry, monkeypatch):
    monkeypatch.setenv(ENV_BILL_CANDIDATE_SCOPES, SCOPE)
    return MailBillProposer(registry.bills, clock=lambda: NOW)


async def _propose(proposer, raw: bytes, *, ref="content://mail/c1/abc", disp="LOW_PRIORITY"):
    return await proposer.propose(SCOPE, content_ref=ref, evidence=parse_mime_evidence(raw),
                                  disposition=disp)


async def test_a_real_bill_becomes_one_proposal_with_the_anchored_amount(
    proposer, registry
) -> None:
    assert await _propose(proposer, BILL) == "PROPOSED"
    (row,) = await registry.bills.list_candidates()
    assert row.status == "proposed" and row.source_kind == "email"
    assert row.source_id == "content://mail/c1/abc"
    assert row.extracted_fields["amount_cents"]["value"] == 8420, "not the $9.99 upsell"
    assert row.extracted_fields["payee"]["value"] == "City Power"
    assert "due_date" in row.extracted_fields


@pytest.mark.parametrize("disp", ["REVIEW", "RESTRICTED", "INCOMPLETE", "SAFE_TO_PROCESS", ""])
async def test_only_low_priority_mail_may_propose(proposer, registry, disp) -> None:
    assert await _propose(proposer, BILL, disp=disp) == "SECURITY_NOT_LOW_PRIORITY"
    assert await registry.bills.list_candidates() == []


@pytest.mark.parametrize("raw", [
    _mail("40% off this weekend", "Save $50 on everything. Sale ends Sunday."),
    _mail("Payment received", "Thank you for your payment of $84.20."),
    _mail("Your order shipped", "Order total $59.99."),
])
async def test_marketing_and_receipts_do_not_propose(proposer, registry, raw) -> None:
    assert await _propose(proposer, raw) == "NOT_A_BILL"
    assert await registry.bills.list_candidates() == []


async def test_not_opted_in_does_nothing(registry, monkeypatch) -> None:
    monkeypatch.delenv(ENV_BILL_CANDIDATE_SCOPES, raising=False)
    p = MailBillProposer(registry.bills, clock=lambda: NOW)
    assert await _propose(p, BILL) == "NOT_OPTED_IN"


async def test_reprocessing_the_same_email_is_idempotent(proposer, registry) -> None:
    assert await _propose(proposer, BILL) == "PROPOSED"
    assert await _propose(proposer, BILL) == "ALREADY_PROPOSED"
    assert len(await registry.bills.list_candidates()) == 1


async def test_a_reminder_for_the_same_bill_is_linked_as_a_duplicate(proposer, registry) -> None:
    await _propose(proposer, BILL, ref="content://mail/c1/first")
    await _propose(proposer, BILL, ref="content://mail/c1/reminder")
    rows = await registry.bills.list_candidates()
    assert len(rows) == 2
    linked = [r for r in rows if r.duplicate_of]
    assert len(linked) == 1 and linked[0].source_id.endswith("reminder")


# -- API ------------------------------------------------------------------------


def _client(registry) -> TestClient:
    from backend.api.endpoints import bills as b

    app = FastAPI()
    app.include_router(b.router)
    app.state.repository_registry = registry
    return TestClient(app)


async def test_list_accept_makes_a_pending_bill(proposer, registry) -> None:
    await _propose(proposer, BILL)
    c = _client(registry)
    listed = c.get("/api/bills/candidates").json()
    assert listed["count"] == 1
    cid = listed["candidates"][0]["id"]
    r = c.post(f"/api/bills/candidates/{cid}/accept")
    assert r.status_code == 200, r.text
    bill = r.json()["bill"]
    assert bill["payee"] == "City Power" and int(bill["amount_cents"]) == 8420
    assert bill["status"] == "pending"
    assert c.get("/api/bills/candidates").json()["count"] == 0
    assert c.post(f"/api/bills/candidates/{cid}/accept").status_code == 409, "no double accept"


async def test_missing_due_date_needs_the_owner_and_overrides_complete_it(
    proposer, registry
) -> None:
    raw = _mail("Statement", "Your statement balance of $1,204.10 is now available.",
                sender="Example Card <statements@card.example>")
    assert await _propose(proposer, raw) == "PROPOSED"
    c = _client(registry)
    cid = c.get("/api/bills/candidates").json()["candidates"][0]["id"]
    assert c.post(f"/api/bills/candidates/{cid}/accept").status_code == 409
    due = (NOW + timedelta(days=10)).isoformat()
    r = c.post(f"/api/bills/candidates/{cid}/accept", json={"due_at": due, "payee": "Example Card"})
    assert r.status_code == 200, r.text
    assert int(r.json()["bill"]["amount_cents"]) == 120410


async def test_reject_and_unknown_ids(proposer, registry) -> None:
    await _propose(proposer, BILL)
    c = _client(registry)
    cid = c.get("/api/bills/candidates").json()["candidates"][0]["id"]
    assert c.post(f"/api/bills/candidates/{cid}/reject").json()["candidate"]["status"] == "rejected"
    assert c.get("/api/bills/candidates").json()["count"] == 0
    assert c.get("/api/bills/candidates?status=all").json()["count"] == 1
    assert c.post("/api/bills/candidates/nope/accept").status_code == 404
    assert c.post("/api/bills/candidates/nope/reject").status_code == 404
    assert c.get("/api/bills/candidates?status=weird").status_code == 422


async def test_invalid_overrides_are_rejected(proposer, registry) -> None:
    await _propose(proposer, BILL)
    c = _client(registry)
    cid = c.get("/api/bills/candidates").json()["candidates"][0]["id"]
    bad = c.post(f"/api/bills/candidates/{cid}/accept", json={"amount_cents": -5})
    assert bad.status_code == 422
    assert c.post(f"/api/bills/candidates/{cid}/accept", json={"payee": ""}).status_code == 422


# -- worker hook: after the verdict, never failing the job -------------------------


class _Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    async def propose(self, scope, **kw):
        self.calls.append({"scope": scope, **kw})
        if self.fail:
            raise RuntimeError("proposer down")
        return "PROPOSED"


@pytest.fixture
async def harness(tmp_path):
    import _mail_security_harness_helpers as H

    from backend.mail_security import telemetry

    h = await H.make_harness(tmp_path / "bills-hook.sqlite")
    telemetry.reset_sweeper_heartbeat()
    yield h
    telemetry.reset_sweeper_heartbeat()
    await H.teardown_harness(h)


async def test_worker_calls_the_proposer_with_the_committed_disposition(
    harness, monkeypatch
) -> None:
    import _mail_security_harness_helpers as H
    from sqlalchemy import select

    from backend.bills.mail_candidates import set_bill_proposer
    from backend.persistence.models.mail_security import MailSecurityAssessmentRow

    rec = _Recorder()
    set_bill_proposer(rec)  # type: ignore[arg-type]
    try:
        await H.ingest(harness, monkeypatch, H.mailitem("<bill-1@x>", H.benign_mime()))
        await H.security_worker(harness).process_one()
    finally:
        set_bill_proposer(None)
    async with harness.registry.session_factory() as s:
        (asmt,) = (await s.execute(select(MailSecurityAssessmentRow))).scalars().all()
    (call,) = rec.calls
    assert call["scope"] == harness.scope
    assert call["disposition"] == asmt.disposition
    assert call["content_ref"] and call["evidence"] is not None


async def test_a_proposer_crash_never_fails_the_security_job(harness, monkeypatch) -> None:
    import _mail_security_harness_helpers as H
    from sqlalchemy import select

    from backend.bills.mail_candidates import set_bill_proposer
    from backend.persistence.models.mail_security import (
        MailSecurityAssessmentRow,
        MailSecurityJobRow,
    )

    set_bill_proposer(_Recorder(fail=True))  # type: ignore[arg-type]
    try:
        await H.ingest(harness, monkeypatch, H.mailitem("<bill-2@x>", H.benign_mime()))
        await H.security_worker(harness).process_one()
    finally:
        set_bill_proposer(None)
    async with harness.registry.session_factory() as s:
        (job,) = (await s.execute(select(MailSecurityJobRow))).scalars().all()
        assert len((await s.execute(select(MailSecurityAssessmentRow))).scalars().all()) == 1
    assert job.state == "DONE"


async def test_owner_tool_dry_run_repo_never_writes(registry) -> None:
    from backend.owner_tools.propose_bills_existing import _DryRunRepo

    dry = _DryRunRepo(registry.bills)
    p = MailBillProposer(dry, clock=lambda: NOW)
    import os

    os.environ[ENV_BILL_CANDIDATE_SCOPES] = SCOPE
    try:
        assert await _propose(p, BILL) == "PROPOSED"
    finally:
        os.environ.pop(ENV_BILL_CANDIDATE_SCOPES, None)
    assert dry.would_create == 1
    assert await registry.bills.list_candidates() == []


async def test_html_heavy_bill_is_proposed_with_the_gate_amount(proposer, registry) -> None:
    """Live case: the biller's text part is HTML; the extractor found no amount."""
    body = ('<table><tr><td style="font-weight:bold">Amount due</td>'
            '<td style="padding:4px">&nbsp;</td><td>&#36;2,500.00</td></tr></table>'
            '<p>Payment is due by December 15, 2026.</p>')
    raw = _mail("Your tuition statement", body, sender="Student Fiscal Services <sfs@uni.example>")
    assert await _propose(proposer, raw) == "PROPOSED"
    (row,) = await registry.bills.list_candidates()
    assert row.extracted_fields["amount_cents"]["value"] == 250000
    assert row.extracted_fields["payee"]["value"] == "Student Fiscal Services"
    assert "due_date" in row.extracted_fields


async def test_amount_in_a_form_the_extractor_misses_still_proposes(proposer, registry) -> None:
    raw = _mail("Your statement", "Past due amount USD 482.17. Please pay to avoid fees.",
                sender="Example Card <no-reply@card.example>")
    assert await _propose(proposer, raw) == "PROPOSED"
    (row,) = await registry.bills.list_candidates()
    assert row.extracted_fields["amount_cents"]["value"] == 48217
    assert row.extracted_fields["payee"]["value"] == "Example Card"


async def test_no_payee_found_is_flagged_for_review(proposer, registry) -> None:
    raw = _mail("Notice", "Amount due $30.00", sender="<billing@noname.example>")
    assert await _propose(proposer, raw) == "PROPOSED"
    (row,) = await registry.bills.list_candidates()
    assert row.extracted_fields["payee"]["value"] in ("Unknown biller", "billing@noname.example")
    assert "payee" in row.needs_review


async def test_past_due_notice_keeps_its_past_date(proposer, registry) -> None:
    """Live case: "past due" + "Nov 8" was read as Nov 8 of NEXT year."""
    raw = _mail("Your payment is past due",
                "Past due amount $75.00. Your payment was due Nov 8.",
                sender="Apple Card Support <no_reply@card.example>")
    assert await _propose(proposer, raw) == "PROPOSED"
    (row,) = await registry.bills.list_candidates()
    due = datetime.fromisoformat(row.extracted_fields["due_date"]["value"])
    assert due.year == 2026 and due.month == 11 and due.day == 8
    assert due <= NOW
    assert "due_date" not in row.needs_review


async def test_implausibly_distant_due_date_is_flagged(proposer, registry) -> None:
    raw = _mail("Your bill", "Amount due: $40.00. Payment due by October 1, 2027.")
    assert await _propose(proposer, raw) == "PROPOSED"
    (row,) = await registry.bills.list_candidates()
    assert "due_date" in row.needs_review


async def test_an_ordinary_upcoming_due_date_is_untouched(proposer, registry) -> None:
    assert await _propose(proposer, BILL) == "PROPOSED"
    (row,) = await registry.bills.list_candidates()
    due = datetime.fromisoformat(row.extracted_fields["due_date"]["value"])
    assert (due.month, due.day) == (11, 20) and "adjusted" not in row.extracted_fields["due_date"]
    assert "due_date" not in row.needs_review
