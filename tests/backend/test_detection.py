"""Gaps4 R103 — deterministic commitment / request / deadline detection tests.

Covers prompt2 §16–§18 (Commitment / Request / Deadline detection) with the
AC gates from ``gaps2.md`` / ``gaps4.md``:

- **AC-R04 — typed findings:** every finding is a
  :class:`~backend.communications.detection.findings.Commitment`,
  ``Request`` or ``Deadline`` carrying the required fields
  (``created_from``, ``confidence``, ``status``, ``due``/``due_info``,
  non-empty ``why[]``, ``model_used``).
- **Recall-first** (prompt2 §72): all six §16 commitment examples and all
  six §17 request examples are detected; deadlines (§18) parse explicit
  dates, relative dates, and business-hour constraints.
- **Precision-first** (prompt2 §72): marketing / newsletter / social
  content is suppressed (zero findings, reason recorded) while personal
  messages are never dropped by the gate.
- **Deterministic path fully green with assist off:** identical input
  produces identical output and ``model_used`` stays
  ``deterministic-extractor``; an :class:`InferenceAssist` may only *refine*
  (never create) findings.
- **§8 events + attention seam (AC-R06):** one ``*.detected`` event per
  finding with a stable idempotency key; the overdue sweep emits the
  overdue variants; findings feed the *existing*
  :class:`~backend.attention.service.AttentionService` through its public
  ``create`` / ``refresh`` seam (one live item per finding, refreshed, not
  duplicated, and the explainable ``_score`` is not replaced).
- **No-plaintext policy (§7 / R107):** findings and events carry only the
  ``content_ref`` pointer, a matched expression, and metadata — never the
  body.

The detection service is in-memory (house style of the personal planes), so
tests use a fresh :class:`AttentionService` per test plus the reset hooks —
no database or NATS required. A fixed ``now`` anchor makes all
relative-date math deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from backend.api.app import create_app
from backend.attention.service import AttentionService
from backend.communications.detection.assist import (
    AssistRefinement,
    assist_context_for,
)
from backend.communications.detection.dates import parse_due_expression
from backend.communications.detection.extractors import detect_communication
from backend.communications.detection.findings import (
    DETERMINISTIC_DETECTOR,
    Commitment,
    Deadline,
    DetectionReport,
    Finding,
    FindingStatus,
    Request,
)
from backend.communications.detection.noise import (
    NOISE_SUPPRESSIONS,
    is_marketing,
    register_subject,
)
from backend.communications.detection.service import DetectionService
from backend.communications.models import (
    Communication,
    CommunicationChannel,
    CommunicationDirection,
)
from backend.communications.detection.noise import clear_subjects
from backend.communications.registry import reset_communications_service

# Fixed anchor: Tuesday 2026-10-20 10:00 UTC (weekday math is deterministic).
NOW = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
OUT = CommunicationDirection.OUTGOING
IN = CommunicationDirection.INCOMING


# -- fixtures / helpers --------------------------------------------------------


@pytest.fixture()
def attention() -> AttentionService:
    return AttentionService()


@pytest.fixture()
def detection(attention: AttentionService) -> DetectionService:
    svc = DetectionService(attention=attention)
    yield svc
    svc.reset()


@pytest.fixture(autouse=True)
def _clean_state():
    clear_subjects()
    reset_communications_service()
    yield
    clear_subjects()
    reset_communications_service()


def make_communication(
    comm_id: str,
    direction: CommunicationDirection,
    *,
    sender: str = "counterpart",
    channel: CommunicationChannel = CommunicationChannel.SMS,
) -> Communication:
    return Communication(
        communication_id=comm_id,
        channel=channel,
        direction=direction,
        sender=sender,
        timestamp=NOW,
        content_ref=f"cr:{comm_id}",
    )


def detect(detection: DetectionService, text: str, direction: CommunicationDirection,
           comm_id: str = "m:1", **kwargs: Any) -> DetectionReport:
    comm = make_communication(comm_id, direction, **kwargs)
    detection.register_content(comm.content_ref, text)
    return detection.detect(comm, now=NOW)


# -- AC-R04: recall-first commitment examples (prompt2 §16) ---------------------


@pytest.mark.parametrize(
    ("text", "due_iso", "why_contains"),
    [
        ("I'll send it tonight.", "2026-10-20T20:00:00+00:00", "tonight"),
        ("I'll call tomorrow.", "2026-10-21T17:00:00+00:00", "tomorrow"),
        ("I can get that done Friday.", "2026-10-23T17:00:00+00:00", "friday"),
        ("Remind me next week.", "2026-10-26T09:00:00+00:00", "next week"),
        ("I'll take care of it.", None, None),
        ("I'll check and get back to you.", None, None),
    ],
)
def test_commitment_recall(detection, text, due_iso, why_contains):
    """All six §16 examples become an OPEN Commitment (recall-first)."""
    report = detect(detection, text, OUT, comm_id="message:8821")
    assert len(report.findings) == 1, "every §16 example must yield a finding"
    finding = report.findings[0]
    assert isinstance(finding, Commitment)
    assert finding.kind == "commitment"
    assert finding.created_from == "message:8821"
    assert finding.status is FindingStatus.OPEN
    assert finding.why, "why[] must be non-empty (§22)"
    assert 0.0 < finding.confidence <= 1.0
    assert finding.model_used == DETERMINISTIC_DETECTOR
    if due_iso is None:
        assert finding.due is None, "open-ended promise carries no due"
    else:
        assert finding.due is not None
        assert finding.due.isoformat() == due_iso
        assert finding.due_info is not None
    if why_contains is not None:
        assert any(why_contains in w.lower() for w in finding.why), finding.why


def test_commitment_fields_are_complete():
    """AC-R04 required fields on the §16 canonical example (message:8821)."""
    detection = DetectionService(attention=AttentionService())
    report = detect(detection, "I'll send it tonight.", OUT, comm_id="message:8821")
    f = report.findings[0]
    dump = f.model_dump()
    for field in ("created_from", "confidence", "status", "due", "why", "model_used"):
        assert field in dump, f"missing AC-R04 field {field}"
    assert dump["created_from"] == "message:8821"
    assert dump["status"] == FindingStatus.OPEN.value
    assert dump["why"], "why[] must not be empty"
    detection.reset()


def test_commitment_outgoing_only():
    """First-person promises are outgoing-user signals; the same text
    incoming is *not* the user's commitment (no false detection)."""
    detection = DetectionService(attention=AttentionService())
    incoming = detect(detection, "I'll call tomorrow.", IN, comm_id="m:2")
    assert incoming.findings == ()
    detection.reset()


