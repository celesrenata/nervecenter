"""Detection service — the stateful orchestrator over the pure extractors.

R103 (AC-R04/R06) integration layer:

1. **Text access without plaintext leakage.** The normalized
   :class:`~backend.communications.models.Communication` record carries only
   a ``content_ref`` pointer. The service resolves transient text through an
   in-memory content registry (mirroring how the source planes — messages /
   email / voicemail — keep bodies in process memory, outside the knowledge
   graph). Findings and events never embed the body; only the pointer,
   matched fragments, and ``why`` strings.
2. **InferenceAssist (off by default, never the sole detector).** When an
   assist is attached and enabled it may *refine* deterministic findings
   (confidence / model tag / due / status + an appended ``why`` entry). It
   can never create a finding on its own, and with the default
   :class:`DisabledAssist` the output is byte-identical for identical input
   (AC-R04: deterministic path fully green with assist off;
   ``model_used`` stays deterministic).
3. **§8 events.** Each finding emits its §8 event on detection
   (``commitment.detected`` / ``request.detected`` / ``deadline.detected``);
   the overdue sweep emits the overdue variants (``commitment.overdue``,
   ``deadline.passed``) and re-detection with a changed due emits
   ``deadline.updated``; explicit resolution emits ``commitment.completed``
   / ``request.resolved``. Events are built as
   :class:`schemas.events.Event` (the bus wire model) with a deterministic
   ``idempotency_key`` (``<event_type>:<finding_id>``) so replays dedupe.
   ``category`` is ``knowledge`` — the closest frozen category for personal
   reasoning findings (the §10 category set has no "personal" entry).
4. **Attention seam (AC-R06, no replacement).** Findings are fed into the
   *existing* :class:`~backend.attention.service.AttentionService` through
   its public ``create`` / ``refresh`` seam — the house pattern used by
   waiting-on (one live :class:`AttentionItem` per finding, refreshed while
   live). The detection's ``due`` drives the existing explainable
   ``_score`` (overdue +0.25, ≤12h +0.20, ≤48h +0.12) and the finding's
   ``why[]`` is passed through verbatim into the item's ``why``. No changes
   to the attention service; no scoring replacement.
5. **In-memory only.** No persistence (house style of the personal planes).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from schemas.events import Event

from backend.attention.service import AttentionService, get_attention_service
from backend.communications.models import Communication

from .assist import (
    DisabledAssist,
    InferenceAssist,
    apply_refinement,
    assist_context_for,
)
from .extractors import detect_communication
from .findings import (
    DETERMINISTIC_DETECTOR,
    Commitment,
    Deadline,
    DetectionReport,
    Finding,
    FindingStatus,
    Request,
)
from .noise import is_marketing

_EVENT_CATEGORY = "knowledge"  # frozen §10 set has no "personal" bucket
_SOURCE = "communications.detection"

#: event_type per finding kind (prompt2 §8).
_DETECTED_EVENT = {
    "commitment": "commitment.detected",
    "request": "request.detected",
    "deadline": "deadline.detected",
}
_OVERDUE_EVENT = {
    "commitment": "commitment.overdue",
    "deadline": "deadline.passed",
}
_RESOLVED_EVENT = {
    "commitment": "commitment.completed",
    "request": "request.resolved",
}


class ContentResolver(Protocol):
    """Transient access to the raw payload behind a ``content_ref``.

    The normalized record never stores the payload (§7); the resolver reads
    it from the source plane (in-process) at detection time only.
    """

    def resolve(self, content_ref: str) -> str | None: ...


class _InMemoryResolver:
    """Default resolver: an in-memory content registry (test/dev seam)."""

    def __init__(self) -> None:
        self._content: dict[str, str] = {}

    def register(self, content_ref: str, text: str) -> None:
        self._content[str(content_ref)] = str(text or "")

    def resolve(self, content_ref: str) -> str | None:
        return self._content.get(str(content_ref))

    def clear(self) -> None:
        self._content.clear()


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _finding_payload(finding: Finding) -> dict[str, Any]:
    """The §8 event payload for a finding — metadata only, no plaintext."""
    return {
        "finding_id": finding.finding_id,
        "kind": finding.kind,
        "created_from": finding.created_from,
        "content_ref": finding.content_ref,
        "status": finding.status.value,
        "confidence": finding.confidence,
        "model_used": finding.model_used,
        "due": _iso(finding.due),
        "due_info": (
            {
                "expr": finding.due_info.expr,
                "time_stated": finding.due_info.time_stated,
                "confidence": finding.due_info.confidence,
            }
            if finding.due_info
            else None
        ),
        "why": list(finding.why),
        "matched": finding.matched,
    }


def _make_event(event_type: str, subject: str, payload: dict[str, Any],
                *, occurred_at: datetime, severity: str = "info") -> Event:
    """Build a bus :class:`schemas.events.Event` with a stable idempotency key.

    ``created_at`` is pinned to ``occurred_at`` so that — with assist off — the
    same detection input produces byte-identical events (AC-R04); only the
    bus ``id`` (uuid4) and integrity ``hash`` may differ per emission, which
    the deterministic-output tests exclude.
    """
    return Event(
        id=str(uuid4()),
        event_type=event_type,
        category=_EVENT_CATEGORY,
        source=_SOURCE,
        subject=subject,
        payload=payload,
        severity=severity,  # type: ignore[arg-type]
        occurred_at=occurred_at,
        created_at=occurred_at,
        idempotency_key=f"{event_type}:{subject}",
    )


class DetectionService:
    """In-memory detection orchestrator over the communications plane."""

    def __init__(
        self,
        *,
        attention: AttentionService | None = None,
        assist: InferenceAssist | None = None,
    ) -> None:
        self._attention = attention or get_attention_service()
        self._assist: InferenceAssist | None = assist
        self._resolver = _InMemoryResolver()
        self._reports: dict[str, DetectionReport] = {}
        self._attention_ids: dict[str, str] = {}
        # Communication objects for findings this service detected (used by
        # the overdue sweep to refresh attention items even when the
        # communication was not ingested through the registry).
        self._communications: dict[str, Communication] = {}

    # -- content seam -------------------------------------------------------
    def register_content(self, content_ref: str, text: str) -> None:
        """Register transient text for a ``content_ref`` (never persisted)."""
        self._resolver.register(content_ref, text)

    def _text_for(self, communication: Communication) -> str:
        raw = self._resolver.resolve(communication.content_ref)
        return raw if raw is not None else ""

    # -- detection ----------------------------------------------------------
    def detect(
        self,
        communication: Communication,
        *,
        now: datetime | None = None,
    ) -> DetectionReport:
        """Detect findings for one normalized communication.

        Deterministic with assist off: identical ``communication`` + content
        + ``now`` → identical report. Marketing/newsletter/social input is
        suppressed (zero findings) with the reason recorded.
        """
        now = _now_utc() if now is None else now
        comm_id = communication.communication_id
        self._communications[comm_id] = communication

        gate = is_marketing(communication)
        if gate is not None:
            report = DetectionReport(
                communication_id=comm_id,
                content_ref=communication.content_ref,
                findings=(),
                suppressed=(gate.reason,),
                detected_at=now,
            )
            self._reports[comm_id] = report
            return report

        report = detect_communication(communication, self._text_for(communication), now=now)

        # Assist refinement: refine-only, off by default (AC-R04).
        report = self._apply_assist(report, now=now)

        # §8 detected events + attention seam.
        events = []
        model_used = DETERMINISTIC_DETECTOR
        for finding in report.findings:
            events.append(
                _make_event(
                    _DETECTED_EVENT[finding.kind],
                    finding.finding_id,
                    _finding_payload(finding),
                    occurred_at=now,
                )
            )
            if finding.model_used != DETERMINISTIC_DETECTOR:
                model_used = finding.model_used
            self._feed_attention(finding, communication, now=now)

        report = report.model_copy(
            update={
                "events": tuple(events),
                # only this communication's findings → attention items
                "attention_ids": {
                    f.finding_id: aid
                    for f in report.findings
                    if (aid := self._attention_ids.get(f.finding_id)) is not None
                },
                "model_used": model_used,
            }
        )
        self._reports[comm_id] = report
        return report

    def _apply_assist(self, report: DetectionReport, *, now: datetime) -> DetectionReport:
        assist = self._assist
        if assist is None or getattr(assist, "enabled", True) is False:
            return report
        refined: list[Commitment | Request | Deadline] = []
        for finding in report.findings:
            ref = assist.refine(finding, assist_context_for(finding, now=now))
            refined.append(apply_refinement(finding, ref) if ref else finding)
        return report.model_copy(update={"findings": tuple(refined)})

    # -- attention seam (AC-R06) ---------------------------------------------
    def _feed_attention(
        self,
        finding: Finding,
        communication: Communication,
        *,
        now: datetime,
    ) -> None:
        """Create / refresh the live AttentionItem for a finding.

        Uses the existing public seam only; the item's ``why`` carries the
        detection's ``why[]`` and its ``due`` drives the existing
        explainable ``_score`` deadline-proximity factors.
        """
        payload = self._attention_payload(finding, communication, now=now)
        aid = self._attention_ids.get(finding.finding_id)
        existing = self._attention.get(aid) if aid else None
        live = existing is not None and existing.status in (
            "NOW", "TODAY", "SOON", "WAITING_ON",
        )
        if aid is not None and live:
            item = self._attention.refresh(aid, payload)
        else:
            item = self._attention.create(payload)
            self._attention_ids[finding.finding_id] = item.attention_id

    def _attention_payload(
        self, finding: Finding, communication: Communication, *, now: datetime
    ) -> dict[str, Any]:
        kind = finding.kind
        sender = communication.sender or "unknown"
        if kind == "commitment":
            reason = f"Follow through: {finding.matched or 'own commitment'}"
            importance = 0.7
            suggested = "Complete the commitment"
        elif kind == "request":
            reason = f"Respond to {sender}: {finding.matched or 'incoming request'}"
            importance = 0.75
            suggested = "Reply to the request"
        else:  # deadline
            reason = f"Deadline: {finding.due_info.expr if finding.due_info else 'due date'}"
            importance = 0.8
            suggested = "Meet the deadline"

        urgency = 0.5
        if finding.due is not None:
            hours = (finding.due - now).total_seconds() / 3600.0
            if hours <= 0:
                urgency = 0.95
            elif hours <= 24:
                urgency = 0.85
            elif hours <= 72:
                urgency = 0.65
            else:
                urgency = 0.55

        return {
            "reason": reason,
            "urgency": urgency,
            "importance": importance,
            "due": finding.due,
            "entities": [sender],
            "sources": [communication.channel.value],
            "suggested_action": suggested,
            "why": [f"detected {kind} ({finding.model_used}): " + w for w in finding.why],
        }

    # -- updates / overdue ----------------------------------------------------
    def sweep_overdue(self, *, now: datetime | None = None) -> tuple[Event, ...]:
        """Flip OPEN findings past their due; emit the §8 overdue events.

        Commitments → ``OVERDUE`` + ``commitment.overdue``; deadlines →
        ``PASSING`` + ``deadline.passed``. Requests have no due in §17 and
        are not swept (follow-up tracking is R104). Returns an empty tuple
        when nothing is past its due.
        """
        now = _now_utc() if now is None else now
        events: list[Event] = []
        reports: dict[str, DetectionReport] = {}
        for comm_id, report in self._reports.items():
            new_findings: list[Commitment | Request | Deadline] = []
            changed = False
            for finding in report.findings:
                f = finding
                if (
                    f.status is FindingStatus.OPEN
                    and f.due is not None
                    and f.due < now
                    and f.kind in _OVERDUE_EVENT
                ):
                    status = (
                        FindingStatus.OVERDUE
                        if f.kind == "commitment"
                        else FindingStatus.PASSING
                    )
                    f = f.model_copy(update={"status": status})
                    changed = True
                    event = _make_event(
                        _OVERDUE_EVENT[f.kind],
                        f.finding_id,
                        _finding_payload(f),
                        occurred_at=now,
                        severity="warning",
                    )
                    events.append(event)
                    self._feed_attention(f, self._communication(comm_id), now=now)
                new_findings.append(f)
            if changed:
                reports[comm_id] = report.model_copy(update={"findings": tuple(new_findings)})
        self._reports.update(reports)
        return tuple(events)

    def _communication(self, comm_id: str) -> Communication | None:
        if comm_id in self._communications:
            return self._communications[comm_id]
        from backend.communications.registry import get_communications_service

        return get_communications_service().get_communication(comm_id)

    def resolve_finding(self, finding_id: str, *, now: datetime | None = None) -> Event | None:
        """Resolve a commitment/request → §8 update event + attention resolve."""
        now = _now_utc() if now is None else now
        report = next((r for r in self._reports.values()
                       if r.by_id(finding_id) is not None), None)
        if report is None:
            return None
        finding = report.by_id(finding_id)
        assert finding is not None
        if finding.kind not in _RESOLVED_EVENT:
            return None
        status = FindingStatus.PASSING
        resolved = finding.model_copy(update={"status": status})
        event = _make_event(
            _RESOLVED_EVENT[finding.kind],
            finding_id,
            _finding_payload(resolved),
            occurred_at=now,
        )
        new = [
            resolved if f.finding_id == finding_id else f for f in report.findings
        ]
        self._reports[report.communication_id] = report.model_copy(
            update={"findings": tuple(new)}
        )
        aid = self._attention_ids.get(finding_id)
        if aid is not None:
            self._attention.resolve(aid)
        return event

    # -- queries --------------------------------------------------------------
    def report_for(self, communication_id: str) -> DetectionReport | None:
        return self._reports.get(communication_id)

    def all_findings(
        self, *, kind: str | None = None, status: str | None = None
    ) -> list[Commitment | Request | Deadline]:
        out: list[Commitment | Request | Deadline] = []
        for report in self._reports.values():
            for f in report.findings:
                if kind is not None and f.kind != kind:
                    continue
                if status is not None and f.status.value != status:
                    continue
                out.append(f)
        return out

    def reset(self) -> None:
        """Test hook — clear all in-memory state (house convention)."""
        self._reports.clear()
        self._attention_ids.clear()
        self._communications.clear()
        self._resolver.clear()


_service: DetectionService | None = None


def get_detection_service() -> DetectionService:
    global _service
    if _service is None:
        _service = DetectionService()
    return _service


def reset_detection_service() -> None:
    global _service
    _service = None


__all__ = [
    "ContentResolver",
    "DetectionService",
    "get_detection_service",
    "reset_detection_service",
]
