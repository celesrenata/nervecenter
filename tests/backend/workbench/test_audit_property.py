"""Property 19: Audit entries.

Feature: opencode-menagerie-workbench, Property 19: Audit entries
**Validates: Requirements 6.11, 6.12**
"""
from __future__ import annotations

import asyncio
import json
import logging

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.persistence.models.workbench import WorkbenchAudit
from backend.workbench.audit import MAX_ENTRY_BYTES, AuditLog

# Identifier-like fields use lowercase letters only (Ll/Lo, includes non-ASCII),
# so they can never contain the uppercase-marked payload text below.
_field = st.text(
    alphabet=st.characters(categories=("Ll", "Lo"), exclude_characters="\x00"),
    min_size=1,
    max_size=3000,
)
_payload = st.text(max_size=2000).map(lambda t: "PAYLOAD_SECRET_" + t)


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


async def _record_and_read(actor, slug, action, outcome):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    try:
        async with engine.begin() as conn:
            # SQLite only autoincrements INTEGER PRIMARY KEY, not BIGINT, so use raw DDL.
            await conn.execute(
                text(
                    "CREATE TABLE workbench_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "at TIMESTAMP NOT NULL, actor TEXT NOT NULL, project_slug TEXT, "
                    "action TEXT NOT NULL, outcome TEXT NOT NULL "
                    "CHECK (outcome IN ('success','failure')))"
                )
            )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        await AuditLog(factory).record(actor, slug, action, outcome)
        async with factory() as session:
            return list((await session.execute(select(WorkbenchAudit))).scalars())
    finally:
        await engine.dispose()


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    actor=_field,
    slug=_field,
    action=_field,
    outcome=st.sampled_from(["success", "failure"]),
    payload=_payload,
)
def test_audit_entry_has_required_fields_bounded_and_payload_free(
    actor, slug, action, outcome, payload
):
    # The payload is handled by the (simulated) action but never reaches the log API.
    _ = payload
    handler = _Capture()
    logger = logging.getLogger("nervecenter.workbench.audit")
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        rows = asyncio.run(_record_and_read(actor, slug, action, outcome))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    # Exactly one entry in the store and one structured log line.
    assert len(rows) == 1
    assert len(handler.lines) == 1
    row = rows[0]
    assert row.actor and row.project_slug and row.action
    assert row.outcome == outcome
    assert actor.startswith(row.actor)
    assert slug.startswith(row.project_slug)
    assert action.startswith(row.action)

    line = handler.lines[0]
    assert len(line.encode("utf-8")) <= MAX_ENTRY_BYTES
    parsed = json.loads(line)
    assert parsed["actor"] == row.actor
    assert parsed["project"] == row.project_slug
    assert parsed["action"] == row.action
    assert parsed["outcome"] == outcome

    # No payload text anywhere in the stored row or log line.
    stored = " ".join([row.actor, row.project_slug, row.action, row.outcome])
    assert "PAYLOAD_SECRET_" not in stored
    assert "PAYLOAD_SECRET_" not in line
    assert payload not in stored and payload not in line