# -- AC-R04: recall-first request examples (prompt2 §17) ------------------------


@pytest.mark.parametrize(
    ("text", "why_contains"),
    [
        ("Can you send me the report.", "can you s"),
        ("Please call me back.", "please"),
        ("Are you available on Thursday?", "available"),
        ("Need this by Friday.", "need"),
        ("Could you review the draft.", "could you r"),
        ("Can you confirm the date?", "can you c"),
    ],
)
def test_request_recall(detection, text, why_contains):
    """All six §17 examples become an OPEN Request (candidate, not priority)."""
    report = detect(detection, text, IN, comm_id="m:10")
    kinds = [f.kind for f in report.findings]
    assert "request" in kinds, f"no request detected for {text!r}: {kinds}"
    req = next(f for f in report.findings if f.kind == "request")
    assert isinstance(req, Request)
    assert req.status is FindingStatus.OPEN
    assert req.why and any(why_contains in w.lower() for w in req.why)
    assert req.created_from == "m:10"


def test_request_with_due_keeps_due_on_deadline_not_request():
    """§17: a request does not automatically become high priority; the
    due of "We need the form by Tuesday." lives on the Deadline finding
    (Tuesday after the Tuesday anchor → next Tuesday)."""
    detection = DetectionService(attention=AttentionService())
    report = detect(detection, "We need the form by Tuesday.", IN, comm_id="m:11")
    by_kind = {f.kind: f for f in report.findings}
    assert set(by_kind) == {"request", "deadline"}
    assert by_kind["request"].due is None
    deadline = by_kind["deadline"]
    assert isinstance(deadline, Deadline)
    assert deadline.due is not None
    assert deadline.due.isoformat() == "2026-10-27T17:00:00+00:00"
    detection.reset()


# -- §18: deadline date parsing (explicit / relative / business-hour) -----------


