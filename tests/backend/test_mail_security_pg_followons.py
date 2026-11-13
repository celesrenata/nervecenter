"""Real-Postgres races for the G8 follow-on repositories (gate evidence).

Skipped unless ``NERVE_TEST_POSTGRES_URL`` points at a SCRATCH database at
migration head. Each test uses a unique account scope and deletes its rows.
SQLite serializes writers, so these insert races only show up on Postgres:

- thread ledger: N concurrent records of the SAME message -> exactly one row;
- campaigns: N concurrent records of the SAME source -> exactly one member;
- observations: N concurrent records of the SAME revision -> exactly one row;
- expectedness: N concurrent increments of the SAME key -> count == N;
- expectedness feedback: N concurrent adds of the SAME feedback id -> one row.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import create_async_engine

from backend.mail_security.campaigns import band_keys, signature
from backend.mail_security.thread_ledger import account_salt, observe
from backend.persistence.connection import make_session_factory
from backend.persistence.models.mail_security import (
    MailCampaignBandRow,
    MailCampaignMemberRow,
    MailExpectednessCountRow,
    MailExpectednessFeedbackRow,
    MailSemanticObservationRow,
    MailThreadLedgerRow,
)
from backend.persistence.repositories.mail_security_campaign_repo import MailCampaignRepository
from backend.persistence.repositories.mail_security_expectedness_repo import (
    MailExpectednessRepository,
)
from backend.persistence.repositories.mail_security_observation_repo import (
    MailObservationRepository,
)
from backend.persistence.repositories.mail_security_thread_repo import (
    MailThreadLedgerRepository,
)

PG_URL = os.environ.get("NERVE_TEST_POSTGRES_URL", "")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not PG_URL.startswith("postgresql"), reason="NERVE_TEST_POSTGRES_URL not set"
    ),
]
N = 16
TEXT = (
    "Dear customer your mailbox storage is almost full and incoming messages will be "
    "rejected within 24 hours unless you verify your account at the secure portal today"
)
TABLES = (MailThreadLedgerRow, MailCampaignBandRow, MailCampaignMemberRow,
          MailSemanticObservationRow, MailExpectednessCountRow, MailExpectednessFeedbackRow)


def _alembic(action, revision: str) -> None:
    """Run Alembic on its own thread (env.py uses asyncio.run)."""
    from concurrent.futures import ThreadPoolExecutor

    from alembic.config import Config

    from backend.persistence import migrate

    config = Config()
    config.set_main_option("script_location", str(migrate._script_location()))
    config.set_main_option("sqlalchemy.url", PG_URL)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(action, config, revision).result()


@pytest.fixture(scope="module", autouse=True)
def migrated():
    """Up to head for the follow-on tables, then back down to 020 afterwards."""
    from alembic import command

    _alembic(command.upgrade, "head")
    try:
        yield
    finally:
        _alembic(command.downgrade, "020_mail_confirmations")


@pytest.fixture
async def sessions():
    engine = create_async_engine(PG_URL, pool_size=N, max_overflow=N)
    factory = make_session_factory(engine)
    scope = f"conn:pgtest-{uuid.uuid4().hex[:12]}"
    try:
        yield factory, scope
    finally:
        async with factory() as s:
            for model in TABLES:
                await s.execute(delete(model).where(model.account_scope == scope))
            await s.commit()
        await engine.dispose()


async def _count(factory, model, scope) -> int:
    async with factory() as s:
        return int((await s.execute(
            select(func.count()).select_from(model).where(model.account_scope == scope)
        )).scalar())


async def test_ledger_same_message_once(sessions) -> None:
    factory, scope = sessions
    repo = MailThreadLedgerRepository(factory)
    msg = observe({"Message-ID": "<race@x>", "From": "a@b.example"}, subject="hi", outgoing=False)
    salt = account_salt(os.urandom(32), scope)
    await asyncio.gather(*(repo.record(scope, salt, msg) for _ in range(N)))
    assert await _count(factory, MailThreadLedgerRow, scope) == 1


async def test_campaign_same_source_once(sessions) -> None:
    factory, scope = sessions
    repo = MailCampaignRepository(factory)
    secret = os.urandom(32)
    sig = signature(secret, scope, TEXT)
    bands = band_keys(secret, scope, sig)
    now = datetime.now(UTC)
    await asyncio.gather(*(
        repo.record(scope, source_key="src-race", content_ref=None, sig=sig, bands=bands, now=now)
        for _ in range(N)
    ))
    assert await _count(factory, MailCampaignMemberRow, scope) == 1


async def test_observation_same_revision_once(sessions) -> None:
    factory, scope = sessions
    repo = MailObservationRepository(factory)
    results = await asyncio.gather(*(
        repo.record(scope, source_key="src-race", revision=2, status="COMPLETED")
        for _ in range(N)
    ))
    assert sum(results) == 1
    assert await _count(factory, MailSemanticObservationRow, scope) == 1


async def test_expectedness_concurrent_increments_all_count(sessions) -> None:
    factory, scope = sessions
    repo = MailExpectednessRepository(factory)
    await asyncio.gather(*(repo.add(scope, "k" * 24, ["PAY"]) for _ in range(N)))
    assert await repo.counts(scope, "k" * 24) == {"PAY": float(N)}


async def test_expectedness_feedback_same_id_once(sessions) -> None:
    factory, scope = sessions
    repo = MailExpectednessRepository(factory)
    expires = datetime.now(UTC) + timedelta(days=1)
    results = await asyncio.gather(*(
        repo.add_feedback(
            feedback_id=f"efb_{scope}", account_scope=scope, assessment_id="a",
            source_key="s", sender_key="k" * 24, request_type="PAY", weight=2,
            expires_at=expires, reviewer_ref="r",
        )
        for _ in range(N)
    ))
    assert sum(1 for _row, created in results if created) == 1
    assert await _count(factory, MailExpectednessFeedbackRow, scope) == 1
