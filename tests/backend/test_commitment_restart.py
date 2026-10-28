"""Restart-semantics test for the durable commitment seam (extend-only).

Mirrors ``tests/backend/test_restart_persistence.py`` (the workstream-F
contract) to prove the one thing this seam adds: a detected commitment,
mirrored into the durable ``commitments`` table through the
:class:`~backend.communications.detection.wiring.CommunicationPipeline`,
survives a *simulated process restart* — the engine is disposed and a fresh
engine + session factory are created the same way the app wiring does, and
the row is read back through the same repository method the endpoints use,
unchanged.

The seam is **extend-only / scope-safe**:

- ``CommunicationPipeline.ingest`` stays **synchronous** (its callers invoke
  it without ``await`` — see ``backend/ingest/mail_worker.py``); the durable
  write is scheduled in the background, never awaited on the ingest path.
- ``CommunicationPipeline`` remains pure unless a
  :class:`~backend.communications.detection.wiring.DurableCommitmentStore`
  is injected. With no store, the pipeline performs zero I/O and behaves
  exactly as before — the injected store is the *only* path to durability.
- The pipeline talks only to the ``DurableCommitmentStore`` protocol, so the
  concrete ``CommitmentsRepository`` (here) — or any test double — satisfies
  it without the pipeline importing the persistence layer.

The database is a **file-backed** SQLite scratch DB (``aiosqlite``), mirroring
the house test convention. A *file* DB — not in-memory — is used deliberately:
``dispose()`` must not drop the data, which is exactly what a real restart
relies on.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

# Circular-import-safe entry (see test_detection_wiring.py): force
# backend.api.app (and with it backend.attention) to fully initialize before
# backend.attention.service is imported directly.
from backend.api.app import create_app  # noqa: F401
from backend.attention.service import AttentionService
from backend.communications.detection.service import DetectionService
from backend.communications.detection.wiring import (
    CommunicationPipeline,
    DurableCommitmentStore,
)
from backend.communications.models import (
    Communication,
    CommunicationChannel,
    CommunicationDirection,
)
from backend.followups.service import FollowUpService
from backend.persistence.connection import make_session_factory
from backend.persistence.models.personal import CommitmentRow, PersonRow
from backend.persistence.repositories import RepositoryRegistry
from backend.persistence.repositories.communications_repo import (
    CommunicationsRepository,
)

# Fixed anchor: Tuesday 2026-10-20 10:00 UTC (weekday/date math is
# deterministic — mirrors test_detection_wiring.py).
NOW = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
#: An OUTGOING commitment carrying an explicit due time ("tonight" →
#: end-of-day 2026-10-20 20:00 UTC), so it is a real, durable, due-eligible
#: commitment finding.
TEXT = "I'll send it tonight."
COMM_ID = "message:8821"
PERSON_ID = "person:sam"


def _same_time(a: Any, b: Any) -> bool:
    """Compare two timestamps without tzinfo.

    The scratch DB is SQLite, which drops timezone awareness on read-back even
    though the column is ``DateTime(timezone=True)`` (Postgres ``timestamptz``
    preserves it). The restart test asserts on wall-clock equality; on a real
    Postgres the values would compare directly.
    """
    if a is None or b is None:
        return a is b
    return a.replace(tzinfo=None) == b.replace(tzinfo=None)


async def _make_engine(db_path: Path) -> AsyncEngine:
    """File-backed scratch engine, mirroring test_restart_persistence.py.

    Only the tables the seam writes into are created (``commitments`` and its
    ``people`` FK target). ``StaticPool`` + a *file* (not in-memory) DB is
    deliberate: disposing the engine must not drop the rows, which is exactly
    what a real process restart relies on.
    """
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    tables = [
        PersonRow.__table__,
        CommitmentRow.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(PersonRow.metadata.create_all, tables=tables)
    return engine


def _make_pipeline(store: DurableCommitmentStore) -> CommunicationPipeline:
    """Compose a fresh pipeline over in-memory services + the durable store.

    The three core services are in-memory (as ``test_detection_wiring.py``
    does); the only durable surface is the injected ``commitment_store``, which
    is the seam under test.
    """
    attention = AttentionService()
    return CommunicationPipeline(
        detection=DetectionService(attention=attention),
        followups=FollowUpService(attention=attention),
        attention=attention,
        commitment_store=store,
    )


def _make_communication() -> Communication:
    return Communication(
        communication_id=COMM_ID,
        channel=CommunicationChannel.SMS,
        direction=CommunicationDirection.OUTGOING,
        sender="me",
        person_id=PERSON_ID,
        timestamp=NOW,
        content_ref=f"cr:{COMM_ID}",
    )


async def _seed_person(comms: CommunicationsRepository, person_id: str) -> None:
    """Persist the durable person row — the only FK target the seam writes.

    The seam keys a durable commitment to a durable ``people`` row; the source
    communication stays in the in-memory domain plane (the pipeline never sees
    the durable ``communications`` row, so it is not FK-linked — see
    ``wiring._persist_one_commitment``).
    """
    await comms.create_person(id=person_id, display_name="Sam")


async def test_commitment_survives_restart(tmp_path: Path):
    """A pipeline-detected commitment persists through a simulated restart.

    Phase 1: persist the durable person row, run one ``ingest`` pass at the
    pinned clock (synchronous — no ``await`` on the ingest path), flush the
    background durable write, and snapshot the row.
    Phase 2: dispose the engine (the restart).
    Phase 3: re-open engine + factory the way the app would and read the row
    back through the same repository method the endpoints use.
    """
    db_path = tmp_path / "commitment_plane.db"
    engine = await _make_engine(db_path)
    registry = RepositoryRegistry(make_session_factory(engine))
    commitments = registry.commitments
    comms = registry.communications
    pipeline = _make_pipeline(commitments)

    # -- Phase 1: seed the FK target + run the §61 pass through the seam. ----
    await _seed_person(comms, PERSON_ID)

    comm = _make_communication()
    # Register the transient plaintext so the deterministic detector sees it
    # (mirrors test_detection_wiring.ingest_text); content is never persisted.
    pipeline.detection.register_content(comm.content_ref, TEXT)
    result = pipeline.ingest(comm, now=NOW)  # synchronous — no await.

    # The pass detected a commitment finding and scheduled exactly one
    # durable write.
    assert any(
        link.finding_kind == "commitment" for link in result.links
    ), result.links
    await pipeline.wait_for_commitments()

    rows = await commitments.list_commitments(person_id=PERSON_ID)
    assert len(rows) == 1, rows
    c1 = rows[0]
    snapshot = {
        "id": c1.id,
        "person_id": c1.person_id,
        "text": c1.text,
        "state": c1.state,
        "source_communication_id": c1.source_communication_id,
    }
    assert c1.person_id == PERSON_ID
    assert c1.state == "open"
    # The pipeline never sees the durable communications row, so no durable
    # FK is written; domain provenance is preserved on the evidence instead.
    assert c1.source_communication_id is None
    assert c1.due_at is not None, "a due-eligible commitment must carry due_at"
    assert c1.evidence["finding_kind"] == "commitment"
    assert c1.evidence["created_from"] == COMM_ID
    assert c1.evidence["why"], "evidence must carry the §22 explanations"

    # -- Phase 2: idempotent replay in the same process. ---------------------
    # A re-ingest of the same communication must NOT schedule a duplicate
    # durable write (the seam is idempotent by the <kind>:<finding_id> key).
    pipeline.ingest(_make_communication(), now=NOW)
    await pipeline.wait_for_commitments()
    assert len(await commitments.list_commitments(person_id=PERSON_ID)) == 1

    # -- Phase 3: simulate a process restart (dispose the engine). -----------
    await engine.dispose()

    # -- Re-create engine + factory the way the app would. --------------------
    engine2 = await _make_engine(db_path)
    registry2 = RepositoryRegistry(make_session_factory(engine2))
    commitments2 = registry2.commitments

    # The durable commitment row survives the restart, unchanged.
    rows2 = await commitments2.list_commitments(person_id=PERSON_ID)
    assert len(rows2) == 1, "the commitment must survive the restart"
    c2 = rows2[0]
    assert (
        c2.id,
        c2.person_id,
        c2.text,
        c2.state,
        c2.source_communication_id,
    ) == (
        snapshot["id"],
        snapshot["person_id"],
        snapshot["text"],
        snapshot["state"],
        snapshot["source_communication_id"],
    )
    assert _same_time(c2.due_at, c1.due_at)
    assert c2.evidence == c1.evidence
    assert c2.evidence["finding_kind"] == "commitment"
    assert c2.evidence["created_from"] == COMM_ID

    await engine2.dispose()


async def test_no_store_is_a_noop():
    """Without an injected store the pipeline performs zero I/O (extend-only).

    ``ingest`` still runs the full deterministic pass and returns the usual
    :class:`PipelineResult`; there is simply nothing durable to flush. This is
    what keeps the default pipeline pure and backward-compatible.
    """
    attention = AttentionService()
    pipeline = CommunicationPipeline(
        detection=DetectionService(attention=attention),
        followups=FollowUpService(attention=attention),
        attention=attention,
    )
    assert pipeline.commitment_store is None

    comm = _make_communication()
    pipeline.detection.register_content(comm.content_ref, TEXT)
    result = pipeline.ingest(comm, now=NOW)  # synchronous — no await.

    # The deterministic pass is intact…
    assert result.communication_id == COMM_ID
    assert any(
        link.finding_kind == "commitment" for link in result.links
    ), result.links
    # …and there is no pending durable work to flush.
    await pipeline.wait_for_commitments()
