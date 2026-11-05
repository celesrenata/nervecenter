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
import hashlib
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
        id: str | None = None,
        person_id: str,
        text: str,
        due_at: datetime | None = None,
        state: str = "open",
        evidence: dict[str, Any] | None = None,
        source_communication_id: str | None = None,
    ) -> Any:
        """Persist one commitment row; returns the stored row.

        An explicit deterministic ``id`` dedups (get-or-create) — the
        pipeline derives it from the finding's provenance key so a replay
        across a restart reuses the row.
        """
        ...


class DurableAttentionStore(Protocol):
    """Durable personal-plane sink for detected attention findings (S4).

    The :class:`CommunicationPipeline` is pure by default. When a durable
    attention store is injected, each detected finding that is NOT already
    covered by the follow-up → attention durable path (i.e. every finding
    except an OUTGOING commitment/request with a due time, which the follow-up
    service already mirrors durably once promoted) is written into the durable
    ``attention_items`` table so the record reaches ``GET /api/today`` (which
    reads the durable table only). In practice this is chiefly the INCOMING
    ``request`` findings incoming mail produces — the ones that stayed
    in-memory-only and left Today empty. The pipeline talks only to this
    protocol, so the concrete
    :class:`~backend.persistence.repositories.attention_repo.AttentionRepository`
    (or any test double) can satisfy it without the pipeline importing the
    persistence layer.
    """

    async def create_attention_item(
        self,
        *,
        id: str | None = None,
        title: str,
        importance: int = 5,
        urgency: str = "normal",
        body: str | None = None,
        deadline_at: datetime | None = None,
        source_refs: list[Any] | None = None,
        recommended_action: str | None = None,
        escalation_policy: dict[str, Any] | None = None,
        state: str = "active",
        resolution_condition: str | None = None,
    ) -> Any:
        """Persist one attention item row; returns the stored row.

        An explicit deterministic ``id`` dedups (get-or-create) — the
        pipeline derives it from the finding's deterministic id so a replay
        across a restart reuses the row instead of stacking a duplicate.
        """
        ...


#: Durable urgency bands (frozen by spec §6) keyed from the finding's
#: deadline-proximity urgency float produced by the detection service.
_URGENCY_FLOAT_TO_BAND: tuple[tuple[float, str], ...] = (
    (0.9, "critical"),
    (0.75, "high"),
    (0.6, "normal"),
    (0.0, "low"),
)