@pytest.mark.parametrize(
    ("text", "due_iso", "time_stated"),
    [
        ("due 2026-11-02", "2026-11-02T17:00:00+00:00", False),
        ("due 2026-11-02 14:30:00", "2026-11-02T14:30:00+00:00", True),
        ("by November 2", "2026-11-02T17:00:00+00:00", False),
        ("by Nov 2nd", "2026-11-02T17:00:00+00:00", False),
        ("by 11/2", "2026-11-02T17:00:00+00:00", False),
        ("by 11/2/2026", "2026-11-02T17:00:00+00:00", False),
        ("tomorrow", "2026-10-21T17:00:00+00:00", False),
        ("tonight", "2026-10-20T20:00:00+00:00", True),
        ("today", "2026-10-20T17:00:00+00:00", False),
        ("next week", "2026-10-26T09:00:00+00:00", True),
        ("by Friday", "2026-10-23T17:00:00+00:00", False),
        ("by 5 pm", "2026-10-20T17:00:00+00:00", True),
    ],
)
def test_deadline_date_parsing(text, due_iso, time_stated):
    """One shared deterministic parser handles §18's due forms."""
    info = parse_due_expression(text, now=NOW)
    assert info is not None, f"no due parsed for {text!r}"
    assert info.due.isoformat() == due_iso
    assert info.time_stated is time_stated
    assert info.confidence > 0.0
    assert info.expr


def test_deadline_no_date_no_guess():
    """No supported due form → None (never a guessed due)."""
    assert parse_due_expression("I'll take care of it.", now=NOW) is None
    assert parse_due_expression("sounds good to me", now=NOW) is None


def test_deadline_requires_due_field():
    """A Deadline without a parsed due is rejected by validation."""
    with pytest.raises(ValidationError):
        Deadline(
            finding_id="deadline-x",
            created_from="m:1",
            content_ref="cr:x",
            why=["due expected"],
        )


def test_deadline_from_incoming_communication(detection):
    """§18 "We need the form by Tuesday." → a Deadline finding (0.91)."""
    report = detect(detection, "Need this by Friday.", IN, comm_id="m:20")
    deadline = next(f for f in report.findings if isinstance(f, Deadline))
    assert deadline.due is not None
    assert deadline.due.isoformat() == "2026-10-23T17:00:00+00:00"
    assert deadline.due_info is not None and deadline.due_info.time_stated is False
    assert deadline.model_used == DETERMINISTIC_DETECTOR


# -- noise gate: precision-first suppression -----------------------------------


@pytest.mark.parametrize(
    ("sender", "subject", "suppress"),
    [
        ("deal@offers.example", None, True),
        ("Digest <digest@news.example>", None, True),
        ("no-reply@shop.example", None, True),
        ("updates@marketing.example", None, True),
        ("alerts@notifications.example", None, True),
        ("corp@example.com", "Your weekly digest", True),
        ("corp@example.com", "This week's roundup", True),
        ("corp@example.com", "Exclusive offer for you", True),
        ("corp@example.com", "unsubscribe — your weekly update", True),
        ("friend@example.com", None, False),
        ("Alice <alice@example.com>", "lunch tomorrow?", False),
        ("Alice", "unsubscribe", False),  # display name, no keyword
        ("corp@example.com", "unsubscribe", False),  # bare link, no keyword
        ("offers.corp.example", "please review the contract", False),
    ],
)
def test_noise_gate_precision(detection, sender, subject, suppress):
    """Marketing / newsletter / social in, personal out — zero FP on
    personal metadata, zero findings on suppressed noise."""
    # §16 commitments are outgoing-only, and the gate is direction-agnostic
    # (it reads only sender + subject), so a single outgoing comm exercises
    # both branches: suppressed noise yields zero findings, a personal message
    # must not be dropped and yields the commitment in its body.
    comm = make_communication(
        "n:1", OUT, sender=sender, channel=CommunicationChannel.EMAIL
    )
    if subject is not None:
        register_subject(comm.content_ref, subject)
    hit = is_marketing(comm)
    assert (hit is not None) is suppress, (
        f"gate mismatch for sender={sender!r} subject={subject!r}"
    )

    body = "I'll call tomorrow. Unsubscribe from nothing here."
    detection.register_content(comm.content_ref, body)
    report = detection.detect(comm, now=NOW)
    if suppress:
        assert report.findings == (), "suppressed noise must yield zero findings"
        assert report.suppressed, "suppression must be auditable (reason recorded)"
    else:
        assert isinstance(report.findings[0], Commitment), (
            "personal message with commitment language must not be dropped"
        )


def test_noise_gate_body_is_never_read(detection):
    """A marketing sender suppresses even when the body is full of
    commitment language — the gate never lets body content leak into
    findings (no-plaintext policy)."""
    report = detect(
        detection,
        "I'll call tomorrow. Please review the deal by Friday.",
        IN,
        comm_id="n:2",
        sender="promo@deals.example",
        channel=CommunicationChannel.EMAIL,
    )
    assert report.findings == ()
    assert report.suppressed
    for ev in report.events:
        assert ev.payload is not None
        assert "I'll call tomorrow" not in str(ev.payload)


