"""Durable follow-up / attention seam tests (prompt2 R104–R106, fix 3).

Extends the durable commitment seam (``tests/backend/test_commitment_restart.py``)
to the follow-up and attention planes: a detected OUTGOING commitment that is
follow-up-eligible must (1) mirror a durable ``CommitmentRow`` (the existing
seam), (2) mirror a durable ``FollowupRow`` from the follow-up service, and
(3) — once due — persist a durable ``AttentionItemRow``. The mirror is
**idempotent**: re-ingesting the same communication in the same process never
duplicates a row (in-process dedup by the deterministic ``created_from`` key),
and re-ingesting through a *fresh* process after a simulated restart never
duplicates either (durable-side dedup by the same ``created_from`` token).

The follow-up service stays pure unless a ``RepositoryRegistry`` is injected —
mirroring the :class:`DurableCommitmentStore` protocol, the service talks only
to the repository and never imports the persistence layer. The SQLite scratch
DB is *file-backed* (``StaticPool``) so ``dispose()`` does not drop the rows,
which is exactly what a real process restart relies on.

Circular-import-safe entry: import ``backend.api.app`` first (as
``test_detection_wiring.py`` / ``test_commitment_restart.py`` do) so
``backend.attention`` is fully initialized before
``backend.attention.service`` is imported directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
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
)
from backend.communications.models import (
    Communication,
    CommunicationChannel,
    CommunicationDirection,
)
from backend.followups.service import FollowUpService
from backend.persistence.connection import make_session_factory
from backend.persistence.models.personal import (
    AttentionItemRow,
    CommunicationRow,
    CommitmentRow,
    FollowupRow,
    PersonRow,
)
from backend.persistence.repositories import RepositoryRegistry
from backend.persistence.repositories.communications_repo import (
    CommunicationsRepository,
)
from backend.persistence.repositories.connections_repo import IdentitySecretCodec

# Fixed anchor: Tuesday 2026-10-20 21:00 UTC — *past* "tonight" (20:00), so an
# OUTGOING "I'll send it tonight" commitment is already due and gets promoted
# to a live AttentionItem by the due-evaluation hook (weekday/date math is
# deterministic — mirrors test_detection_wiring.py / test_commitment_restart.py).
NOW = datetime(2026, 10, 20, 21, 0, tzinfo=UTC)
TEXT = "I'll send it tonight."
COMM_ID = "message:8821"
PERSON_ID = "person:sam"

_TABLES = (
    PersonRow.__table__,
    CommunicationRow.__table__,
    CommitmentRow.__table__,
    FollowupRow.__table__,
    AttentionItemRow.__table__,
)


async def _make_engine(db_path: Path) -> AsyncEngine:
    """File-backed scratch engine over the durable personal-plane tables."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        # ``run_sync`` passes the sync connection as the ``bind`` positional,
        # mirroring test_p0_pipeline.py's working pattern.
        await conn.run_sync(PersonRow.metadata.create_all, tables=list(_TABLES))
    return engine


def _registry(engine: AsyncEngine) -> RepositoryRegistry:
    """A registry over the shared session factory + the *single* identity codec."""
    return RepositoryRegistry(
        make_session_factory(engine),
        secret_codec=IdentitySecretCodec(),
    )