def _urgency_band(urgency_float: float) -> str:
    """Map a detection urgency float (0..1) onto a durable urgency band."""
    for threshold, band in _URGENCY_FLOAT_TO_BAND:
        if urgency_float >= threshold:
            return band
    return "normal"


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
    detected commitments into the durable personal plane. With no store
    given the pipeline falls back to the follow-up service's
    ``RepositoryRegistry`` (when one was injected there) — the *inject
    nothing* composition stays pure (zero I/O) and behaves exactly as
    before. A pinned ``now`` makes the whole pass reproducible.
    """

    def __init__(
        self,
        *,
        detection: DetectionService,
        followups: FollowUpService,
        attention: AttentionService,
        commitment_store: DurableCommitmentStore | None = None,
        attention_store: DurableAttentionStore | None = None,
    ) -> None:
        self._detection = detection
        self._followups = followups
        self._attention = attention
        self._commitment_store = commitment_store
        self._attention_store = attention_store
        #: Commitment finding keys (``<kind>:<finding_id>``) already mirrored
        #: to the durable store, so re-ingesting the same communication in
        #: process never schedules a duplicate write (mirrors the follow-up
        #: idempotency convention).
        self._persisted_commitments: set[str] = set()
        #: Detected-finding ids already mirrored to the durable attention
        #: table in this process, so re-ingesting the same communication never
        #: schedules a duplicate durable attention write (the durable id is
        #: deterministic, so a second write is a get-or-create no-op anyway —
        #: this just skips the round trip).
        self._persisted_attention: set[str] = set()
        #: Background tasks persisting commitments; flushed by
        #: :meth:`wait_for_commitments`.
        self._commitment_tasks: list[asyncio.Task[None]] = []
        #: Background tasks persisting durable attention items; drained by
        #: :meth:`flush_durable`.
        self._attention_tasks: list[asyncio.Task[None]] = []

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
        """The explicitly injected durable commitment sink, or ``None``.

        Resolution of the sink that actually writes is
        :attr:`_commitment_sink` (the follow-up registry is the fallback).
        """
        return self._commitment_store

    @property
    def attention_store(self) -> DurableAttentionStore | None:
        """The explicitly injected durable attention sink, or ``None``.

        Resolution of the sink that actually writes is
        :attr:`_attention_sink` (the follow-up registry is the fallback).
        """
        return self._attention_store

    @property
    def _attention_sink(self) -> DurableAttentionStore | None:
        """The resolved durable attention sink — explicit store, else the
        follow-up service's registry's ``attention`` repository (extend-only
        durability fallback, mirroring :attr:`_commitment_sink`).

        A caller may bind the *registry* itself (not the attention
        repository); a registry is unwrapped to its ``attention`` repository.
        ``None`` (no explicit store, no registry) means zero I/O.
        """
        store = self._attention_store
        if store is None:
            store = getattr(self._followups, "registry", None)
        if store is None:
            return None
        repo = getattr(store, "attention", None)
        if repo is not None and hasattr(repo, "create_attention_item"):
            return repo
        if hasattr(store, "create_attention_item"):
            return store
        return None

    @property
    def _commitment_sink(self) -> DurableCommitmentStore | None:
        """The resolved durable sink — explicit store, else the follow-up
        service's registry (extend-only durability fallback).

        A caller may bind the *registry* itself (not the commitments
        repository), and a pipeline composed with no explicit store still
        mirrors through the registry its follow-up service was given — so
        the sink is *resolved*, never assumed: a registry is unwrapped to
        its ``commitments`` repository. ``None`` (no explicit store, no
        registry) means zero I/O.
        """
        store = self._commitment_store
        if store is None:
            store = getattr(self._followups, "registry", None)
        if store is None:
            return None
        repo = getattr(store, "commitments", None)
        if repo is not None and hasattr(repo, "create_commitment"):
            return repo
        return store

    def _commitment_write_lock(self) -> asyncio.Lock | None:
        """The lock serializing this pipeline's commitment writes.

        Shared with the follow-up service's mirrors through the registry
        (one durable plane = one lock). ``None`` → no lock (a bare store
        without a registry; a single pipeline is then the only writer).
        """
        registry = getattr(self._followups, "registry", None)
        if registry is not None:
            lock = getattr(registry, "durable_lock", None)
            if lock is not None:
                return lock
        if self._commitment_store is not None:
            return getattr(self._commitment_store, "durable_lock", None)
        return None

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

        # S4 durable attention seam: mirror each detected finding into the
        # durable ``attention_items`` table (no-op when no store is resolved)
        # so Today — which reads the durable table only — surfaces them.
        # Follow-up-eligible findings (OUTGOING commitment/request with a due)
        # are EXCLUDED here: they already get a durable attention item through
        # the follow-up service's own attention mirror once promoted, so
        # mirroring them again would stack a duplicate row. The seam therefore
        # covers exactly the findings that otherwise never reach the durable
        # table — chiefly the INCOMING ``request`` findings Today needs.
        self._persist_attention(
            [f for f in report.findings if not self._follows_up(f, communication)],
            communication,
            now=clock,
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

        No-op unless a durable sink is resolved (explicit store, else the
        follow-up service's registry — see :attr:`_commitment_sink`). The
        pipeline is synchronous and must stay so (its callers invoke
        ``ingest`` without ``await``), so each write is scheduled as a
        background task rather than awaited. Writes are idempotent by the
        ``<kind>:<finding_id>`` key so a replay in the same process never
        schedules a duplicate row.
        """
        store = self._commitment_sink
        if store is None:
            return
        lock = self._commitment_write_lock()

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
                lock=lock,
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
        lock: asyncio.Lock | None = None,
    ) -> None:
        """Persist a single commitment finding via the resolved store.

        A commitment must be owed to a person; with no ``person_id`` there is
        nothing durable to key the row on, so it is skipped.

        ``source_communication_id`` is left ``None``: the pipeline only ever
        sees the in-memory domain :class:`Communication`, never the durable
        ``communications`` row, so the domain ``communication_id``
        (``<prefix>:<id>``) is not a valid FK target for the durable column.
        Provenance to the source communication is preserved on
        ``evidence["created_from"]`` instead — the domain id the finding was
        extracted from (AC-R04).

        The durable row carries *templated, provenance-only* strings (P0-I):
        the detector's quoted fragments (``finding.matched`` / the quoted
        part of ``finding.why``) stay on the live in-memory finding and
        never reach the durable plane. The row id is derived from the
        finding's deterministic provenance key, so a replay across a
        restart reuses the row (get-or-create) instead of stacking a
        duplicate. When a lock is given (the registry's durable lock) the
        write is serialized with the follow-up / attention mirrors.
        """
        if person_id is None:
            return

        async def _write() -> None:
            key = f"{finding.kind}:{finding.finding_id}"
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
            due_iso = finding.due.isoformat() if finding.due is not None else None
            text = f"Commitment — {finding.finding_id}"
            if due_iso is not None:
                text += f" (due {due_iso})"
            templated_why: list[str] = [
                f"{finding.kind}: detected by {finding.model_used}",
                f"finding {finding.finding_id}",
            ]
            if due_iso is not None:
                templated_why.append(f"due {due_iso}")
            await store.create_commitment(
                id=f"ct-{digest}",
                person_id=person_id,
                text=text,
                due_at=finding.due,
                state="open",
                evidence={
                    "finding_id": finding.finding_id,
                    "finding_kind": finding.kind,
                    "created_from": finding.created_from,
                    "content_ref": finding.content_ref,
                    "confidence": finding.confidence,
                    "model_used": finding.model_used,
                    "why": templated_why,
                },
            )

        if lock is None:
            await _write()
        else:
            async with lock:
                await _write()

    # -- durable attention seam (S4) ----------------------------------------
    @staticmethod
    def _attention_id_for(finding: Any) -> str:
        """Deterministic durable attention id derived from the finding.

        The finding id is itself deterministic (``<kind>-<communication_id>``,
        R103), so hashing ``<kind>:<finding_id>`` and taking the row id
        ``at-<digest>`` makes the durable attention write idempotent across
        restarts — re-ingesting the same communication reuses the same row
        (get-or-create by id) instead of stacking a duplicate.
        """
        key = f"{finding.kind}:{finding.finding_id}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
        return f"at-{digest}"

    @staticmethod
    def _attention_columns(
        finding: Any, communication: Communication, *, now: datetime
    ) -> dict[str, Any]:
        """Map a finding onto durable ``attention_items`` columns.

        Carries metadata/suggested-action/entity refs only — never the raw
        body text or tokens (§7). The title is *fully templated* from the
        finding's kind, sender, and a due timestamp (never the detector's
        quoted body fragments ``finding.matched`` / ``finding.due_info.expr``,
        which stay on the live in-memory finding only — the same P0-I policy
        :meth:`_persist_one_commitment` applies to the durable commitment
        text). The in-memory :meth:`DetectionService._attention_payload` may
        quote the fragment in its ephemeral ``reason``; the durable plane must
        not, so the field choices are mirrored in *shape* (what each kind
        means) but the durable title carries no body substring.
        """
        kind = finding.kind
        sender = communication.sender or "unknown"
        due_iso = finding.due.isoformat() if finding.due is not None else None
        if kind == "commitment":
            title = "Follow through on your commitment"
            importance = 7
            recommended = "Complete the commitment"
        elif kind == "request":
            title = f"Respond to {sender}"
            importance = 8
            recommended = "Reply to the request"
        else:  # deadline
            title = f"Deadline due {due_iso}" if due_iso else "Upcoming deadline"
            importance = 8
            recommended = "Meet the deadline"

        # Static urgency band from the finding's due proximity at detection
        # time (mirrors DetectionService._attention_payload). The escalation
        # plane re-derives live proximity from ``deadline_at`` afterwards.
        urgency_float = 0.5
        if finding.due is not None:
            hours = (finding.due - now).total_seconds() / 3600.0
            if hours <= 0:
                urgency_float = 0.95
            elif hours <= 24:
                urgency_float = 0.85
            elif hours <= 72:
                urgency_float = 0.65
            else:
                urgency_float = 0.55

        body = f"Detected {kind} from {communication.channel.value} ({finding.model_used})."

        return {
            "title": title,
            "importance": importance,
            "urgency": _urgency_band(urgency_float),
            "body": body,
            "deadline_at": finding.due,
            "source_refs": [
                f"communication:{communication.communication_id}",
                f"finding:{finding.finding_id}",
            ],
            "recommended_action": recommended,
            "state": "active",
        }

    def _persist_attention(
        self, findings: Sequence[Any], communication: Communication, *, now: datetime
    ) -> None:
        """Mirror detected findings into the durable attention table.

        No-op unless a durable sink is resolved (explicit store, else the
        follow-up service's registry — see :attr:`_attention_sink`). Best
        effort: the pipeline is synchronous and must stay so, so each write is
        scheduled as a background task rather than awaited, and a failure is
        logged (never raised) so a durable attention write failure degrades
        honestly without crashing ingest. Writes are idempotent by the
        deterministic durable id so a replay in the same process never
        schedules a duplicate row.
        """
        store = self._attention_sink
        if store is None:
            return
        lock = self._commitment_write_lock()

        for finding in findings:
            key = f"{getattr(finding, 'kind', None)}:{getattr(finding, 'finding_id', None)}"
            if key in self._persisted_attention:
                continue
            self._persisted_attention.add(key)
            coro = self._persist_one_attention(
                finding,
                communication=communication,
                store=store,
                lock=lock,
                now=now,
            )
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(coro)
                continue
            task = loop.create_task(coro)
            task.add_done_callback(_log_attention_task_exception)
            self._attention_tasks.append(task)

    async def _persist_one_attention(
        self,
        finding: Any,
        *,
        communication: Communication,
        store: DurableAttentionStore,
        lock: asyncio.Lock | None = None,
        now: datetime,
    ) -> None:
        """Persist a single finding as a durable attention item.

        The row id is derived from the finding's deterministic id, so a replay
        across a restart reuses the row (get-or-create). When a lock is given
        (the registry's durable lock) the write is serialized with the
        follow-up / commitment mirrors so their ``session.commit()`` calls on a
        single-connection engine never interleave.
        """
        columns = self._attention_columns(finding, communication, now=now)

        async def _write() -> None:
            await store.create_attention_item(
                id=self._attention_id_for(finding),
                **columns,
            )

        if lock is None:
            await _write()
        else:
            async with lock:
                await _write()

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

    async def flush_durable(self) -> Exception | None:
        """Settle the pending durable mirror writes; return the first
        failure (``None`` when everything committed — or nothing was
        pending).

        The durable mirror is best-effort and never raises from the
        synchronous ``ingest``; this is the explicit outcome check. The mail
        worker gates its cursor on it (P0-D commit-gate); tests use it (via
        the seam) to settle the plane before asserting. The commitment
        writes are drained first, then the follow-up mirrors (deterministic
        ordering of the reported failure).
        """
        first: Exception | None = None
        if self._commitment_tasks:
            tasks, self._commitment_tasks = self._commitment_tasks, []
            for result in await asyncio.gather(*tasks, return_exceptions=True):
                if isinstance(result, Exception) and first is None:
                    first = result
        # S4 durable attention is BEST-EFFORT (not gated): the attention
        # tasks are drained so none leak, but their failures are swallowed
        # here (already logged by the done-callback) and never returned —
        # a durable attention write failure must degrade honestly and never
        # block the worker's cursor advance the way a lost commitment/person
        # write does.
        if self._attention_tasks:
            tasks, self._attention_tasks = self._attention_tasks, []
            await asyncio.gather(*tasks, return_exceptions=True)
        drain = getattr(self._followups, "drain_durable", None)
        if drain is not None:
            follow_first = await drain()
            if first is None and follow_first is not None:
                first = follow_first
        return first


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