def test_noise_suppression_vocabulary_is_stable():
    """Tests/fixtures can assert against the gate's exact vocabulary."""
    assert "newsletter" in NOISE_SUPPRESSIONS["sender_domains"]
    assert "unsubscribe" in str(NOISE_SUPPRESSIONS["subject_markers"]).lower()


# -- §8 events -----------------------------------------------------------------


def test_detected_events_per_finding(detection):
    """Each finding emits its §8 event with a stable idempotency key."""
    report = detect(detection, "I'll send it tonight.", OUT, comm_id="message:8821")
    assert len(report.events) == 1
    ev = report.events[0]
    assert ev.event_type == "commitment.detected"
    assert ev.category == "knowledge"
    assert ev.source == "communications.detection"
    assert ev.subject == "commitment-message:8821"
    assert ev.idempotency_key == "commitment.detected:commitment-message:8821"
    assert ev.occurred_at == NOW
    assert ev.severity == "info"
    payload = ev.payload
    assert payload["kind"] == "commitment"
    assert payload["created_from"] == "message:8821"
    assert payload["content_ref"] == "cr:message:8821"
    assert payload["status"] == "OPEN"
    assert payload["model_used"] == DETERMINISTIC_DETECTOR
    assert payload["due"] == "2026-10-20T20:00:00+00:00"
    assert payload["due_info"]["expr"].startswith("tonight")
    assert payload["why"], "event payload must carry the explainable why[]"


def test_mixed_findings_emit_one_event_each(detection):
    """Request + deadline in one communication → two events, right types."""
    report = detect(detection, "We need the form by Tuesday.", IN, comm_id="m:30")
    types = [ev.event_type for ev in report.events]
    assert types == ["request.detected", "deadline.detected"]
    assert {ev.subject for ev in report.events} == {
        "request-m:30",
        "deadline-m:30",
    }


def test_suppressed_communication_emits_no_events(detection):
    report = detect(
        detection,
        "I'll call tomorrow.",
        IN,
        comm_id="n:3",
        sender="digest@news.example",
        channel=CommunicationChannel.EMAIL,
    )
    assert report.events == ()
    assert report.findings == ()
    assert report.suppressed


# -- attention seam (AC-R06) ----------------------------------------------------


def test_attention_seam_one_item_per_finding(detection, attention):
    """Findings feed the *existing* AttentionService through its public
    seam: one live item per finding, explainable why, due drives _score."""
    report = detect(detection, "I'll send it tonight.", OUT, comm_id="m:40")
    assert len(attention.list()) == 1
    item = attention.list()[0]
    assert item.status.value == "NOW"
    assert item.reason.startswith("Follow through:")
    assert item.due is not None and item.due.isoformat() == "2026-10-20T20:00:00+00:00"
    assert item.suggested_action == "Complete the commitment"
    assert item.sources == ["sms"]
    assert item.why, "attention item must stay explainable (§22)"
    assert report.attention_ids, "report must map finding → attention item"
    assert list(report.attention_ids) == ["commitment-m:40"]
    assert attention.get(report.attention_ids["commitment-m:40"]) is item


def test_attention_re_detect_refreshes_not_duplicates(detection, attention):
    report1 = detect(detection, "I'll send it tonight.", OUT, comm_id="m:41")
    assert len(attention.list()) == 1
    report2 = detect(detection, "I'll send it tonight.", OUT, comm_id="m:41")
    assert len(attention.list()) == 1, "re-detection must refresh, not stack"
    assert report2.attention_ids == report1.attention_ids


def test_attention_why_carries_detection_explanations(detection, attention):
    detect(detection, "I can get that done Friday.", OUT, comm_id="m:42")
    item = attention.list()[0]
    assert any("friday" in w.lower() for w in item.why)
    assert item.priority > 0.0


# -- overdue sweep (status transitions + §8 overdue events) ---------------------


