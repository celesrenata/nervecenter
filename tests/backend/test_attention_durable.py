"""Durable attention seam (S4) — detection findings reach the durable table.

The just-shipped S1 fix made the detector run on real body text and produce
findings, and those findings already reach the *in-memory* ``AttentionService``
(``GET /api/attention``). But ``GET /api/today`` reads the **durable**
``attention_items`` table only, so the two halves never met and Today stayed
empty. This suite proves S4: a detected finding, mirrored into the durable
table through the
:class:`~backend.communications.detection.wiring.CommunicationPipeline`
attention seam, now

  (i)   writes a row to the durable ``attention_items`` table (not just memory),
  (ii)  appears via ``GET /api/today`` (needs_you) AND the durable read path,
  (iii) is idempotent across a re-ingest (deterministic id — no duplicate rows),
  (iv)  degrades gracefully when the durable write fails (ingest never crashes),
        and the in-memory item (and ``GET /api/attention``) is unaffected.

Design choices under test (documented on the seam):
- **durable + in-memory mirror** — the finding is persisted durably AND still
  fed to the in-memory ``AttentionService`` (so ``/api/attention`` keeps its
  behavior); the durable write is the NEW half that lets Today surface it.
- **best-effort (not gated)** — a durable attention write failure is logged
  and swallowed; it never blocks the worker's cursor advance (unlike the gated
  commitment/person write).
- **deterministic id** — the durable id is ``at-<sha256(kind:finding_id)[:12]>``;
  the finding id is itself deterministic (``<kind>-<communication_id>``), so a
  replay reuses the row (get-or-create).

The scratch DB is file-backed SQLite (``aiosqlite``) with ``StaticPool``,
mirroring the house durable-test convention (cf. test_commitment_restart.py).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

# Circular-import-safe entry (see test_commitment_restart.py): force
# backend.api.app to fully initialize before backend.attention.service is
# imported directly.
from backend.api.app import create_app  # noqa: F401
from backend.attention.service import AttentionService
from backend.communications.detection.service import DetectionService
from backend.communications.detection.wiring import CommunicationPipeline
from backend.communications.models import (
    Communication,
    CommunicationChannel,
    CommunicationDirection,
)
from backend.followups.service import FollowUpService
from backend.persistence.connection import make_session_factory
from backend.persistence.models.personal import (
    AttentionItemRow,
    CommitmentRow,
    ConnectionRow,
    FollowupRow,
    PersonRow,
    PreferenceRow,
)
from backend.persistence.repositories import RepositoryRegistry

# Tuesday 2026-10-20 10:00 UTC — deterministic clock (mirrors the sibling
# durable tests).
NOW = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)

#: An INCOMING request — the real-life case: incoming mail yields a ``request``
#: finding (the 17 in-memory findings the map describes), which detection feeds
#: to attention but, before S4, never reached the durable table.
TEXT = "Can you send me the report."
COMM_ID = "message:9001"
PERSON_ID = "person:dana"

#: Durable tables the seam + Today read path touch.
_TABLES = (
    PersonRow.__table__,
    CommitmentRow.__table__,
    FollowupRow.__table__,
    AttentionItemRow.__table__,
    ConnectionRow.__table__,
    PreferenceRow.__table__,
)


async def _make_engine(db_path: Path) -> AsyncEngine:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            PersonRow.metadata.create_all, tables=list(_TABLES)
        )
    return engine


def _make_pipeline(
    registry: RepositoryRegistry,
) -> tuple[CommunicationPipeline, AttentionService]:
    """A fresh pipeline whose durable attention mirror is bound to the test
    registry (so detected findings persist). The three core services are
    in-memory; the ``attention_store`` is the seam under test."""
    attention = AttentionService()
    pipeline = CommunicationPipeline(
        detection=DetectionService(attention=attention),
        followups=FollowUpService(attention=attention),
        attention=attention,
        attention_store=registry.attention,
    )
    return pipeline, attention


def _make_communication() -> Communication:
    return Communication(
        communication_id=COMM_ID,
        channel=CommunicationChannel.EMAIL,
        direction=CommunicationDirection.INCOMING,
        sender="dana@example.com",
        person_id=PERSON_ID,
        timestamp=NOW,
        content_ref=f"cr:{COMM_ID}",
    )


def _ingest(pipeline: CommunicationPipeline, comm: Communication) -> Any:
    pipeline.detection.register_content(comm.content_ref, TEXT)
    return pipeline.ingest(comm, now=NOW)


def _make_comm(
    comm_id: str,
    *,
    sender: str,
    flags: dict | None = None,
) -> Communication:
    """A fresh INCOMING communication with an explicit sender + FEAT-001
    flags (carried on ``classification['flags']``, the in-memory read point)."""
    classification: dict[str, Any] = {}
    if flags is not None:
        classification["flags"] = flags
    return Communication(
        communication_id=comm_id,
        channel=CommunicationChannel.EMAIL,
        direction=CommunicationDirection.INCOMING,
        sender=sender,
        person_id=PERSON_ID,
        timestamp=NOW,
        content_ref=f"cr:{comm_id}",
        classification=classification,
    )


def _ingest_text(pipeline: CommunicationPipeline, comm: Communication, text: str) -> Any:
    pipeline.detection.register_content(comm.content_ref, text)
    return pipeline.ingest(comm, now=NOW)


async def _needs_you_ids(registry: RepositoryRegistry) -> set[tuple[str, str]]:
    """The (kind, id) set GET /api/today surfaces in needs_you over the
    durable registry — the relevance floor applied."""
    app = FastAPI()
    from backend.api.endpoints import today

    app.include_router(today.router)
    app.state.repository_registry = registry
    app.state._test_now = NOW
    with TestClient(app) as client:
        body = client.get("/api/today").json()
    return {(it["kind"], it["id"]) for it in body["needs_you"]}


async def test_detected_finding_writes_durable_attention_row(tmp_path: Path):
    """(i) A detected request finding writes a row to the durable
    ``attention_items`` table — not just the in-memory queue."""
    engine = await _make_engine(tmp_path / "attn.db")
    registry = RepositoryRegistry(make_session_factory(engine))
    pipeline, attention = _make_pipeline(registry)

    result = _ingest(pipeline, _make_communication())
    # The detector produced at least one finding, fed to the in-memory queue…
    assert result.finding_ids, "the detector must produce a finding"
    assert attention.list(), "the in-memory mirror must still be fed"
    # …and the durable mirror scheduled the write; flush it.
    failure = await pipeline.flush_durable()
    assert failure is None

    rows = await registry.attention.list_attention_items(state="active")
    assert len(rows) == 1, rows
    row = rows[0]
    # §7: metadata/refs only — never the raw body text NOR the detector's
    # quoted body fragment (``finding.matched``). The leaking value is the
    # signal fragment, not just the full sentence, so assert BOTH are absent
    # and that the title is the purely templated request form (no body
    # substring interpolated into it).
    assert TEXT not in (row.body or "")
    assert TEXT not in row.title
    report = pipeline.detection.report_for(COMM_ID)
    request_finding = next(f for f in report.findings if f.kind == "request")
    matched = request_finding.matched
    assert matched, "the request finding must carry a matched fragment to guard"
    assert matched not in row.title, f"§7: body fragment leaked into title: {row.title!r}"
    assert matched not in (row.body or "")
    # The request title is fully templated from kind + sender only.
    assert row.title == "Respond to dana@example.com"
    assert f"communication:{COMM_ID}" in row.source_refs
    assert any(r.startswith("finding:") for r in row.source_refs)
    assert row.recommended_action == "Reply to the request"
    assert row.state == "active"
    assert row.urgency in ("low", "normal", "high", "critical")

    await engine.dispose()


async def test_durable_attention_appears_on_today_and_durable_read(tmp_path: Path):
    """(ii) The durable row appears via ``GET /api/today`` (needs_you) AND via
    the durable read path the endpoint uses."""
    engine = await _make_engine(tmp_path / "attn_today.db")
    registry = RepositoryRegistry(make_session_factory(engine))
    pipeline, _ = _make_pipeline(registry)

    _ingest(pipeline, _make_communication())
    assert await pipeline.flush_durable() is None

    # Durable read path (what the Today _AttentionProvider.needs_you reads).
    durable = await registry.attention.list_attention_items(state="active")
    assert len(durable) == 1

    # GET /api/today via the real router over the same durable registry.
    app = FastAPI()
    from backend.api.endpoints import today

    app.include_router(today.router)
    app.state.repository_registry = registry
    app.state._test_now = NOW
    with TestClient(app) as client:
        body = client.get("/api/today").json()

    needs = {(it["kind"], it["id"]) for it in body["needs_you"]}
    assert ("attention", durable[0].id) in needs, body["needs_you"]

    await engine.dispose()


async def test_reingest_is_idempotent_no_duplicate_rows(tmp_path: Path):
    """(iii) Re-running detection/ingest for the same communication does NOT
    create a duplicate durable attention row (deterministic id)."""
    engine = await _make_engine(tmp_path / "attn_idem.db")
    registry = RepositoryRegistry(make_session_factory(engine))
    pipeline, _ = _make_pipeline(registry)

    _ingest(pipeline, _make_communication())
    assert await pipeline.flush_durable() is None
    first = await registry.attention.list_attention_items()
    assert len(first) == 1
    first_id = first[0].id

    # Replay in the same process (in-process dedup) …
    _ingest(pipeline, _make_communication())
    assert await pipeline.flush_durable() is None
    assert len(await registry.attention.list_attention_items()) == 1

    # … and through a *fresh* pipeline (restart — the deterministic id makes
    # the durable write a get-or-create no-op, no in-process memo to help).
    pipeline2, _ = _make_pipeline(registry)
    _ingest(pipeline2, _make_communication())
    assert await pipeline2.flush_durable() is None
    rows = await registry.attention.list_attention_items()
    assert len(rows) == 1, rows
    assert rows[0].id == first_id

    await engine.dispose()


class _FailingAttentionStore:
    """A durable attention sink that always raises on write (simulated DB
    outage) — proves the best-effort degradation contract."""

    async def create_attention_item(self, **_kw: Any) -> Any:
        raise RuntimeError("durable attention write failed")


async def test_durable_write_failure_degrades_gracefully(tmp_path: Path):
    """(iv) A durable attention write failure degrades honestly: ingest never
    crashes, ``flush_durable`` does not surface it (best-effort, not gated),
    and the in-memory mirror is unaffected (``/api/attention`` still works)."""
    attention = AttentionService()
    pipeline = CommunicationPipeline(
        detection=DetectionService(attention=attention),
        followups=FollowUpService(attention=attention),
        attention=attention,
        attention_store=_FailingAttentionStore(),
    )

    # ingest is synchronous and must not raise despite the failing store.
    result = _ingest(pipeline, _make_communication())
    assert result.finding_ids
    # The in-memory half is unaffected — this is what /api/attention reads.
    assert attention.list()

    # Best-effort: the attention failure is logged + swallowed, NEVER returned
    # as a gated failure (which would block the worker's cursor advance).
    assert await pipeline.flush_durable() is None


async def test_no_store_is_a_noop(tmp_path: Path):
    """Without an injected attention store the seam performs zero durable I/O
    (extend-only); the in-memory mirror still runs."""
    attention = AttentionService()
    pipeline = CommunicationPipeline(
        detection=DetectionService(attention=attention),
        followups=FollowUpService(attention=attention),
        attention=attention,
    )
    assert pipeline.attention_store is None

    result = _ingest(pipeline, _make_communication())
    assert result.finding_ids
    assert attention.list()  # in-memory half still fed
    assert await pipeline.flush_durable() is None  # nothing durable to flush


# ---------------------------------------------------------------------------
# FEAT-002 — relevance classification + scoring at the mint layer.
# ---------------------------------------------------------------------------


async def test_no_reply_request_is_low_importance_non_actionable(tmp_path: Path):
    """A request from a no-reply/automated sender mints a low-importance
    (<=2) NON-actionable row (no 'Reply to the request') that is absent from
    GET /api/today needs_you — but still persisted (auditable)."""
    engine = await _make_engine(tmp_path / "attn_noreply.db")
    registry = RepositoryRegistry(make_session_factory(engine))
    pipeline, _ = _make_pipeline(registry)

    comm = _make_comm(
        "message:noreply",
        sender="no-reply@login.gov",
        flags={"no_reply": True, "automated": True},
    )
    _ingest_text(pipeline, comm, "Can you send me the report.")
    assert await pipeline.flush_durable() is None

    rows = await registry.attention.list_attention_items()
    request_rows = [r for r in rows if "finding:request-message:noreply" in (r.source_refs or [])]
    assert len(request_rows) == 1, rows
    row = request_rows[0]
    # Low-importance, non-actionable, informational.
    assert row.importance <= 2
    assert row.recommended_action is None
    assert row.title == "Saw a message from no-reply@login.gov"
    # Persisted (auditable) but kept off the Today surface.
    assert row.state == "active"
    assert ("attention", row.id) not in await _needs_you_ids(registry)

    await engine.dispose()


async def test_vip_person_request_is_high_and_on_today(tmp_path: Path):
    """A request from a VIP person scores high (9) and appears in needs_you."""
    engine = await _make_engine(tmp_path / "attn_vip.db")
    registry = RepositoryRegistry(make_session_factory(engine))
    pipeline, _ = _make_pipeline(registry)

    comm = _make_comm(
        "message:vip",
        sender="ceo@example.com",
        flags={"is_vip": True},
    )
    _ingest_text(pipeline, comm, "Can you send me the report.")
    assert await pipeline.flush_durable() is None

    rows = await registry.attention.list_attention_items()
    request_rows = [r for r in rows if "finding:request-message:vip" in (r.source_refs or [])]
    assert len(request_rows) == 1, rows
    row = request_rows[0]
    # High: base person 6 + VIP 2 + actionable 1 (+1 when the detector is
    # high-confidence). Well above the Today floor.
    assert row.importance >= 9
    assert row.recommended_action == "Reply to the request"
    assert row.title == "Respond to ceo@example.com"
    assert ("attention", row.id) in await _needs_you_ids(registry)

    await engine.dispose()


async def test_far_future_deadline_is_importance_one_and_off_today(tmp_path: Path):
    """A far-future (>400 days) deadline is importance 1 / urgency low and
    absent from needs_you — the 'Deadline due 2028-…' critical-row class is
    gone."""
    engine = await _make_engine(tmp_path / "attn_farfuture.db")
    registry = RepositoryRegistry(make_session_factory(engine))
    pipeline, _ = _make_pipeline(registry)

    # NOW is 2026-10-20; 2028-11-02 is well over 400 days out.
    comm = _make_comm("message:farfuture", sender="dana@example.com")
    _ingest_text(pipeline, comm, "Please file the report due 2028-11-02.")
    assert await pipeline.flush_durable() is None

    rows = await registry.attention.list_attention_items()
    deadline_rows = [
        r for r in rows if "finding:deadline-message:farfuture" in (r.source_refs or [])
    ]
    assert len(deadline_rows) == 1, rows
    row = deadline_rows[0]
    assert row.importance == 1
    assert row.urgency == "low"
    assert ("attention", row.id) not in await _needs_you_ids(registry)

    await engine.dispose()