def _log_attention_task_exception(task: "asyncio.Task[None]") -> None:
    """Log (never raise) a background durable-attention persistence failure.

    The durable attention mirror is best-effort (S4): a failed write must not
    surface as an error in ``ingest()`` (callers invoke it synchronously) nor
    block the worker's cursor advance. The exception is logged, not raised.
    """
    try:
        exc = task.exception()
    except BaseException as exc:  # cancelled
        _logger.warning(
            "durable attention persistence cancelled: %s", exc, exc_info=exc
        )
        return
    if exc is not None:
        _logger.warning(
            "durable attention persistence failed: %s", exc, exc_info=exc
        )


# Process-wide composition over the existing singletons (app seam, see the
# module docstring). The class itself stays fully injectable/testable.
_pipeline: CommunicationPipeline | None = None
#: Pending store for pipelines composed after :func:`set_commitment_store`
#: (startup wires the store before the first ``get_pipeline()`` call).
_pending_store: DurableCommitmentStore | None = None
#: Pending durable attention store for pipelines composed after
#: :func:`set_attention_store` (same startup-before-first-ingest contract).
_pending_attention_store: DurableAttentionStore | None = None


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


def set_attention_store(store: DurableAttentionStore | None) -> None:
    """App seam: bind (or unbind) the durable attention sink (extend-only, S4).

    ``get_pipeline()`` composes the pipeline with no store, so by default it
    is pure — detected findings are never written to the durable attention
    table. The app startup wiring calls this once with the
    :class:`~backend.persistence.repositories.attention_repo.AttentionRepository`
    (obtained via the ``RepositoryRegistry``) whenever a durable engine
    exists, so the live app mirrors detected findings to Postgres and Today
    surfaces them. The value is applied to the live pipeline when one exists
    *and* recorded as the default for pipelines composed later (startup
    precedes the first ingest), mirroring :func:`set_commitment_store`.
    """
    global _pipeline, _pending_attention_store
    _pending_attention_store = store
    if _pipeline is not None:
        _pipeline._attention_store = store


def get_pipeline() -> CommunicationPipeline:
    """Bind the three existing process-wide singletons into one pipeline."""
    global _pipeline
    if _pipeline is None:
        _pipeline = CommunicationPipeline(
            detection=get_detection_service(),
            followups=get_followup_service(),
            attention=get_attention_service(),
            commitment_store=_pending_store,
            attention_store=_pending_attention_store,
        )
    return _pipeline


def reset_pipeline() -> None:
    """Test hook — forget the composed pipeline and any bound store
    (singletons untouched; restores the pure default)."""
    global _pipeline, _pending_store, _pending_attention_store
    _pipeline = None
    _pending_store = None
    _pending_attention_store = None


__all__ = [
    "CommunicationPipeline",
    "DurableAttentionStore",
    "DurableCommitmentStore",
    "FollowUpLink",
    "PipelineResult",
    "get_pipeline",
    "reset_pipeline",
    "set_attention_store",
    "set_commitment_store",
]