def test_sweep_overdue_commitment_and_deadline(detection, attention):
    """OPEN findings past their due flip (commitment→OVERDUE,
    deadline→PASSING) with the §8 overdue events + attention refresh."""
    detect(detection, "I'll send it tonight.", OUT, comm_id="m:50")  # due 20:00
    # "Need this by tomorrow." yields both a Request (no due) and a Deadline
    # due +1d 17:00 (end of business day); only the deadline is swept.
    detect(detection, "Need this by tomorrow.", IN, comm_id="m:51")
    assert all(f.status is FindingStatus.OPEN for f in detection.all_findings())

    # Sweep after *both* dues have passed (20:00 tonight and 17:00 tomorrow):
    # the commitment flips to OVERDUE, the deadline to PASSING, the request
    # stays untouched.
    sweep_now = (NOW + timedelta(days=1)).replace(hour=17, minute=30)
    events = detection.sweep_overdue(now=sweep_now)
    assert [ev.event_type for ev in events] == [
        "commitment.overdue",
        "deadline.passed",
    ]
    assert all(ev.severity == "warning" for ev in events)
    assert events[0].subject == "commitment-m:50"
    assert events[1].subject == "deadline-m:51"

    assert (
        detection.report_for("m:50").by_id("commitment-m:50").status
        is FindingStatus.OVERDUE
    )
    assert (
        detection.report_for("m:51").by_id("deadline-m:51").status
        is FindingStatus.PASSING
    )
    # attention refreshed, not duplicated (one live item per finding)
    assert len(attention.list()) == 3


def test_sweep_future_due_is_not_flipped(detection):
    detect(detection, "Remind me next week.", OUT, comm_id="m:60")  # due +6d 09:00
    events = detection.sweep_overdue(now=NOW.replace(hour=23))  # same day
    assert events == ()
    assert detection.all_findings(kind="commitment")[0].status is FindingStatus.OPEN


def test_sweep_open_ended_is_not_flipped(detection):
    detect(detection, "I'll take care of it.", OUT, comm_id="m:61")  # no due
    events = detection.sweep_overdue(now=NOW.replace(hour=23))
    assert events == ()
    assert detection.all_findings()[0].status is FindingStatus.OPEN


def test_sweep_communication_not_in_registry(detection, attention):
    """The sweep's attention refresh must not depend on the global
    registry (communications detected directly, not ingested)."""
    detect(detection, "I'll send it tonight.", OUT, comm_id="m:62")
    reset_communications_service()  # registry no longer knows m:62
    events = detection.sweep_overdue(now=NOW.replace(hour=21))
    assert [ev.event_type for ev in events] == ["commitment.overdue"]
    item = attention.get(list(detection.report_for("m:62").attention_ids.values())[0])
    assert item is not None


# -- explicit resolution ---------------------------------------------------------


def test_resolve_commitment_emits_completed(detection, attention):
    detect(detection, "I'll send it tonight.", OUT, comm_id="m:70")
    aid = list(detection.report_for("m:70").attention_ids.values())[0]

    event = detection.resolve_finding("commitment-m:70", now=NOW)
    assert event is not None
    assert event.event_type == "commitment.completed"
    assert event.payload["status"] == "PASSING"

    finding = detection.report_for("m:70").by_id("commitment-m:70")
    assert finding.status is FindingStatus.PASSING
    assert attention.get(aid).status.value == "RESOLVED"


def test_resolve_request_emits_resolved(detection):
    detect(detection, "Can you confirm the date?", IN, comm_id="m:71")
    event = detection.resolve_finding("request-m:71", now=NOW)
    assert event is not None
    assert event.event_type == "request.resolved"


def test_resolve_unknown_or_deadline_returns_none(detection):
    detect(detection, "I'll send it tonight.", OUT, comm_id="m:72")
    assert detection.resolve_finding("commitment-nope") is None
    detect(detection, "Need this by Friday.", IN, comm_id="m:73")
    assert detection.resolve_finding("deadline-m:73") is None  # deadlines are swept


# -- assist path (refine-only, off by default) -----------------------------------