def _make_pipeline(
    *, attention: AttentionService, followups: FollowUpService,
) -> CommunicationPipeline:
    """Compose a fresh pipeline over the given in-memory services.

    The durable sink (``registry.commitments``) and the durable follow-up
    mirror (``registry``) are the only durable surface — injected, so the
    pipeline / follow-up service never import the persistence layer.
    """
    return CommunicationPipeline(
        detection=DetectionService(attention=attention),
        followups=followups,
        attention=attention,
        commitment_store=followups.registry,
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
    await comms.create_person(id=person_id, display_name="Sam")


def _flush(pipeline: CommunicationPipeline, followups: FollowUpService) -> None:
    """Synchronous caller path: the durable writes were scheduled inline
    (``asyncio.run``) because ``ingest`` ran without a running loop. A helper
    no-op kept for symmetry with the running-loop path."""
    return None


async def test_followup_attention_durable_and_idempotent(tmp_path: Path):
    """A detected commitment mirrors a durable follow-up and, once due, an
    attention item — and re-ingesting (same process) never duplicates them."""
    db_path = tmp_path / "followup_plane.db"
    engine = await _make_engine(db_path)
    registry = _registry(engine)
    comms: CommunicationsRepository = registry.communications

    await _seed_person(comms, PERSON_ID)

    attention = AttentionService()
    followups = FollowUpService(attention=attention, registry=registry)
    pipeline = _make_pipeline(attention=attention, followups=followups)

    # Register the transient plaintext so the deterministic detector sees it
    # (mirrors test_detection_wiring.ingest_text); content is never persisted.
    pipeline.detection.register_content(f"cr:{COMM_ID}", TEXT)
    result = pipeline.ingest(_make_communication(), now=NOW)  # synchronous.

    assert any(link.finding_kind == "commitment" for link in result.links), result.links
    await followups.wait_for_followups()
    await pipeline.wait_for_commitments()

    # -- the durable row set: exactly one of each --------------------------------
    commitments = await registry.commitments.list_commitments(person_id=PERSON_ID)
    assert len(commitments) == 1, commitments
    assert commitments[0].state == "open"

    followups_rows = await registry.commitments.list_followups()
    assert len(followups_rows) == 1, followups_rows
    assert followups_rows[0].state == "open"
    assert followups_rows[0].person_id == PERSON_ID
    # The deterministic provenance token is persisted for cross-restart dedup.
    assert any(
        isinstance(r, dict) and r.get("created_from")
        for r in followups_rows[0].linked_refs
    ), followups_rows[0].linked_refs

    attention_rows = await registry.attention.list_attention_items()
    assert len(attention_rows) == 1, attention_rows
    assert attention_rows[0].state == "active"
    assert attention_rows[0].title  # a real headline, not empty

    # -- replay in the SAME process: no new rows --------------------------------
    result2 = pipeline.ingest(_make_communication(), now=NOW)  # same clock
    assert result2.followup_ids == result.followup_ids  # reused, not re-created
    await followups.wait_for_followups()
    await pipeline.wait_for_commitments()

    assert len(await registry.commitments.list_commitments(person_id=PERSON_ID)) == 1
    assert len(await registry.commitments.list_followups()) == 1
    assert len(await registry.attention.list_attention_items()) == 1

    # -- simulated restart: dispose + re-open; rows survive ---------------------
    await engine.dispose()
    engine2 = await _make_engine(db_path)
    registry2 = _registry(engine2)
    assert len(await registry2.commitments.list_commitments(person_id=PERSON_ID)) == 1
    assert len(await registry2.commitments.list_followups()) == 1
    assert len(await registry2.attention.list_attention_items()) == 1

    # -- replay through a FRESH process: still no duplicates --------------------
    attention2 = AttentionService()
    followups2 = FollowUpService(attention=attention2, registry=registry2)
    pipeline2 = _make_pipeline(attention=attention2, followups=followups2)
    pipeline2.detection.register_content(f"cr:{COMM_ID}", TEXT)
    pipeline2.ingest(_make_communication(), now=NOW)
    await followups2.wait_for_followups()
    await pipeline2.wait_for_commitments()

    assert len(await registry2.commitments.list_commitments(person_id=PERSON_ID)) == 1
    assert len(await registry2.commitments.list_followups()) == 1
    assert len(await registry2.attention.list_attention_items()) == 1

    await engine2.dispose()


async def test_no_registry_is_pure(tmp_path: Path):
    """With no ``RepositoryRegistry`` injected the service is pure — zero I/O,
    and the in-memory follow-up / attention behavior is unchanged."""
    attention = AttentionService()
    followups = FollowUpService(attention=attention)  # no registry
    pipeline = _make_pure_pipeline(attention=attention, followups=followups)

    pipeline.detection.register_content(f"cr:{COMM_ID}", TEXT)
    result = pipeline.ingest(_make_communication(), now=NOW)

    assert result.followup_ids, result  # a follow-up is still created in-memory
    assert followups.registry is None
    # No durable write was scheduled (no registry).
    await followups.wait_for_followups()


def _make_pure_pipeline(*, attention: AttentionService, followups: FollowUpService):
    """A pipeline with NO durable store — the pure in-memory path."""
    return CommunicationPipeline(
        detection=DetectionService(attention=attention),
        followups=followups,
        attention=attention,
        commitment_store=None,
    )
