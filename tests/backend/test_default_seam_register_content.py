"""S1 — the production ``DefaultIngestSeam`` forwards the stored body to the
detector (register_content passthrough).

The regression this file guards: in production the mail worker tries to hand
the body it just stored to the detector via
``register = getattr(self._seam, "register_content", None)``
(``backend/ingest/mail_worker.py`` ~896) and only registers when it is not
``None``. The production seam is :class:`DefaultIngestSeam`; before the fix it
had no ``register_content`` method, so ``register`` was ``None``, the body was
never registered, ``DetectionService._text_for`` resolved ``""``, and the
detector produced **zero findings** — leaving the entire post-ingest pipeline
dead even though mail persisted durably.

Unlike ``test_p0_pipeline.py`` (which drives a hand-written ``_RecordingSeam``
double and therefore passes regardless of the production seam), these tests
exercise the real :class:`DefaultIngestSeam`:

- ``test_default_seam_registers_nonempty_body`` — drive the worker's ingest
  path end-to-end with the production seam over an actionable INCOMING mail and
  assert the detection pipeline is invoked with the NON-EMPTY body (not ``""``)
  and returns a non-empty :class:`DetectionReport`. Direct regression for the
  missing passthrough.
- ``test_outgoing_due_message_lands_durable_commitment`` — an end-to-end
  detection→durable proof: feed a seeded OUTGOING+due commitment phrase through
  the seam so a row lands in the durable ``commitments`` (and ``followups``)
  table, proving the whole detection→durable path works once text flows.
- ``test_incoming_mail_yields_request_attention_no_error`` — the current live
  mail mix is all INCOMING, which can only yield ``request`` findings → in-
  memory attention (not durable commitments); confirm it degrades gracefully
  (findings produced, attention item fed, no error).

Harness mirrors ``test_p0_pipeline.py`` / ``test_followup_durable.py``:
file-backed SQLite (``StaticPool``) scratch engines, a durable
``RepositoryRegistry``, and the in-memory object store. The seam's lazy
``pipeline`` property is primed with a pipeline composed over the test's
durable registry (so no process-wide singleton is mutated).

Circular-import-safe entry: import ``backend.api.app`` first so
``backend.attention`` is fully initialized before ``backend.attention.service``
is imported directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

# Circular-import-safe entry (see test_p0_pipeline.py): force backend.api.app
# (and with it backend.attention) to fully initialize before
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
from backend.ingest.mail_worker import DefaultIngestSeam, MailIngestWorker
from backend.persistence.connection import make_session_factory
from backend.persistence.models.personal import (
    AttentionItemRow,
    CommitmentRow,
    ConnectionRow,
    FollowupRow,
    ImportJobRow,
    IngestCursorRow,
    PersonRow,
    SecretRow,
)
from backend.persistence.repositories import RepositoryRegistry
from backend.persistence.repositories.connections_repo import IdentitySecretCodec
from backend.storage.memory import InMemoryObjectStore

# Fixed anchor: Tuesday 2026-10-20 21:00 UTC — *past* "tonight" (20:00), so an
# OUTGOING "I'll send it tonight" commitment is already due (mirrors
# test_followup_durable.py).
NOW = datetime(2026, 10, 20, 21, 0, tzinfo=UTC)

_ACCOUNT = "user@example.com"
_SECRET = "super-secret-password"

#: Durable personal-plane tables the detection mirror writes into.
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


async def _make_engine(db_path: Path, tables: tuple) -> AsyncEngine:
    """File-backed scratch engine (``StaticPool`` + a *file* DB)."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(PersonRow.metadata.create_all, tables=list(tables))
    return engine


def _registry(engine: AsyncEngine) -> RepositoryRegistry:
    return RepositoryRegistry(
        make_session_factory(engine),
        secret_codec=IdentitySecretCodec(),
    )


def _durable_pipeline(registry: RepositoryRegistry) -> tuple[
    CommunicationPipeline, AttentionService, FollowUpService
]:
    """A fresh pipeline whose commitment + follow-up mirrors are bound to the
    test's durable registry (so detected commitments/follow-ups persist)."""
    attention = AttentionService()
    followups = FollowUpService(attention=attention, registry=registry)
    pipeline = CommunicationPipeline(
        detection=DetectionService(attention=attention),
        followups=followups,
        attention=attention,
        commitment_store=registry.commitments,
    )
    return pipeline, attention, followups


def _bind_seam_pipeline(
    seam: DefaultIngestSeam, pipeline: CommunicationPipeline
) -> None:
    """Prime the seam's lazy ``pipeline`` property with a test pipeline so the
    production ``register_content`` / ``run_pipeline`` forward to it without
    touching the process-wide singletons."""
    seam._pipeline = pipeline


