"""Full-capture backfill (NS-ADR-071): revision 2 beside an immutable revision 1."""
from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

import _mail_security_harness_helpers as H
import pytest
from test_mail_security_full_capture import RawFakeGmail

from backend.api.models import MailConnector  # noqa: F401 - import-order guard
from backend.ingest.mail_worker import _content_key, _raw_content_key
from backend.mail_security import telemetry
from backend.mail_security.worker import source_revision_of
from backend.owner_tools import backfill_raw_capture as tool
from backend.persistence.repositories.mail_security_gate_repo import HeldPromotionRepository


@pytest.mark.parametrize(
    ("meta", "rev"),
    [({}, 1), ({"source_revision": 2}, 2), ({"source_revision": 0}, 1),
     ({"source_revision": 101}, 1), ({"source_revision": "2"}, 1),
     ({"source_revision": True}, 1), (None, 1)],
)
def test_source_revision_is_bounded(meta: Any, rev: int) -> None:
    assert source_revision_of(meta) == rev


@pytest.fixture
async def harness(tmp_path):
    from backend.persistence.models.mail_security import MailSecurityHeldPromotionRow

    h = await H.make_harness(tmp_path / "rawfill.sqlite")
    async with h.engine.begin() as conn:
        await conn.run_sync(
            lambda sc: MailSecurityHeldPromotionRow.__table__.create(sc, checkfirst=True)
        )
    telemetry.reset_sweeper_heartbeat()
    yield h
    telemetry.reset_sweeper_heartbeat()
    await H.teardown_harness(h)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


async def test_backfill_adds_revision_two_and_readers_see_it(harness, monkeypatch) -> None:
    mid = "gmail:m1"
    raw = H.forged_auth_mime()
    # Revision 1: the historical body-only ingest (decoded body, no headers).
    await H.ingest(harness, monkeypatch, H.mailitem(mid, b"Your statement is ready.\n"))
    await H.security_worker(harness).process_one()
    repo = harness.registry.mail_security
    (r1,) = await repo.list_assessments(harness.scope)
    assert r1.evidence_fidelity == "BODY_ONLY" and r1.revision == 1

    fake = RawFakeGmail(raw={"m1": (200, {"raw": _b64(raw)})})
    monkeypatch.setattr(tool.httpx, "AsyncClient", lambda **kw: fake)
    reg = SimpleNamespace(connections=harness.registry.connections,
                          session_factory=harness.registry.session_factory)
    conn = SimpleNamespace(id=harness.conn.id, config={"account": H.ACCOUNT})

    dry = await tool._connection(conn, reg, harness.store, apply=False, limit=10)
    assert dry["eligible"] == 1 and fake.raw_calls == []
    applied = await tool._connection(conn, reg, harness.store, apply=True, limit=10)
    assert applied["requeued"] == 1 and fake.raw_calls == ["m1"]
    assert await harness.store.get(_raw_content_key(harness.conn.id, mid)) == raw

    await H.security_worker(harness).process_one()
    rows = {a.revision: a for a in await repo.list_assessments(harness.scope)}
    assert set(rows) == {1, 2}
    assert rows[1].evidence_fidelity == "BODY_ONLY"  # immutable history
    assert rows[2].evidence_fidelity == "ORIGINAL_RFC822"
    latest = await repo.latest_assessment_for_source(harness.scope, r1.source_id)
    assert latest.revision == 2
    body_ref = f"content://{_content_key(harness.conn.id, mid)}"
    assert await repo.source_key_for_content_ref(harness.scope, body_ref) == r1.source_id
    verdict = await HeldPromotionRepository(harness.registry.session_factory).latest_verdict(
        harness.scope, body_ref
    )
    assert verdict is not None and verdict[1] == rows[2].assessment_id
    (job,) = await repo.list_jobs(harness.scope)
    assert job.state == "DONE" and job.meta["acquisition_mode"] == "GMAIL_RAW"

    # Idempotent: nothing left to move.
    again = await tool._connection(conn, reg, harness.store, apply=True, limit=10)
    assert again["eligible"] == 0 and again["requeued"] == 0


async def test_backfill_skips_on_gmail_failure(harness, monkeypatch) -> None:
    await H.ingest(harness, monkeypatch, H.mailitem("gmail:m2", b"hello there\n"))
    await H.security_worker(harness).process_one()
    fake = RawFakeGmail(raw={})  # 404
    monkeypatch.setattr(tool.httpx, "AsyncClient", lambda **kw: fake)
    reg = SimpleNamespace(connections=harness.registry.connections,
                          session_factory=harness.registry.session_factory)
    conn = SimpleNamespace(id=harness.conn.id, config={"account": H.ACCOUNT})
    n = await tool._connection(conn, reg, harness.store, apply=True, limit=10)
    assert n["skip_http_404"] == 1 and n["requeued"] == 0
    (job,) = await harness.registry.mail_security.list_jobs(harness.scope)
    assert job.state == "DONE" and job.meta["acquisition_mode"] == "LEGACY_BODY_ONLY"