class _TestAssist:
    """A deterministic test assist that refines every finding."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def model_name(self) -> str:
        return "assist-test-model"

    @property
    def enabled(self) -> bool:
        return True

    def refine(self, finding, context):
        self.calls += 1
        assert context.content_ref == finding.content_ref
        return AssistRefinement(
            reason="assist: pattern confirmed",
            confidence=0.97,
            model_used=self.model_name,
        )


def test_assist_disabled_by_default_keeps_deterministic_tag(detection):
    report = detect(detection, "I'll send it tonight.", OUT, comm_id="m:80")
    assert report.findings[0].model_used == DETERMINISTIC_DETECTOR
    assert report.model_used == DETERMINISTIC_DETECTOR
    assert "assist" not in str(report.findings[0].why).lower()


def test_assist_refines_but_never_creates(detection):
    assist = _TestAssist()
    svc = DetectionService(attention=attention_fixture(), assist=assist)
    report = _detect_on(svc, "I'll call tomorrow.", OUT, comm_id="m:81")

    assert assist.calls == 1, "assist ran once per deterministic finding"
    f = report.findings[0]
    assert f.model_used == "assist-test-model"
    assert f.confidence == 0.97
    assert any("assist: pattern confirmed" in w for w in f.why)
    assert f.created_from == "m:81"
    # refinement never changes provenance of the detection itself
    assert f.kind == "commitment"
    assert f.due is not None

    # noise + assist → still zero findings (assist cannot create)
    report2 = _detect_on(svc, "I'll call tomorrow.", IN, comm_id="n:4",
                         sender="digest@news.example",
                         channel=CommunicationChannel.EMAIL)
    assert report2.findings == ()
    assert assist.calls == 1  # no findings → no refine calls


def test_deterministic_identical_input_identical_output():
    """AC-R04: deterministic path fully green with assist off — identical
    input produces byte-identical findings and events."""
    a = DetectionService(attention=AttentionService())
    b = DetectionService(attention=AttentionService())
    r1 = detect(a, "I'll call tomorrow.", OUT, comm_id="m:90")
    r2 = detect(b, "I'll call tomorrow.", OUT, comm_id="m:90")
    assert r1.findings[0].model_dump_json() == r2.findings[0].model_dump_json()
    assert (
        [ev.model_dump(exclude={"id", "hash"}) for ev in r1.events]
        == [ev.model_dump(exclude={"id", "hash"}) for ev in r2.events]
    )
    a.reset()
    b.reset()


# -- no-plaintext policy (§7 / R107) ----------------------------------------------


def test_no_plaintext_in_findings_or_events(detection):
    body = "I'll send the zebra-quantum-7731 document by tomorrow."
    report = detect(detection, body, OUT, comm_id="m:100")
    finding = report.findings[0]
    secret = "zebra-quantum-7731"
    assert secret not in finding.model_dump_json()
    assert secret not in str(report.model_dump())
    for ev in report.events:
        assert secret not in ev.model_dump()
    assert finding.matched and len(finding.matched) <= 80
    assert finding.created_from == "m:100"
    assert finding.content_ref == "cr:m:100"


def test_communication_record_carries_only_pointer():
    comm = make_communication("m:101", OUT)
    assert "content_ref" in comm.model_fields
    assert not hasattr(comm, "body")
    assert comm.content_ref == "cr:m:101"


# -- pure extractor contract ------------------------------------------------------


def test_extractors_are_pure_functions():
    """Same (communication, text, now) → same report, no global state."""
    comm = make_communication("m:102", OUT)
    r1 = detect_communication(comm, "I'll call tomorrow.", now=NOW)
    r2 = detect_communication(comm, "I'll call tomorrow.", now=NOW)
    assert r1.findings[0].model_dump() == r2.findings[0].model_dump()
    assert r1.model_dump(exclude={"detected_at"}) == r2.model_dump(exclude={"detected_at"})


def test_empty_text_yields_no_findings(detection):
    report = detect(detection, "   ", OUT, comm_id="m:103")
    assert report.findings == ()
    assert report.events == ()


def test_finding_id_is_stable_and_kind_scoped():
    comm = make_communication("message:999", IN)
    r = detect_communication(comm, "Can you confirm the date?", now=NOW)
    assert r.findings[0].finding_id == "request-message:999"
    assert r.findings[0].communication_id == "message:999"


# -- app wiring gate (house convention: API builds the graph) ---------------------


def test_app_wires_detection_via_api_import():
    """Importing the API module graph must expose the detection service
    singleton (the house wiring path used by every personal plane)."""
    app = create_app()
    assert app is not None
    from backend.communications.detection import (
        DetectionService,
        get_detection_service,
        reset_detection_service,
    )

    svc = get_detection_service()
    assert isinstance(svc, DetectionService)
    assert get_detection_service() is svc
    reset_detection_service()
    assert get_detection_service() is not svc


def attention_fixture() -> AttentionService:
    return AttentionService()


def _detect_on(
    svc: DetectionService, text: str, direction: CommunicationDirection,
    comm_id: str, **kwargs: Any,
) -> DetectionReport:
    comm = make_communication(comm_id, direction, **kwargs)
    svc.register_content(comm.content_ref, text)
    return svc.detect(comm, now=NOW)