async def _mail_harness(
    tmp_path: Path, db_name: str
) -> tuple[RepositoryRegistry, InMemoryObjectStore, ConnectionRow]:
    """The four-table worker harness (mirrors test_p0_pipeline.py) plus a
    seeded IMAP connection + app-password secret."""
    engine = await _make_engine(tmp_path / db_name, _WORKER_TABLES)
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
        subject="Please review",
        content_bytes=body.encode("utf-8"),
        timestamp=NOW,
        headers={
            "From": "boss@corp.com",
            "Message-ID": message_id,
            "Date": "Mon, 27 Oct 2026 09:00:00 +0000",
        },
    )


# --------------------------------------------------------------------------
# (i) direct regression: the production seam forwards the NON-EMPTY body
# --------------------------------------------------------------------------


async def test_default_seam_registers_nonempty_body(tmp_path: Path) -> None:
    """The worker driving the production ``DefaultIngestSeam`` reads the body
    back out of the object store and registers it with the detector, so the
    pipeline is invoked on the NON-EMPTY text (not ``""``) and returns a
    non-empty DetectionReport."""
    registry, store, conn = await _mail_harness(tmp_path, "seam_i.db")
    pipeline, _attention, _followups = _durable_pipeline(registry)
    seam = DefaultIngestSeam()
    _bind_seam_pipeline(seam, pipeline)

    body = "Can you please send me the Q3 report by Friday?"
    transport = _SingleItemTransport([_mail_item("<ask@corp>", body)], {"uid": 7})
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

    # The detector saw the real body, not "": a report exists and the resolver
    # resolves the exact text under the content_ref the seam registered.
    detection = pipeline.detection
    comm_id = next(iter(detection._reports))  # the single ingested message
    report = detection.report_for(comm_id)
    assert report is not None, "detection must have run on the ingested mail"
    assert detection._text_for(
        detection._communications[comm_id]
    ) == body, "the seam must register the non-empty body under the content_ref"
    # An actionable incoming request yields at least one finding.
    assert report.findings, report


# --------------------------------------------------------------------------
# (ii) end-to-end detection → durable proof through the production seam
# --------------------------------------------------------------------------


async def test_outgoing_due_message_lands_durable_commitment(tmp_path: Path) -> None:
    """A seeded OUTGOING+due commitment phrase fed through the production seam
    (register_content → run_pipeline) lands a durable row in ``commitments``
    and ``followups`` — proving the detection→durable path once text flows."""
    engine = await _make_engine(tmp_path / "seam_ii.db", _DURABLE_TABLES)
    registry = _registry(engine)
    await registry.communications.create_person(id="person:sam", display_name="Sam")

    pipeline, _attention, followups = _durable_pipeline(registry)
    seam = DefaultIngestSeam()
    _bind_seam_pipeline(seam, pipeline)

    content_ref = "content://cr/outgoing-1"
    communication = Communication(
        communication_id="comm-outgoing-1",
        channel=CommunicationChannel.SMS,
        direction=CommunicationDirection.OUTGOING,
        sender="me",
        person_id="person:sam",
        timestamp=NOW,
        content_ref=content_ref,
    )

    # Exactly the worker's order: register the transient body, then run.
    seam.register_content(content_ref, "I'll send the contract tonight.")
    seam.run_pipeline(communication, now=NOW)

    await followups.wait_for_followups()
    await pipeline.wait_for_commitments()

    commitments = await registry.commitments.list_commitments(person_id="person:sam")
    assert len(commitments) == 1, commitments
    followup_rows = await registry.commitments.list_followups()
    assert len(followup_rows) == 1, followup_rows

    await engine.dispose()


# --------------------------------------------------------------------------
# (iii) INCOMING mail degrades gracefully to request/attention (no error)
# --------------------------------------------------------------------------


async def test_incoming_mail_yields_request_attention_no_error(
    tmp_path: Path,
) -> None:
    """The current live mail mix is all INCOMING: through the production seam
    it must yield ``request``-type findings that become in-memory attention
    items (not durable commitments) and must NOT error."""
    registry, store, conn = await _mail_harness(tmp_path, "seam_iii.db")
    pipeline, attention, _followups = _durable_pipeline(registry)
    seam = DefaultIngestSeam()
    _bind_seam_pipeline(seam, pipeline)

    body = "Could you please approve the budget by tomorrow?"
    transport = _SingleItemTransport([_mail_item("<req@corp>", body)], {"uid": 11})
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

    detection = pipeline.detection
    requests = detection.all_findings(kind="request")
    assert requests, "an actionable incoming mail yields a request finding"
    # Incoming requests feed in-memory attention, never durable commitments.
    assert attention.list(), "the request must feed an in-memory attention item"
