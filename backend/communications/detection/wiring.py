"""R106 — end-to-end wiring: detection → follow-ups → attention.

AC-R06 / AC-R07 / AC-R15: prove that the R103 detection service, the R104
follow-up service, and the R108 attention engine compose into one
deterministic, traceable ingest path without losing provenance.

Design notes
------------
- **Pure orchestration, no globals on the class.**
  :class:`CommunicationPipeline` takes its three services through the
  constructor, so tests compose fresh in-memory instances (no network, no
  database, no app import for the class itself). The module-level
  :func:`get_pipeline` convenience is the *only* place that touches the
  existing process-wide singletons — the app seam (see below) binds to it.
- **Follow-up idempotency via a deterministic key derived from the finding.**
  ``FollowUpService.create()`` mints its own id, so the pipeline derives a
  stable provenance key from the finding — ``created_from =
  "<kind>:<finding_id>"`` — and the finding id is itself deterministic
  (``<kind>-<communication_id>``, R103). Before creating, the pipeline
  looks up any existing follow-up whose target and key match and reuses it,
  so re-ingesting the same communication never duplicates a follow-up.
- **One ingest = one pass.** (1) detection over the normalized
  :class:`~backend.communications.models.Communication`; (2) follow-up
  creation for each detected OUTGOING commitment/request that carries a
  due/trigger time; (3) ``evaluate_due(now)``; (4) a
  :class:`PipelineResult` carrying every id — finding → follow-up →
  attention — so the §61 scenario is traceable end-to-end.
- **App seam (the one line the integrator needs).** In the
  ``POST /api/communications/ingest`` handler
  (``backend/api/endpoints/communications.py::ingest_communication``), after
  the communication is persisted and its content registered with the
  detection service, call::

      get_pipeline().ingest(communication, now=communication.timestamp)

  Detection's §8 events, its own attention seam (AC-R06), follow-up
  creation, and the due-evaluation promotion all flow through the same
  singletons ``get_pipeline`` binds — no other wiring is required.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Protocol, Sequence

from pydantic import BaseModel, Field

from backend.attention.service import AttentionService, get_attention_service
from backend.communications.detection.service import (
    DetectionService,
    get_detection_service,
)
from backend.communications.models import Communication, CommunicationDirection
from backend.followups.service import FollowUpService, get_followup_service

_logger = logging.getLogger(__name__)


def _ensure_utc(value: datetime | None) -> datetime:
    """Normalize a clock to UTC; ``None`` means "wall clock now"."""
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


#: Finding kinds that open an unanswered outgoing loop (§57). Commitments
#: only arise from outgoing communications and requests from incoming ones
#: in the R103 extractor, so this plus the direction check below means:
#: "an outgoing promise or ask that is due at a specific time".
_FOLLOWUP_KINDS = ("commitment", "request")

#: AttentionItem statuses that still show on a feed (mirror of
#: ``FollowUpService._upsert_attention``). SUPPRESSED / RESOLVED items are
#: not live and are never reported as live follow-up attention.
_LIVE_ATTENTION_STATUSES = ("NOW", "TODAY", "SOON", "WAITING_ON", "FYI")


class DurableCommitmentStore(Protocol):
    """Durable personal-plane sink for detected commitments (extend-only).

    The :class:`CommunicationPipeline` is pure by default — it performs no
    I/O unless a store is injected. When one is supplied, each detected
    commitment finding is mirrored into the durable ``commitments`` table so
    the commitment survives a process restart. The pipeline talks only to this
    protocol, so the concrete ``CommitmentsRepository`` (or any test double)
    can satisfy it without the pipeline importing the persistence layer.
    """

    async def create_commitment(
        self,
        *,
        person_id: str,
        text: str,
        due_at: datetime | None = None,
        state: str = "open",
        evidence: dict[str, Any] | None = None,
        source_communication_id: str | None = None,
    ) -> Any:
        """Persist one commitment row; returns the stored row."""
        ...


class FollowUpLink(BaseModel):
    """One traceable link: detected finding → follow-up → (optionally)
    the live AttentionItem that ``evaluate_due`` promoted."""

    finding_id: str
    finding_kind: str
    followup_id: str
    created: bool = Field(
        description="False when a replay reused the pre-existing follow-up."
    )
    attention_id: str | None = Field(
        default=None,
        description=(
            "Live AttentionItem id after the due evaluation (None until the "
            "follow-up is due and promoted, or while terminal)."
        ),
    )


class PipelineResult(BaseModel):
    """End-to-end trace for one :meth:`CommunicationPipeline.ingest` pass.

    Every id the pipeline touched is carried here so the §61 scenario is
    auditable: which findings were detected, which follow-up each produced
    (and whether it was newly created or reused on replay), which
    follow-ups the due evaluation promoted, and which AttentionItems are
    live as a result.
    """

    communication_id: str
    detected_at: datetime
    #: R103 — every finding this communication produced, in extractor order.
    finding_ids: list[str] = Field(default_factory=list)
    #: R103's own attention seam (AC-R06): finding_id → AttentionItem id.
    detection_attention_ids: dict[str, str] = Field(default_factory=dict)
    #: R104 — follow-ups created or reused for the follow-up-eligible findings.
    followup_ids: list[str] = Field(default_factory=list)
    #: Follow-up-driven live AttentionItem ids (post ``evaluate_due``), in
    #: link order, deduplicated. Detection-seam items are in
    #: ``detection_attention_ids`` instead.
    attention_ids: list[str] = Field(default_factory=list)
    #: Per-finding trace (finding → follow-up → attention).
    links: list[FollowUpLink] = Field(default_factory=list)
    #: Raw ``evaluate_due`` records ``{followup, attention, event,
    #: days_overdue}`` — the AC-R07 promotion evidence.
    evaluated: list[dict[str, Any]] = Field(default_factory=list)


class CommunicationPipeline:
    """Deterministic orchestration of detection → follow-ups → attention.

    No globals, no I/O by default: the three core services are injected, and
    an optional :class:`DurableCommitmentStore` may be injected to mirror
    detected commitments into the durable personal plane. With no store given,
    the pipeline performs zero I/O and behaves exactly as before — the
    injected store is the *only* path to durability. A pinned ``now`` makes
    the whole pass reproducible.
    """

    def __init__(
        self,
        *,
        detection: DetectionService,
        followups: FollowUpService,
        attention: AttentionService,
        commitment_store: DurableCommitmentStore | None = None,
    ) -> None:
        self._detection = detection
        self._followups = followups
        self._attention = attention
        self._commitment_store = commitment_store
        #: Commitment finding keys (``<kind>:<finding_id>``) already mirrored
        #: to the durable store, so re-ingesting the same communication in
        #: process never schedules a duplicate write (mirrors the follow-up
        #: idempotency convention).
        self._persisted_commitments: set[str] = set()
        #: Background tasks persisting commitments; flushed by
        #: :meth:`wait_for_commitments`.
        self._commitment_tasks: list[asyncio.Task[None]] = []

    # -- injected services --------------------------------------------------
    @property
    def detection(self) -> DetectionService:
        return self._detection

    @property
    def followups(self) -> FollowUpService:
        return self._followups

    @property
    def attention(self) -> AttentionService:
        return self._attention

    @property
    def commitment_store(self) -> DurableCommitmentStore | None:
        """The injected durable commitment sink, or ``None`` (no I/O)."""
        return self._commitment_store

    # -- ingest ------------------------------------------------------------
    def ingest(
        self,
        communication: Communication,
        *,
        now: datetime | None = None,
    ) -> PipelineResult:
        """Run the full §61 pass over one normalized communication.

        1. Detect (R103) — findings + §8 events + the detection's own
           attention seam.
        2. For each detected OUTGOING commitment/request with a due/trigger
           time, ensure a follow-up exists (R104) — idempotent by the
           deterministic ``<kind>:<finding_id>`` key, so a replay never
           duplicates.
        3. Evaluate due follow-ups at ``now`` (AC-R07) — promotes each due
           follow-up to exactly one live AttentionItem.
        4. Return a :class:`PipelineResult` with every id linked.
        """
        clock = _ensure_utc(now)
        report = self._detection.detect(communication, now=clock)

        # Extend-only durability seam: mirror each detected commitment into
        # the durable personal plane (no-op when no store is injected).
        # ``ingest`` stays synchronous — writes run in the background, never
        # awaited here, so the call-site contract (no ``await``) is preserved.
        self._persist_commitments(
            report.findings,
            person_id=communication.person_id,
        )

        links: list[FollowUpLink] = []
        for finding in report.findings:
            if not self._follows_up(finding, communication):
                continue
            links.append(
                self._ensure_followup(finding, person_id=communication.person_id)
            )

        evaluated = self._followups.evaluate_due(now=clock)

        # Refresh each link's *live* attention id from the follow-up record
        # (``_upsert_attention`` sets ``followup.attention_id``). A terminal
        # follow-up keeps the stale id of its resolved item — that item is
        # not live, so the link (and ``attention_ids``) stays None.
        for link in links:
            fu = self._followups.get(link.followup_id)
            item = (
                self._attention.get(fu.attention_id)
                if fu is not None and fu.attention_id
                else None
            )
            link.attention_id = (
                item.attention_id
                if item is not None
                and item.status in _LIVE_ATTENTION_STATUSES
                else None
            )
        attention_ids = list(dict.fromkeys(
            link.attention_id for link in links if link.attention_id
        ))

        return PipelineResult(
            communication_id=communication.communication_id,
            detected_at=clock,
            finding_ids=[f.finding_id for f in report.findings],
            detection_attention_ids=dict(report.attention_ids),
            followup_ids=[link.followup_id for link in links],
            attention_ids=attention_ids,
            links=links,
            evaluated=list(evaluated),
        )

    # -- follow-up eligibility / idempotency --------------------------------
    @staticmethod
    def _follows_up(finding: Any, communication: Communication) -> bool:
        """OUTGOING commitment/request findings with an explicit due time
        become follow-ups; open-ended promises and everything else do not."""
        return (
            finding.kind in _FOLLOWUP_KINDS
            and finding.due is not None
            and communication.direction is CommunicationDirection.OUTGOING
        )

    def _ensure_followup(self, finding: Any, *, person_id: str | None) -> FollowUpLink:
        """Create the follow-up for ``finding`` — or reuse the one a previous
        ingest already created (deterministic key derived from the finding)."""
        key = f"{finding.kind}:{finding.finding_id}"
        existing = next(
            (
                fu
                for fu in self._followups.list()
                if fu.target_kind == finding.kind
                and fu.target_id == finding.finding_id
                and (fu.created_from or "") == key
            ),
            None,
        )
        if existing is not None:
            return FollowUpLink(
                finding_id=finding.finding_id,
                finding_kind=finding.kind,
                followup_id=existing.id,
                created=False,
                attention_id=existing.attention_id,
            )
        created = self._followups.create(
            target_kind=finding.kind,
            target_id=finding.finding_id,
            reason=f"Follow through: {finding.matched or finding.why[0]}",
            person_id=person_id,
            due_at=finding.due,
            created_from=key,
        )
        fu = created["followup"]
        return FollowUpLink(
            finding_id=finding.finding_id,
            finding_kind=finding.kind,
            followup_id=fu.id,
            created=True,
            attention_id=fu.attention_id,
        )

    # -- durable commitment seam (extend-only) ------------------------------
    def _persist_commitments(
        self,
        findings: Sequence[Any],
        *,
        person_id: str | None,
    ) -> None:
        """Mirror detected commitment findings into the durable store.

        No-op unless a :class:`DurableCommitmentStore` was injected. The
        pipeline is synchronous and must stay so (its callers invoke
        ``ingest`` without ``await``), so each write is scheduled as a
        background task rather than awaited. Writes are idempotent by the
        ``<kind>:<finding_id>`` key so a replay in the same process never
        schedules a duplicate row.
        """
        store = self._commitment_store
        if store is None:
            return

        for finding in findings:
            if getattr(finding, "kind", None) != "commitment":
                continue
            key = f"{finding.kind}:{finding.finding_id}"
            if key in self._persisted_commitments:
                continue
            self._persisted_commitments.add(key)
            coro = self._persist_one_commitment(
                finding,
                person_id=person_id,
                store=store,
            )
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No running loop (a synchronous caller): settle the write
                # inline so durability still lands, then move on.
                asyncio.run(coro)
                continue
            task = loop.create_task(coro)
            task.add_done_callback(_log_task_exception)
            self._commitment_tasks.append(task)

    async def _persist_one_commitment(
        self,
        finding: Any,
        *,
        person_id: str | None,
        store: DurableCommitmentStore,
    ) -> None:
        """Persist a single commitment finding via the injected store.

        A commitment must be owed to a person; with no ``person_id`` there is
        nothing durable to key the row on, so it is skipped.

        ``source_communication_id`` is left ``None``: the pipeline only ever
        sees the in-memory domain :class:`Communication`, never the durable
        ``communications`` row, so the domain ``communication_id``
        (``<prefix>:<id>``) is not a valid FK target for the durable column.
        Provenance to the source communication is preserved on
        ``evidence["created_from"]`` instead — the domain id the finding was
        extracted from (AC-R04).
        """
        if person_id is None:
            return
        why = getattr(finding, "why", None) or []
        matched = getattr(finding, "matched", "") or ""
        evidence: dict[str, Any] = {
            "finding_id": finding.finding_id,
            "finding_kind": finding.kind,
            "created_from": finding.created_from,
            "content_ref": finding.content_ref,
            "confidence": finding.confidence,
            "model_used": finding.model_used,
            "why": list(why),
        }
        if matched:
            evidence["matched"] = matched
        await store.create_commitment(
            person_id=person_id,
            text=matched or (why[0] if why else "commitment"),
            due_at=finding.due,
            state="open",
            evidence=evidence,
        )

    async def wait_for_commitments(self) -> None:
        """Await all pending durable-commitment writes (flush the seam).

        ``ingest`` stays synchronous, so durable writes are scheduled in the
        background. Callers (and tests) that need the durable plane settled —
        e.g. before shutdown or before asserting on the rows — await this. A
        write that already ran to completion via ``asyncio.run`` in a
        synchronous context has no pending task.
        """
        if not self._commitment_tasks:
            return
        tasks, self._commitment_tasks = self._commitment_tasks, []
        await asyncio.gather(*tasks, return_exceptions=True)


def _log_task_exception(task: "asyncio.Task[None]") -> None:
    """Log (never raise) a background persistence failure.

    Durability is a best-effort extension of the deterministic pipeline; a
    failed write must not surface as an error in ``ingest()`` (which callers
    invoke synchronously). The exception is logged, not raised.
    """
    try:
        exc = task.exception()
    except BaseException as exc:  # cancelled
        _logger.warning(
            "durable commitment persistence cancelled: %s", exc, exc_info=exc
        )
        return
    if exc is not None:
        _logger.warning(
            "durable commitment persistence failed: %s", exc, exc_info=exc
        )


# Process-wide composition over the existing singletons (app seam, see the
# module docstring). The class itself stays fully injectable/testable.
_pipeline: CommunicationPipeline | None = None
#: Pending store for pipelines composed after :func:`set_commitment_store`
#: (startup wires the store before the first ``get_pipeline()`` call).
_pending_store: DurableCommitmentStore | None = None


def set_commitment_store(store: DurableCommitmentStore | None) -> None:
    """App seam: bind (or unbind) the durable commitment sink (extend-only).

    ``get_pipeline()`` composes the pipeline with no store, so by default it
    is pure — detected commitments are never written to the durable table.
    The app startup wiring calls this once with a
    :class:`~backend.persistence.repositories.commitments_repo.CommitmentsRepository`
    (obtained via the ``RepositoryRegistry``) whenever a durable engine exists,
    so the live app mirrors detected commitments to Postgres. The value is
    applied to the live pipeline when one exists *and* recorded as the default
    for pipelines composed later (the usual case: startup precedes the first
    ingest), so re-calling ``get_pipeline()`` after ``reset_pipeline()`` keeps
    the store. Tests that build the pipeline directly stay pure unless they
    call this setter. The setter itself never rebuilds the composed pipeline.
    """
    global _pipeline, _pending_store
    _pending_store = store
    if _pipeline is not None:
        _pipeline._commitment_store = store


def get_pipeline() -> CommunicationPipeline:
    """Bind the three existing process-wide singletons into one pipeline."""
    global _pipeline
    if _pipeline is None:
        _pipeline = CommunicationPipeline(
            detection=get_detection_service(),
            followups=get_followup_service(),
            attention=get_attention_service(),
            commitment_store=_pending_store,
        )
    return _pipeline


def reset_pipeline() -> None:
    """Test hook — forget the composed pipeline and any bound store
    (singletons untouched; restores the pure default)."""
    global _pipeline, _pending_store
    _pipeline = None
    _pending_store = None


__all__ = [
    "CommunicationPipeline",
    "DurableCommitmentStore",
    "FollowUpLink",
    "PipelineResult",
    "get_pipeline",
    "reset_pipeline",
    "set_commitment_store",
]
