"""P0-C / P0-D / P0-I — durable personal-plane pipeline (production activation).

Three related defects in the personal-plane durable persistence, fixed in one
TDD pass (this file fails first, then the implementation lands):

**P0-C — mail-ingest detection sees no text, so the durable mirror never
fires on the worker path.**
``MailIngestWorker`` stores each body in the object store and keeps only a
``content_ref`` on the graph record, but the :class:`DetectionService`
resolves text through an *in-memory* transient registry that nothing on the
worker path ever populates — so mail ingest yields zero findings and zero
durable rows. The fix: the worker reads the body it just wrote back out of
the object store and transiently registers it with the detection service
(before ``run_pipeline``), so detection sees the same text it would have seen
from a registered payload. The text is **transient** — it is never persisted
into the knowledge graph, only the ``content_ref`` pointer is. Detection stays
fully synchronous (no async-in-sync: the worker does the store read in its own
async context).

**P0-D — the mail worker advances its ingest cursor even when the
pipeline's durable writes failed.**
The cursor (and its seen-list fold) must advance *only* after the pipeline's
durable mirror writes have settled. The seam gains an optional async
``flush_durable() -> Exception | None`` that awaits the pending commitment /
follow-up / attention mirror tasks and surfaces the first failure. The worker
gates the cursor on it: on a durable failure the poll is a
``commit_failed`` error — no cursor advance, no seen-list fold — and the
transport re-delivers the same items on the next poll (the seen-list dedup
absorbs the ones that already committed; the mirrors are idempotent).

**P0-I — raw message text leaks into durable records.**
The deterministic detector carries short quoted fragments in
``finding.matched`` / ``finding.why`` (required in the *live* explanations —
``test_detection.py`` asserts on them). Those fragments must never reach the
durable plane: ``CommitmentRow.text`` / ``evidence`` / the follow-up
``FollowupRow.text`` / the durable attention title / body / recommended_action
all carry *templated, provenance-only* strings (detector signal label + ISO
due + deterministic provenance token) instead. Live in-memory records keep the
fragments; only the durable projections are scrubbed.

Fixed-test constraints honored:

- ``tests/backend/test_commitment_restart.py`` is treated as fixed — its
  file-backed SQLite harness creates only ``people`` + ``commitments`` and the
  pipeline must stay pure without a store. Its evidence assertions
  (``finding_kind`` / ``created_from`` / non-empty ``why``) survive the P0-I
  scrub because the durable ``evidence["why"]`` keeps the *templated* entries
  (no matched fragment) and ``CommitmentRow.text`` becomes a template.
- ``test_detection.py`` / ``test_detection_wiring.py`` keep asserting on live
  ``finding.why`` / attention ``reason`` / ``why`` fragments — the scrub is
  durable-side only.
- ``test_mail_ingest.py`` keeps its four-table harness; the seam additions
  (``register_content`` / ``flush_durable``) are optional (getattr-guarded) so
  the existing ``FakeSeam`` stays green.
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
from backend.communications.detection.wiring import CommunicationPipeline
from backend.communications.models import (
    Communication,
    CommunicationChannel,
    CommunicationDirection,
)
from backend.connectors.mail.mail_transport import MailItem
from backend.followups.service import FollowUpService
from backend.ingest.mail_worker import MailIngestWorker
from backend.persistence.connection import make_session_factory
from backend.persistence.models.personal import (
    AttentionItemRow,
    CommitmentRow,
    ConnectionRow,
    FollowupRow,
    IngestCursorRow,
    ImportJobRow,
    PersonRow,
    SecretRow,
)
from backend.persistence.repositories import RepositoryRegistry
from backend.persistence.repositories.connections_repo import IdentitySecretCodec
from backend.storage.memory import InMemoryObjectStore

# Fixed anchor: Tuesday 2026-10-20 21:00 UTC — *past* "tonight" (20:00), so an
# OUTGOING "I'll send it tonight" commitment is already due and gets promoted
# to a live AttentionItem by the due-evaluation hook (weekday/date math is
# deterministic — mirrors test_followup_durable.py).
NOW = datetime(2026, 10, 20, 21, 0, tzinfo=UTC)
TEXT = "I'll send the contract tonight."
COMM_ID = "message:8821"
PERSON_ID = "person:sam"
#: Raw fragments that must never reach the durable plane (P0-I).
FRAGMENTS = ("send the contract", "contract", "tonight")

#: Durable personal-plane tables the pipeline mirror writes into.
_DURABLE_TABLES = (
    PersonRow.__table__,
    CommitmentRow.__table__,
    FollowupRow.__table__,
    AttentionItemRow.__table__,
)
#: The four personal-plane tables the mail worker touches.
_WORKER_TABLES = (
    ConnectionRow.__table__,
    SecretRow.__table__,
    IngestCursorRow.__table__,
    ImportJobRow.__table__,
)

_ACCOUNT = "user@example.com"
_SECRET = "super-secret-password"


async def _make_engine(db_path: Path, tables: tuple) -> AsyncEngine:
    """File-backed scratch engine (``StaticPool`` + *file* DB so ``dispose()``
    does not drop rows — a simulated process restart)."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        # ``run_sync`` passes the sync connection as the ``bind`` positional,
        # mirroring test_commitment_restart.py's working pattern.
        await conn.run_sync(PersonRow.metadata.create_all, tables=list(tables))
    return engine


