"""R106 — end-to-end detection → follow-up → attention wiring tests.

AC-R06 / AC-R07 / AC-R15 over
:mod:`backend.communications.detection.wiring`:

- **§61 chain:** ingesting an outgoing "I'll send the contract Friday"
  at the pinned clock produces a commitment finding with a non-empty
  ``why[]`` and a Friday due, plus a follow-up (target = the finding,
  provenance key ``commitment:<finding_id>``). A later ingest (same
  communication, later clock) reuses the follow-up and the due-evaluation
  sweep promotes it to exactly one live AttentionItem sourced
  ``followups`` whose explanation carries the finding's provenance and
  whose audit trail is populated (AC-R15). Every id is traceable:
  finding → follow-up → attention.
- **Idempotent replay:** re-ingesting the same communication never
  duplicates findings, follow-ups, or live attention items — the
  deterministic ``<kind>:<finding_id>`` key is reused.
- **Precision:** a marketing sender yields zero findings, zero
  follow-ups, zero follow-up-driven attention.
- **Terminal states:** a FORGOTTEN (or COMPLETED) follow-up produces no
  live attention, its item is resolved, and the follow-up is immutable.
- **Pinned-clock determinism:** two independent runs at the same pinned
  clocks produce identical output (modulo uuid ids and the attention
  scorer's real-clock deadline-proximity factor, which is documented P0
  behavior — the scorer reads its own clock, not the pipeline's).

The class tests need no network — all services are injected.
``backend.api.app`` is imported first *only* as the
circular-import-safe entry point for this package layout (same
convention as :mod:`tests.backend.test_detection`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

# Circular-import-safe entry (see test_detection.py).
from backend.api.app import create_app  # noqa: F401
from backend.api.models import AttentionState
from backend.attention.service import (
    AttentionService,
    get_attention_service,
    reset_attention_service,
)
from backend.communications.detection.findings import Commitment
from backend.communications.detection.noise import clear_subjects
from backend.communications.detection.service import (
    DetectionService,
    get_detection_service,
    reset_detection_service,
)
from backend.communications.detection.wiring import (
    CommunicationPipeline,
    PipelineResult,
    get_pipeline,
    reset_pipeline,
)
from backend.communications.models import (
    Communication,
    CommunicationChannel,
    CommunicationDirection,
)
from backend.communications.registry import reset_communications_service
from backend.followups.models import FollowUpStatus, FollowUpTransitionError
from backend.followups.service import (
    FollowUpService,
    get_followup_service,
    reset_followup_service,
)

OUT = CommunicationDirection.OUTGOING
IN = CommunicationDirection.INCOMING

# Fixed anchor: Tuesday 2026-10-20 10:00 UTC (weekday math is deterministic).
NOW = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
#: "Friday" parsed from NOW → end-of-business-day Friday 2026-10-23 17:00 UTC.
FRIDAY_DUE = datetime(2026, 10, 23, 17, 0, tzinfo=UTC)
#: A whole-day-overdue clock after the due time (3 days → urgency capped
#: formula 0.6 + 0.05*3 = 0.75), so the due-sweep math is exact.
AFTER_DUE = FRIDAY_DUE + timedelta(days=3)  # 2026-10-26 17:00 UTC

TEXT = "I'll send the contract Friday"


def make_communication(
    comm_id: str,
    direction: CommunicationDirection,
    *,
    sender: str = "me",
    channel: CommunicationChannel = CommunicationChannel.SMS,
    person_id: str | None = None,
) -> Communication:
    return Communication(
        communication_id=comm_id,
        channel=channel,
        direction=direction,
        sender=sender,
        person_id=person_id,
        timestamp=NOW,
        content_ref=f"cr:{comm_id}",
    )


def ingest_text(
    pipeline: CommunicationPipeline,
    comm: Communication,
    text: str,
    *,
    now: datetime = NOW,
) -> PipelineResult:
    pipeline.detection.register_content(comm.content_ref, text)
    return pipeline.ingest(comm, now=now)


def run_61(pipeline: CommunicationPipeline) -> tuple[PipelineResult, PipelineResult]:
    """The two-phase §61 pass: ingest at NOW (creates the follow-up),
    then the same communication again at AFTER_DUE (reuses it and the
    due-sweep promotes it)."""
    comm = make_communication("message:8821", OUT, person_id="person:sam")
    first = ingest_text(pipeline, comm, TEXT, now=NOW)
    second = pipeline.ingest(comm, now=AFTER_DUE)
    return first, second


@pytest.fixture(autouse=True)
def _clean_state():
    clear_subjects()
    reset_communications_service()
    reset_pipeline()
    yield
    clear_subjects()
    reset_communications_service()
    reset_pipeline()


@pytest.fixture()
def attention() -> AttentionService:
    return AttentionService()


@pytest.fixture()
def followups(attention: AttentionService) -> FollowUpService:
    return FollowUpService(attention=attention)


@pytest.fixture()
def detection(attention: AttentionService) -> DetectionService:
    return DetectionService(attention=attention)


@pytest.fixture()
def pipeline(
    detection: DetectionService,
    followups: FollowUpService,
    attention: AttentionService,
) -> CommunicationPipeline:
    return CommunicationPipeline(
        detection=detection, followups=followups, attention=attention
    )


#: Statuses whose items are removed from the feed; everything else —
#: including the escalation targets of the R108 deadline-promotion path
#: (AC-R15) — is still visible ("live").
_NONLIVE = (AttentionState.SUPPRESSED, AttentionState.RESOLVED)


def _followup_items(attention: AttentionService) -> list[Any]:
    """Live, follow-up-sourced AttentionItems (R104's promotion)."""
    return [
        i
        for i in attention.list()
        if "followups" in i.sources and i.status not in _NONLIVE
    ]


# -- the §61 chain -----------------------------------------------------------


def test_full_61_chain(pipeline, detection, followups, attention):
    """Ingest → commitment finding (with why[]) → follow-up → due sweep →
    one live AttentionItem, with every id linked end-to-end."""
    comm = make_communication("message:8821", OUT, person_id="person:sam")
    first = ingest_text(pipeline, comm, TEXT, now=NOW)

    # Phase 1 (before the due time): finding + follow-up, nothing due.
    assert first.finding_ids == ["commitment-message:8821"]
    assert first.followup_ids
    fu_id = first.followup_ids[0]
    fu = followups.get(fu_id)
    assert fu is not None
    assert fu.status == FollowUpStatus.OPEN
    assert fu.target_kind == "commitment"
    assert fu.target_id == "commitment-message:8821"
    assert fu.due_at == FRIDAY_DUE
    assert first.evaluated == []
    assert first.attention_ids == []
    assert first.links[0].created is True
    assert _followup_items(attention) == []

    # R103's own attention seam (AC-R06): the finding's why[] surfaces in
    # the item's explanation, and the item's due drives its score.
    det_item = attention.get(first.detection_attention_ids["commitment-message:8821"])
    assert det_item is not None
    finding = detection.report_for("message:8821").findings[0]
    assert isinstance(finding, Commitment)
    assert finding.why, "AC-R04: findings must answer 'why'"
    assert any("detected commitment" in w for w in det_item.why), det_item.why
    assert det_item.due == FRIDAY_DUE

    # Phase 2 (same communication, later clock): reuse + due-sweep
    # promotion — this is the §61 progression.
    second = pipeline.ingest(comm, now=AFTER_DUE)

    assert len(second.evaluated) == 1
    rec = second.evaluated[0]
    assert rec["followup"].id == fu_id
    assert rec["event"].event_type == "followup.triggered"
    assert rec["days_overdue"] == 3
    item = rec["attention"]
    assert item.sources == ["followups"]
    assert item.status is AttentionState.NOW
    assert item.due == FRIDAY_DUE
    assert item.suggested_action.startswith("Follow up:")
    # Provenance back to the finding is in the explanation (AC-R06/R07);
    # audit fields are populated (AC-R15).
    assert any("commitment/commitment-message:8821" in w for w in item.why)
    assert item.created_at is not None and item.updated_at is not None
    audit = attention.policy.audit_response(item, now=AFTER_DUE)
    assert audit.attention_id == item.attention_id
    assert [a.action for a in audit.actions] == ["create"]

    # Id linkage: finding → follow-up → attention.
    assert fu.attention_id == item.attention_id
    assert second.attention_ids == [item.attention_id]
    link = second.links[0]
    assert (link.finding_id, link.followup_id, link.attention_id) == (
        "commitment-message:8821",
        fu_id,
        item.attention_id,
    )
    assert link.created is False
    assert item in attention.now()
    assert len(_followup_items(attention)) == 1


def test_followup_provenance_and_audit(pipeline, followups, attention):
    """The follow-up carries the deterministic provenance key, the person
    link, and an auditable history (AC-R07 created_from / §49)."""
    comm = make_communication("message:8821", OUT, person_id="person:sam")
    res = ingest_text(pipeline, comm, TEXT, now=NOW)
    fu = followups.get(res.followup_ids[0])
    assert fu.created_from == "commitment:commitment-message:8821"
    assert fu.person_id == "person:sam"
    assert fu.audit_history[0].action == "created"
    assert "commitment:commitment-message:8821" in fu.audit_history[0].detail
    # The follow-up engine's lifecycle events are inspectable (no NATS).
    created = followups.events("followup.created")
    assert len(created) == 1
    assert created[0].idempotency_key == f"followup.created:{fu.id}"


def test_evaluate_before_due_no_attention(pipeline, followups, attention):
    """A follow-up whose due time has not passed produces no follow-up
    attention — only R103's detection-seam item is live."""
    comm = make_communication("message:8821", OUT, person_id="person:sam")
    res = ingest_text(pipeline, comm, TEXT, now=NOW)
    fu = followups.get(res.followup_ids[0])
    assert fu.status == FollowUpStatus.OPEN
    assert fu.attention_id is None
    assert _followup_items(attention) == []
    assert followups.list_due(now=NOW) == []
    assert res.evaluated == []
    assert res.attention_ids == []


def test_open_ended_commitment_creates_no_followup(pipeline, followups, attention):
    """'I'll take care of it.' — commitment with no due time: a finding
    and a detection-seam attention item, but no follow-up."""
    comm = make_communication("m:70", OUT)
    res = ingest_text(pipeline, comm, "I'll take care of it.", now=NOW)
    assert res.finding_ids == ["commitment-m:70"]
    assert res.followup_ids == []
    assert res.attention_ids == []
    assert followups.list() == []
    # The detection's attention seam still surfaces the open commitment.
    assert len(res.detection_attention_ids) == 1
    item = attention.get(next(iter(res.detection_attention_ids.values())))
    assert item is not None and item.due is None


def test_incoming_request_creates_no_followup(pipeline, followups, attention):
    """An incoming request is answered by replying, not by following up
    with the other party — no follow-up for incoming findings."""
    comm = make_communication("m:71", IN, sender="sam@x.com", person_id="person:sam")
    res = ingest_text(pipeline, comm, "Please send me the report by Friday.", now=NOW)
    # request + deadline are detected (the deadline carries the due).
    assert res.finding_ids == ["request-m:71", "deadline-m:71"]
    assert res.followup_ids == []
    assert res.attention_ids == []
    assert followups.list() == []


# -- idempotent replay --------------------------------------------------------


def test_replay_does_not_duplicate(pipeline, detection, followups, attention):
    """Re-ingesting the same communication never duplicates findings,
    follow-ups, or live attention items."""
    comm = make_communication("message:8821", OUT, person_id="person:sam")
    first = ingest_text(pipeline, comm, TEXT, now=NOW)
    first_fu = first.followup_ids[0]

    # Replay at the same clock.
    second = ingest_text(pipeline, comm, TEXT, now=NOW)
    assert second.finding_ids == first.finding_ids
    assert second.followup_ids == [first_fu]
    assert len(followups.list()) == 1, "replay must not create a second follow-up"
    assert second.links[0].followup_id == first_fu
    assert second.links[0].created is False
    assert len(detection.report_for("message:8821").findings) == 1

    # Replay after the due time: the follow-up re-triggers but its single
    # live item is refreshed, never duplicated.
    third = pipeline.ingest(comm, now=AFTER_DUE)
    assert third.followup_ids == [first_fu]
    assert third.links[0].created is False
    item = attention.get(third.attention_ids[0])
    assert item is not None
    # One follow-up item + one detection-seam item, nothing more.
    assert len(attention.list()) == 2
    assert _followup_items(attention) == [item]
    assert followups.get(first_fu).attention_id == item.attention_id


# -- noise / precision ---------------------------------------------------------


def test_noise_message_yields_zero_everything(pipeline, detection, followups, attention):
    """A marketing sender is suppressed: zero findings, zero follow-ups,
    zero follow-up attention — even when the body is full of commitment
    language (the gate never reads the body)."""
    comm = make_communication(
        "n:90", OUT, sender="no-reply@shop.example", channel=CommunicationChannel.EMAIL
    )
    res = ingest_text(pipeline, comm, TEXT, now=NOW)
    assert res.finding_ids == []
    assert res.followup_ids == []
    assert res.attention_ids == []
    assert res.evaluated == []
    assert res.detection_attention_ids == {}
    assert detection.report_for("n:90").suppressed
    assert followups.list() == []
    assert attention.list() == []


# -- terminal follow-ups -------------------------------------------------------


def test_forgotten_followup_produces_no_attention(pipeline, followups, attention):
    """A terminal (FORGOTTEN) follow-up is excluded from the due sweep: no
    live attention, its item is resolved, and it stays immutable."""
    _, second = run_61(pipeline)
    fu = followups.get(second.followup_ids[0])
    item = attention.get(fu.attention_id)
    assert item is not None and item.status is AttentionState.NOW

    followups.forget(fu.id, note="no longer relevant", now=AFTER_DUE + timedelta(hours=1))
    assert fu.status == FollowUpStatus.FORGOTTEN
    assert item.status is AttentionState.RESOLVED
    assert _followup_items(attention) == []

    # A later due-evaluation must not resurrect it.
    comm = make_communication("message:8821", OUT, person_id="person:sam")
    third = pipeline.ingest(comm, now=AFTER_DUE + timedelta(days=1))
    assert third.evaluated == []
    assert third.attention_ids == []
    assert third.links[0].attention_id is None
    # The resolved item stays in the store, but no new live item appears.
    assert len(attention.list()) == 2  # detection-seam item + resolved fu item
    assert _followup_items(attention) == []
    # Terminal states are immutable.
    with pytest.raises(FollowUpTransitionError):
        followups.snooze(fu.id, now=AFTER_DUE + timedelta(days=2))
    assert any(a.action == "forgotten" for a in fu.audit_history)


def test_completed_followup_resolves_its_item(pipeline, followups, attention):
    """Completing a follow-up resolves the live item the same way."""
    _, second = run_61(pipeline)
    fu = followups.get(second.followup_ids[0])
    item = attention.get(fu.attention_id)
    assert item is not None and item.status is AttentionState.NOW

    followups.complete(fu.id, note="contract sent", now=AFTER_DUE + timedelta(hours=1))
    assert fu.status == FollowUpStatus.COMPLETED
    assert item.status is AttentionState.RESOLVED
    assert _followup_items(attention) == []
    comm = make_communication("message:8821", OUT, person_id="person:sam")
    third = pipeline.ingest(comm, now=AFTER_DUE + timedelta(days=1))
    assert third.attention_ids == []
    assert third.evaluated == []


# -- PipelineResult shape --------------------------------------------------------


def test_pipeline_result_shape(pipeline, followups, attention):
    """PipelineResult is a plain pydantic trace carrying every id."""
    _, res = run_61(pipeline)
    assert isinstance(res, PipelineResult)
    assert res.communication_id == "message:8821"
    assert res.detected_at == AFTER_DUE
    assert res.finding_ids == ["commitment-message:8821"]
    assert set(res.detection_attention_ids) == {"commitment-message:8821"}
    assert len(res.followup_ids) == 1
    assert len(res.attention_ids) == 1
    assert len(res.links) == 1
    assert res.links[0].finding_kind == "commitment"
    assert res.links[0].created is False  # the follow-up came from phase 1
    # Evaluation records carry the AC-R07 promotion evidence.
    assert len(res.evaluated) == 1
    rec: dict[str, Any] = res.evaluated[0]
    assert set(rec) == {"followup", "attention", "event", "days_overdue"}
    assert rec["days_overdue"] == 3
    assert rec["event"].event_type == "followup.triggered"
    # JSON-safe dump (auditability).
    dumped = res.model_dump(mode="json")
    assert dumped["links"][0]["finding_id"] == "commitment-message:8821"
    assert dumped["followup_ids"] == res.followup_ids


# -- pinned-clock determinism ----------------------------------------------------


def _normalized(res: PipelineResult, item: Any) -> dict[str, Any]:
    """Deterministic projection of a run: everything the pinned clocks
    determine. Excludes uuid ids and the attention scorer's real-clock
    deadline-proximity factor (documented P0 behavior — the scorer uses its
    own clock, not the pipeline's pinned one)."""
    why = [
        w
        for w in item.why
        if not w.startswith("explicit deadline")
        and not w.startswith("deadline already passed")
    ]
    return {
        "communication_id": res.communication_id,
        "detected_at": res.detected_at.isoformat(),
        "finding_ids": res.finding_ids,
        "detection_attention_keys": sorted(res.detection_attention_ids),
        "followup_count": len(res.followup_ids),
        "link_keys": [
            {"finding_id": l.finding_id, "kind": l.finding_kind, "created": l.created}
            for l in res.links
        ],
        "evaluated": [
            {
                "status": r["followup"].status,
                "days_overdue": r["days_overdue"],
                "event_type": r["event"].event_type,
                "attention_status": r["attention"].status.value,
            }
            for r in res.evaluated
        ],
        "attention_live": len(res.attention_ids),
        "item": {
            "reason": item.reason,
            "urgency": item.urgency,
            "importance": item.importance,
            "due": item.due.isoformat() if item.due else None,
            "entities": item.entities,
            "sources": item.sources,
            "suggested_action": item.suggested_action,
            "status": item.status.value,
            "why": why,
        },
    }


def _run_once() -> tuple[PipelineResult, Any]:
    """One independent end-to-end run at the same pinned clocks."""
    attention = AttentionService()
    detection = DetectionService(attention=attention)
    followups = FollowUpService(attention=attention)
    pipeline = CommunicationPipeline(
        detection=detection, followups=followups, attention=attention
    )
    first, second = run_61(pipeline)
    del first  # phase 1 is identical by construction (same pinned NOW)
    item = attention.get(second.attention_ids[0])
    return second, item


def test_pinned_clock_determinism():
    """Two runs at the same pinned clocks produce identical output
    (modulo uuid ids / the scorer's own-clock proximity factor)."""
    a = _normalized(*_run_once())
    b = _normalized(*_run_once())
    assert a == b
    # The deterministic core is actually exercised, not vacuous.
    assert a["finding_ids"] == ["commitment-message:8821"]
    assert a["item"]["status"] == "NOW"
    assert a["item"]["sources"] == ["followups"]
    assert a["item"]["urgency"] == pytest.approx(0.75)  # 3 days overdue
    assert any("commitment/commitment-message:8821" in w for w in a["item"]["why"])
    assert a["evaluated"][0]["days_overdue"] == 3


# -- module convenience / singleton seam ------------------------------------------


def test_get_pipeline_binds_existing_singletons():
    """get_pipeline() is the one-line seam: it binds the three existing
    process-wide singletons, and reset_pipeline() forgets the binding."""
    reset_detection_service()
    reset_followup_service()
    reset_attention_service()
    reset_pipeline()
    try:
        pipe = get_pipeline()
        assert isinstance(pipe, CommunicationPipeline)
        assert pipe.detection is get_detection_service()
        assert pipe.followups is get_followup_service()
        assert pipe.attention is get_attention_service()
        # One ingest through the seam touches only the singletons.
        comm = make_communication("message:8821", OUT, person_id="person:sam")
        pipe.detection.register_content(comm.content_ref, TEXT)
        res = pipe.ingest(comm, now=NOW)
        assert res.finding_ids == ["commitment-message:8821"]
        assert len(get_followup_service().list()) == 1
        assert len(res.followup_ids) == 1
        # Same instance until reset.
        assert get_pipeline() is pipe
        reset_pipeline()
        assert get_pipeline() is not pipe
    finally:
        reset_detection_service()
        reset_followup_service()
        reset_attention_service()
        reset_pipeline()


def test_injected_pipeline_never_touches_singletons(detection, followups, attention):
    """Class tests with injected services are hermetic: the singletons
    stay untouched."""
    reset_detection_service()
    reset_followup_service()
    reset_attention_service()
    try:
        comm = make_communication("message:8821", OUT, person_id="person:sam")
        res = ingest_text(
            CommunicationPipeline(
                detection=detection, followups=followups, attention=attention
            ),
            comm,
            TEXT,
            now=NOW,
        )
        assert res.followup_ids
        assert get_followup_service().list() == []
        assert get_detection_service().all_findings() == []
        assert get_attention_service().list() == []
    finally:
        reset_detection_service()
        reset_followup_service()
        reset_attention_service()
