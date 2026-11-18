"""Property 23: Usage records are content-free.

Feature: opencode-menagerie-workbench, Property 23: Usage records are content-free
**Validates: Requirements 8.8**
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.workbench.usage import UsageRecorder

_ALLOWED_COLUMNS = {"id", "session_id", "model", "input_tokens", "output_tokens", "recorded_at"}

# Identifiers use lowercase letters/digits only, so they can never contain the
# uppercase-marked prompt/completion text below.
_ident = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_", min_size=1, max_size=64)
_content = st.text(max_size=500).map(lambda t: "PROMPT_SECRET_" + t)
_timestamps = st.datetimes(
    min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1)
).map(lambda d: d.replace(tzinfo=UTC))


async def _run(session_id, model, tokens_in, tokens_out, when):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE workbench_model_usage ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, "
                    "model TEXT NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0, "
                    "output_tokens INTEGER NOT NULL DEFAULT 0, recorded_at TIMESTAMP NOT NULL)"
                )
            )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        recorder = UsageRecorder(factory)
        rec = await recorder.record(session_id, model, tokens_in, tokens_out, when)
        listed = await recorder.list_usage(session_id)
        async with engine.connect() as conn:
            cols = [r[1] for r in (await conn.execute(text("PRAGMA table_info(workbench_model_usage)")))]
            rows = [tuple(r) for r in (await conn.execute(text("SELECT * FROM workbench_model_usage")))]
        return rec, listed, cols, rows
    finally:
        await engine.dispose()


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    session_id=_ident,
    model=_ident,
    tokens_in=st.integers(min_value=0, max_value=10**9),
    tokens_out=st.integers(min_value=0, max_value=10**9),
    when=_timestamps,
    prompt=_content,
    completion=_content,
)
def test_usage_record_is_content_free(
    session_id, model, tokens_in, tokens_out, when, prompt, completion
):
    # Prompt and completion exist in the caller but record() has no way to receive them.
    _ = (prompt, completion)
    params = set(inspect.signature(UsageRecorder.record).parameters) - {"self"}
    assert params == {"session_id", "model", "input_tokens", "output_tokens", "recorded_at"}

    rec, listed, cols, rows = asyncio.run(_run(session_id, model, tokens_in, tokens_out, when))

    # Stored fields are present and exact.
    assert rec.session_id == session_id
    assert rec.model == model
    assert rec.input_tokens == tokens_in
    assert rec.output_tokens == tokens_out
    assert rec.recorded_at is not None
    assert listed == [rec] or [r.id for r in listed] == [rec.id]

    # Schema carries no text columns beyond identifiers.
    assert set(cols) == _ALLOWED_COLUMNS
    assert not {"prompt", "completion", "content", "text", "message"} & set(cols)

    # No prompt or completion text appears in any stored row.
    assert len(rows) == 1
    flat = " ".join(str(v) for v in rows[0])
    assert "PROMPT_SECRET_" not in flat
    assert prompt not in flat
    assert completion not in flat