def _registry(engine: AsyncEngine) -> RepositoryRegistry:
    """A registry over the shared session factory + the single identity codec."""
    return RepositoryRegistry(
        make_session_factory(engine),
        secret_codec=IdentitySecretCodec(),
    )


def _make_pipeline(
    *, attention: AttentionService, followups: FollowUpService
) -> CommunicationPipeline:
    """Fresh pipeline over in-memory services + the registry-wired follow-up
    service (the durable mirror under test)."""
    return CommunicationPipeline(
        detection=DetectionService(attention=attention),
        followups=followups,
        attention=attention,
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


async def _seed_person(registry: RepositoryRegistry) -> None:
    """Seed the FK target the durable mirror writes ``person_id`` against."""
    await registry.communications.create_person(id=PERSON_ID, display_name="Sam")


def _assert_no_raw_text(obj: Any) -> None:
    """Recursively assert none of the raw fragments survive (P0-I)."""
    for frag in FRAGMENTS:
        blob = ""
        stack: list[Any] = [obj]
        while stack:
            cur = stack.pop()
            if isinstance(cur, str):
                blob += cur.lower()
            elif isinstance(cur, dict):
                stack.extend(cur.values())
            elif isinstance(cur, (list, tuple)):
                stack.extend(cur)
        assert frag not in blob, f"raw fragment {frag!r} leaked into {obj!r}"


# --------------------------------------------------------------------------
# P0-C — durable attention mirror through the pipeline
# --------------------------------------------------------------------------


async def test_pipeline_durable_attention_mirror(tmp_path: Path) -> None:
    """A detected, due commitment mirrors a durable CommitmentRow,
    FollowupRow AND (once promoted) an AttentionItemRow — and a same-process
    re-ingest never duplicates any of them."""
    engine = await _make_engine(tmp_path / "p0_plane.db", _DURABLE_TABLES)
    registry = _registry(engine)
    await _seed_person(registry)

    attention = AttentionService()
    followups = FollowUpService(attention=attention, registry=registry)
    pipeline = _make_pipeline(attention=attention, followups=followups)

    pipeline.detection.register_content(f"cr:{COMM_ID}", TEXT)
    result = pipeline.ingest(_make_communication(), now=NOW)  # synchronous.

    assert any(link.finding_kind == "commitment" for link in result.links), (
        result.links
    )
    await followups.wait_for_followups()
    await pipeline.wait_for_commitments()

    assert len(await registry.commitments.list_commitments(person_id=PERSON_ID)) == 1
    assert len(await registry.commitments.list_followups()) == 1
    items = await registry.attention.list_attention_items()
    assert len(items) == 1, items

    # The durable attention item is the follow-up-driven mirror: active,
    # provenance-bearing, no raw text.
    item = items[0]
    assert item.state == "active"
    assert any("commitment" in str(ref) for ref in item.source_refs), (
        item.source_refs
    )
    _assert_no_raw_text(
        {
            "title": item.title,
            "body": item.body,
            "recommended_action": item.recommended_action,
            "source_refs": item.source_refs,
            "escalation_policy": item.escalation_policy,
        }
    )

    # Idempotent replay in the same process: no duplicate rows anywhere.
    pipeline.ingest(_make_communication(), now=NOW)
    await followups.wait_for_followups()
    await pipeline.wait_for_commitments()
    assert len(await registry.commitments.list_commitments(person_id=PERSON_ID)) == 1
    assert len(await registry.commitments.list_followups()) == 1
    assert len(await registry.attention.list_attention_items()) == 1

    await engine.dispose()


# --------------------------------------------------------------------------
# P0-C — the worker resolves text from the object store (transient)
# --------------------------------------------------------------------------


async def _mail_harness(tmp_path: Path) -> tuple[RepositoryRegistry, InMemoryObjectStore, ConnectionRow]:
    """The four-table worker harness (mirrors test_mail_ingest.py) plus a
    seeded IMAP connection + app-password secret."""
    engine = await _make_engine(tmp_path / "p0_mail.db", _WORKER_TABLES)
    registry = _registry(engine)

    conn = await registry.connections.create_connection(
        kind="imap",
        label="Work",
        config={"account": _ACCOUNT, "imap_host": "mail.corp.com", "mailbox": "INBOX"},
        auth_status="authenticated",
        operational_status="never_synced",
        enabled=True,
    )
    await registry.connections.upsert_secret(
        kind="app_password",
        owner_kind="connection",
        owner_id=conn.id,
        plaintext=_SECRET,
    )
    return registry, InMemoryObjectStore(), conn


class _SingleItemTransport:
    """A transport that returns one scripted item on every poll."""

    def __init__(self, items: list[MailItem], next_cursor: dict[str, Any]) -> None:
        self._items = list(items)
        self._next = dict(next_cursor)
        self.calls = 0

    async def run(
        self,
        connector: Any,
        cursor: dict[str, Any],
        *,
        credentials: dict[str, Any] | None = None,
    ) -> tuple[list[MailItem], dict[str, Any]]:
        self.calls += 1
        return list(self._items), dict(self._next)


def _mail_item(message_id: str, body: str) -> MailItem:
    return MailItem(
        message_id=message_id,
        account=_ACCOUNT,
        subject="Deploy by Friday",
        content_bytes=body.encode("utf-8"),
        timestamp=NOW,
        headers={
            "From": "boss@corp.com",
            "Message-ID": message_id,
            "Date": "Mon, 27 Oct 2026 09:00:00 +0000",
        },
    )


async def test_worker_registers_body_from_object_store(tmp_path: Path) -> None:
    """P0-C: after storing a body in the object store, the worker reads it
    back and transiently registers it with the detection service before
    ``run_pipeline`` — so detection sees the text, while only the
    ``content_ref`` pointer is kept on the graph record."""

    class _RecordingSeam:
        def __init__(self) -> None:
            self.registered: list[tuple[str, str]] = []
            self.ingests: list[dict[str, Any]] = []
            self.pipeline_calls: list[Any] = []

        def register_content(self, content_ref: str, text: str) -> None:
            self.registered.append((content_ref, text))

        def ingest_communication(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.ingests.append(payload)
            communication = SimpleNamespace(
                communication_id=payload["communication_id"],
                content_ref=payload.get("content_ref"),
                channel=payload.get("channel"),
                timestamp=payload.get("timestamp"),
            )
            return {"communication": communication, "conversation": None, "created": True}

        def run_pipeline(self, communication: Any, *, now: Any) -> Any:
            self.pipeline_calls.append(communication)
            return None

    registry, store, conn = await _mail_harness(tmp_path)
    seam = _RecordingSeam()
    transport = _SingleItemTransport(
        [_mail_item("<deploy@corp>", "Please deploy by Friday")], {"uid": 42}
    )
    worker = MailIngestWorker(
        connection_id=conn.id,
        cursor_kind="imap_seq",
        connections=registry.connections,
        imports=registry.imports,
        object_store=store,
        transport=transport,
        seam=seam,
        poll_interval_s=30.0,
    )

    await worker.poll_once()
    summary = worker.status()["last"]
    assert summary["status"] == "ok", summary
    assert summary["ingested"] == 1

    # The worker read the body back from the object store and registered it
    # transiently under the content_ref pointer (never the raw bytes inline).
    assert len(seam.registered) == 1, seam.registered
    ref, text = seam.registered[0]
    assert ref.startswith("content://"), ref
    assert text == "Please deploy by Friday", text

    # The graph record kept only the pointer, not the body.
    assert len(seam.ingests) == 1
    assert seam.ingests[0]["content_ref"] == ref
    assert "Please deploy by Friday" not in str(seam.ingests[0])

    await _dispose(registry)


# --------------------------------------------------------------------------
# P0-I — durable records carry templated, provenance-only text
# --------------------------------------------------------------------------


async def test_durable_records_carry_no_raw_text(tmp_path: Path) -> None:
    """P0-I: raw message-text fragments (``finding.matched`` / the quoted part
    of ``finding.why``) never reach the durable plane — commitments,
    follow-ups and attention rows carry templated provenance only — while the
    live in-memory findings keep their explainable fragments."""
    engine = await _make_engine(tmp_path / "p0_scrub.db", _DURABLE_TABLES)
    registry = _registry(engine)
    await _seed_person(registry)

    attention = AttentionService()
    followups = FollowUpService(attention=attention, registry=registry)
    pipeline = _make_pipeline(attention=attention, followups=followups)

    pipeline.detection.register_content(f"cr:{COMM_ID}", TEXT)
    pipeline.ingest(_make_communication(), now=NOW)
    await followups.wait_for_followups()
    await pipeline.wait_for_commitments()

    # Live findings still carry the explainable fragment (test_detection.py
    # contract) — the scrub is durable-side only.
    live = pipeline.detection.report_for(COMM_ID)
    assert live is not None
    finding = live.findings[0]
    live_blob = " ".join([*finding.why, finding.matched]).lower()
    assert any(frag in live_blob for frag in FRAGMENTS), (
        "live finding must stay explainable with its fragment"
    )

    # Every durable record is scrubbed (no fragment anywhere, recursively).
    commitment_rows = await registry.commitments.list_commitments(person_id=PERSON_ID)
    assert commitment_rows, "a durable commitment must exist"
    for row in commitment_rows:
        _assert_no_raw_text(row.text)
        _assert_no_raw_text(row.evidence)
        assert "matched" not in row.evidence, row.evidence
    for row in await registry.commitments.list_followups():
        _assert_no_raw_text(row.text)
        _assert_no_raw_text(row.linked_refs)
    for row in await registry.attention.list_attention_items():
        _assert_no_raw_text(
            {"title": row.title, "body": row.body, "source_refs": row.source_refs}
        )

    # Provenance survives the scrub: the deterministic finding key + a
    # templated, non-empty why.
    evidence = commitment_rows[0].evidence
    assert evidence["finding_id"] == "commitment-message:8821"
    assert evidence["finding_kind"] == "commitment"
    assert evidence["created_from"] == COMM_ID
    assert evidence["why"], "durable evidence keeps templated explanations"

    await engine.dispose()


# --------------------------------------------------------------------------
# P0-D — cursor advances only when the pipeline's durable writes succeed
# --------------------------------------------------------------------------


class _FailingFlushSeam:
    """A seam whose ``flush_durable`` surfaces a durable-write failure — the
    commit-gate contract under test (P0-D)."""

    def __init__(self, failure: Exception) -> None:
        self._failure = failure
        self.ingests: list[dict[str, Any]] = []
        self.pipeline_calls: list[Any] = []

    def ingest_communication(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.ingests.append(payload)
        communication = SimpleNamespace(
            communication_id=payload["communication_id"],
            content_ref=payload.get("content_ref"),
            channel=payload.get("channel"),
            timestamp=payload.get("timestamp"),
        )
        return {"communication": communication, "conversation": None, "created": True}

    def run_pipeline(self, communication: Any, *, now: Any) -> Any:
        self.pipeline_calls.append(communication)
        return None

    async def flush_durable(self) -> Exception | None:
        return self._failure


async def test_cursor_holds_when_commit_fails(tmp_path: Path) -> None:
    """P0-D: when the pipeline's durable mirror writes fail, the worker's
    poll is a ``commit_failed`` error — the durable cursor and its seen list
    do NOT advance (the transport re-delivers the same items; the seen-list
    dedup absorbs it)."""
    registry, store, conn = await _mail_harness(tmp_path)
    seam = _FailingFlushSeam(RuntimeError("durable mirror failed: FK violation"))
    transport = _SingleItemTransport(
        [_mail_item("<1@corp>", "hello"), _mail_item("<2@corp>", "hi")], {"uid": 99}
    )
    worker = MailIngestWorker(
        connection_id=conn.id,
        cursor_kind="imap_seq",
        connections=registry.connections,
        imports=registry.imports,
        object_store=store,
        transport=transport,
        seam=seam,
        poll_interval_s=30.0,
    )

    delay = await worker.poll_once()
    status = worker.status()["last"]
    assert status["status"] == "error", status
    assert status.get("error_code") == "commit_failed", status

    # The durable cursor did NOT advance: still no row (first poll) and the
    # seen list was NOT folded back.
    row = await registry.imports.get_cursor(conn.id, "imap_seq")
    assert row is None or not row.cursor_value.get("seen"), row.cursor_value
    if row is not None:
        assert row.cursor_value.get("uid") != 99

    # The connection records the commit failure (error + commit_failed).
    updated = await registry.connections.get_connection(conn.id)
    assert updated.operational_status == "error"
    assert updated.last_error_code == "commit_failed"

    # The failure path applied backoff, not the idle poll interval.
    assert delay < 30.0

    await _dispose(registry)


async def _dispose(registry: RepositoryRegistry) -> None:
    """Best-effort teardown: the scratch engines are file-backed and cleaned up
    by ``tmp_path``; this keeps the helper symmetric with the harness yield."""
    return None
