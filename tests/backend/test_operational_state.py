"""R109 — Daily Operational State (§52) tests (AC-R17, AC-R09).

The ``GET /api/briefing/operational`` route (and the
:meth:`backend.briefing.operational.compose_operational` it wraps) returns the
§52 rollup — the user's current operational picture — as a *bare* object (not
the §46 ``ListResponse`` envelope) consumed by the ``nerve today`` CLI (AC-R11)
and the §64 ``get_daily_brief`` MCP tool (AC-R10).

The rollup projects eight §52 sections, each from its *owning* service (R109
step 4 — "do not invent aggregate data"):

- in-memory planes (always available): detection (commitments / requests /
  deadlines), waiting-on, calendar;
- persistence-backed services (on ``app.state.services``): goals, incidents,
  approvals (``policy``).

Every test pins the clock through an injected ``now`` so the time-relative
sections (upcoming deadlines, blocked goals, waiting-on day counts) are exact
and reproducible. The tests assert:

- all eight sections are always present (even empty) and the shape is stable;
- each section is populated from its owning service when seeded;
- the state is deterministic and reproducible for a given input + clock;
- sections do not contaminate one another (a finding of one kind never leaks
  into another kind's section; filtered rows never appear);
- sections whose owning service is absent render as honest empties.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

import backend.api.app  # noqa: F401  (resolves the attention<->api cycle, like the suite)
from backend.api.app import create_app
from backend.attention.service import AttentionService
from backend.briefing.operational import (
    SECTION_KEYS,
    compose_operational,
)
from backend.calendar import CalendarService
from backend.calendar.service import reset_calendar_service
from backend.communications.detection.findings import (
    Commitment,
    Deadline,
    FindingStatus,
    Request,
)
from backend.communications.detection.service import (
    DetectionService,
    reset_detection_service,
)
from backend.communications.models import (
    Communication,
    CommunicationChannel,
    CommunicationDirection,
)
from backend.waiting_on import reset_waiting_on_service
from backend.waiting_on.service import WaitingOnService

# Fixed anchor: Tuesday 2026-10-20 10:00 UTC (weekday math is deterministic).
PINNED_NOW = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
OUT = CommunicationDirection.OUTGOING
IN = CommunicationDirection.INCOMING


# -- fakes for the persistence-backed owning services ---------------------
# These stand in for the real GoalService / IncidentService / approval store
# that live on ``app.state.services`` only when ``NERVE_DB_URL`` is wired. They
# implement the exact no-arg read method the composer calls (via
# ``_first_result``), returning plain rows (dicts / pydantic models).


class _GoalsService:
    """Stand-in for ``GoalService`` — exposes ``list_goals`` (async)."""

    def __init__(self, goals: list[Any]) -> None:
        self._goals = goals

    async def list_goals(self, **kwargs: Any) -> list[Any]:
        return self._goals


class _IncidentsService:
    """Stand-in for ``IncidentService`` — exposes ``list_incidents`` (async)."""

    def __init__(self, incidents: list[dict[str, Any]]) -> None:
        self._incidents = incidents

    async def list_incidents(self) -> list[dict[str, Any]]:
        return self._incidents


class _ApprovalsService:
    """Stand-in for the ``policy`` approvals store — exposes ``list_approvals``."""

    def __init__(self, approvals: list[dict[str, Any]]) -> None:
        self._approvals = approvals

    async def list_approvals(self) -> list[dict[str, Any]]:
        return self._approvals


# -- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_planes():
    """Reset the in-memory plane singletons before and after each test so the
    composer's defaulted singletons (detection / waiting-on / calendar) start
    empty and do not leak state across tests."""
    reset_detection_service()
    reset_waiting_on_service()
    reset_calendar_service()
    yield
    reset_detection_service()
    reset_waiting_on_service()
    reset_calendar_service()


def _compose(*, services=None, now=PINNED_NOW, detection=None, waiting_on=None,
             calendar=None):
    return compose_operational(
        services, now=now, detection=detection, waiting_on=waiting_on,
        calendar=calendar,
    )


def _finding(kind: str, finding_id: str, *, status=FindingStatus.OPEN, due=None,
             confidence: float = 0.9):
    """Build one typed finding directly (precise control over kind/status/due,
    independent of the deterministic extractor's text matching)."""
    cls = {"commitment": Commitment, "request": Request, "deadline": Deadline}[kind]
    fields: dict[str, Any] = dict(
        finding_id=finding_id,
        created_from=f"comm:{finding_id}",
        content_ref=f"cr:{finding_id}",
        status=status,
        confidence=confidence,
        why=[f"{kind} test finding"],
        matched="test",
    )
    if due is not None:
        fields["due"] = due
    return cls(**fields)


class _FakeDetection:
    """A stand-in for ``DetectionService.all_findings`` returning hand-built
    findings — precise control over which findings are present and their
    status/due, to test the composer's selection filters directly."""

    def __init__(self, findings: list[Any]) -> None:
        self._findings = list(findings)

    def all_findings(self, kind=None, status=None):
        out = self._findings
        if kind is not None:
            out = [f for f in out if f.kind == kind]
        if status is not None:
            out = [f for f in out if f.status == status]
        return out


def _seed(detection: DetectionService, comm_id: str, direction, text: str,
          comm=CommunicationChannel.SMS):
    """Detect one normalized communication, returning the finding report."""
    comm = Communication(
        communication_id=comm_id,
        channel=comm,
        direction=direction,
        sender="counterpart",
        timestamp=PINNED_NOW,
        content_ref=f"cr:{comm_id}",
    )
    detection.register_content(comm.content_ref, text)
    return detection.detect(comm, now=PINNED_NOW)


# -- 1. all eight sections are always present (even empty) -----------------


async def test_all_eight_sections_present_even_when_empty():
    """A fresh (empty) rollup still carries all eight §52 keys, each an empty
    list, with ``empty: True`` and a pinned ``as_of``."""
    state = (await _compose()).model_dump()
    for key in SECTION_KEYS:
        assert key in state, f"missing §52 section {key!r}"
        assert state[key] == [], f"{key} should be empty, got {state[key]!r}"
    assert state["as_of"] == PINNED_NOW
    assert state["empty"] is True
    assert "data" not in state and "meta" not in state, "must be a bare object"


async def test_section_keys_are_exactly_the_eight():
    """The §52 shape is exactly the eight sections (plus ``as_of`` / ``empty``)
    — no more, no fewer."""
    state = (await _compose()).model_dump()
    assert set(state) == set(SECTION_KEYS) | {"as_of", "empty"}


# -- 2. populated from seeded state (each section from its owner) -----------


@pytest.fixture()
def detection():
    svc = DetectionService(attention=AttentionService())
    yield svc
    svc.reset()


@pytest.fixture()
def waiting_on():
    return WaitingOnService(attention=AttentionService())


@pytest.fixture()
def calendar():
    return CalendarService()


async def test_populated_from_seeded_state(detection, waiting_on, calendar):
    """Seed every in-memory plane + fake persistence services; each of the
    eight sections carries exactly its owning row, and no row leaks into a
    section of a different kind."""
    # In-memory planes (real services, deterministic).
    _seed(detection, "c:commit", OUT, "I'll send it tonight.")       # commitment
    _seed(detection, "c:req", IN, "Can you send me the report.")     # request
    _seed(detection, "c:deadline", IN, "due 2026-11-02")            # deadline
    waiting_on.ingest({
        "request_id": "wo:1",
        "person_display_name": "Sam Patel",
        "description": "deployment credentials",
        "requested_at": (PINNED_NOW - timedelta(days=3)).isoformat(),
    })
    calendar.ingest({
        "event_id": "ev:1",
        "title": "Standup",
        "start": PINNED_NOW.replace(hour=9, minute=0).isoformat(),
        "end": PINNED_NOW.replace(hour=9, minute=30).isoformat(),
    })

    services = {
        "goals": _GoalsService([
            {"id": "g:blocked", "title": "Ship beta", "status": "active",
             "deadline": (PINNED_NOW - timedelta(days=1)).isoformat()},
        ]),
        "incidents": _IncidentsService([
            {"id": "i:active", "title": "API 500s", "status": "active"},
        ]),
        "policy": _ApprovalsService([
            {"approval_id": "ap:1", "subject": "send transfer",
             "state": "WAITING_FOR_APPROVAL"},
        ]),
    }
    state = (await _compose(services=services, detection=detection,
                            waiting_on=waiting_on,
                            calendar=calendar)).model_dump()

    assert state["empty"] is False
    # Each section is populated exactly once.
    assert [c["finding_id"] for c in state["open_commitments"]] == [
        "commitment-c:commit"]
    assert [c["finding_id"] for c in state["incoming_requests"]] == [
        "request-c:req"]
    assert [c["finding_id"] for c in state["upcoming_deadlines"]] == [
        "deadline-c:deadline"]
    assert state["upcoming_deadlines"][0]["due"] == "2026-11-02T17:00:00+00:00"
    assert state["waiting_on"] == [
        {"request_id": "wo:1", "person": "Sam Patel",
         "what": "deployment credentials", "days": 3}
    ]
    assert [e["event_id"] for e in state["calendar_today"]] == ["ev:1"]
    assert [g["id"] for g in state["blocked_goals"]] == ["g:blocked"]
    assert [i["id"] for i in state["active_incidents"]] == ["i:active"]
    assert [a["approval_id"] for a in state["pending_approvals"]] == ["ap:1"]

    # No cross-kind leakage: every finding row lives only in its own section.
    for key in ("open_commitments", "incoming_requests", "upcoming_deadlines"):
        prefix = key.split("_")[0] if key != "upcoming_deadlines" else "deadline"
        prefix = {"open_commitments": "commitment",
                  "incoming_requests": "request",
                  "upcoming_deadlines": "deadline"}[key]
        for row in state[key]:
            assert row["finding_id"].startswith(prefix + "-"), row
    # A deadline row must not appear in commitments/requests and vice versa.
    all_ids = {
        row["finding_id"]
        for key in ("open_commitments", "incoming_requests",
                    "upcoming_deadlines")
        for row in state[key]
    }
    assert all_ids == {
        "commitment-c:commit", "request-c:req", "deadline-c:deadline"}


# -- 3. no cross-section contamination -------------------------------------


async def test_mixed_finding_splits_across_sections(detection):
    """A single communication that yields a *request* and a *deadline* places
    each in its own section (never both in one), keyed by distinct finding ids."""
    _seed(detection, "c:mix", IN, "We need the form by Tuesday.")
    state = (await _compose(detection=detection)).model_dump()
    req_ids = [r["finding_id"] for r in state["incoming_requests"]]
    dl_ids = [d["finding_id"] for d in state["upcoming_deadlines"]]
    assert "request-c:mix" in req_ids
    assert "deadline-c:mix" in dl_ids
    # Disjoint: the request is not a deadline and vice versa.
    assert not (set(req_ids) & set(dl_ids))
    # The deadline is upcoming (Tuesday 2026-10-27 is after the anchor).
    assert any(d["due"] == "2026-10-27T17:00:00+00:00" for d in state["upcoming_deadlines"])


# -- 4. empty-state determinism --------------------------------------------


async def test_empty_state_is_deterministic():
    """Two composes over empty services at the same pinned clock are byte
    identical (no wall-clock jitter, no invented data)."""
    a = (await _compose()).model_dump()
    b = (await _compose()).model_dump()
    assert a == b
    assert a["empty"] is True


async def test_same_seed_is_reproducible():
    """The same findings at the same clock produce the same section contents
    (only the clock-injected ``now`` is a nondeterminism source)."""
    def build():
        det = DetectionService(attention=AttentionService())
        _seed(det, "c:commit", OUT, "I'll send it tonight.")
        return compose_operational(None, now=PINNED_NOW, detection=det)

    a = (await build()).model_dump()
    b = (await build()).model_dump()
    assert a["open_commitments"] == b["open_commitments"]
    assert a["as_of"] == b["as_of"] == PINNED_NOW


# -- 5. honest empties for missing owning services --------------------------


async def test_missing_persistence_services_render_honest_empties(detection):
    """Goals / incidents / approvals absent → those sections are empty lists,
    no error, and the in-memory sections are unaffected."""
    _seed(detection, "c:commit", OUT, "I'll send it tonight.")
    state = (await _compose(services={}, detection=detection)).model_dump()
    assert state["blocked_goals"] == []
    assert state["active_incidents"] == []
    assert state["pending_approvals"] == []
    # The in-memory section is still populated.
    assert [c["finding_id"] for c in state["open_commitments"]] == [
        "commitment-c:commit"]


async def test_no_services_at_all_is_fully_empty():
    state = (await _compose()).model_dump()
    assert all(state[k] == [] for k in SECTION_KEYS)
    assert state["empty"] is True


# -- 6. status / filter correctness per section ----------------------------


async def test_blocked_goals_only_active_past_deadline():
    """Only ``active`` goals with a *passed* deadline are blocked: a future
    deadline, a missing deadline, and a non-active goal are all excluded."""
    goals = [
        {"id": "g:past", "status": "active",
         "deadline": (PINNED_NOW - timedelta(days=1)).isoformat()},
        {"id": "g:future", "status": "active",
         "deadline": (PINNED_NOW + timedelta(days=1)).isoformat()},
        {"id": "g:none", "status": "active", "deadline": None},
        {"id": "g:done", "status": "achieved",
         "deadline": (PINNED_NOW - timedelta(days=5)).isoformat()},
        {"id": "g:paused", "status": "paused",
         "deadline": (PINNED_NOW - timedelta(days=5)).isoformat()},
    ]
    state = (await _compose(services={"goals": _GoalsService(goals)})).model_dump()
    assert [g["id"] for g in state["blocked_goals"]] == ["g:past"]


async def test_active_incidents_only_nonterminal():
    """Only ``active`` / ``in-progress`` incidents surface; terminal states are
    excluded."""
    incidents = [
        {"id": "i:active", "status": "active"},
        {"id": "i:progress", "status": "in-progress"},
        {"id": "i:resolved", "status": "resolved"},
        {"id": "i:closed", "status": "closed"},
    ]
    state = (await _compose(services={"incidents": _IncidentsService(incidents)})).model_dump()
    assert sorted(i["id"] for i in state["active_incidents"]) == [
        "i:active", "i:progress"]


async def test_pending_approvals_filters_state():
    """A pending-state approval is kept; a decided one is dropped; an approval
    with no state field is kept (authoritative store)."""
    approvals = [
        {"approval_id": "ap:pending", "state": "WAITING_FOR_APPROVAL"},
        {"approval_id": "ap:approved", "state": "APPROVED"},
        {"approval_id": "ap:nostate"},
    ]
    state = (await _compose(services={"policy": _ApprovalsService(approvals)})).model_dump()
    ids = [a["approval_id"] for a in state["pending_approvals"]]
    assert "ap:pending" in ids
    assert "ap:approved" not in ids
    assert "ap:nostate" in ids


async def test_upcoming_deadlines_exclude_passed():
    """An OPEN deadline is upcoming only while its ``due`` is at/after ``now``;
    a passed due (already swept to ``PASSING``) is excluded."""
    det = _FakeDetection([
        _finding("deadline", "deadline-c:upcoming",
                 status=FindingStatus.OPEN,
                 due=PINNED_NOW + timedelta(days=10)),
        _finding("deadline", "deadline-c:passed",
                 status=FindingStatus.PASSING,
                 due=PINNED_NOW - timedelta(days=1)),
    ])
    state = (await _compose(detection=det)).model_dump()
    assert [d["finding_id"] for d in state["upcoming_deadlines"]] == [
        "deadline-c:upcoming"]


async def test_overdue_commitment_stays_open():
    """A commitment swept to ``OVERDUE`` is still an *open* commitment
    (OPEN or OVERDUE), so it remains in the section; a resolved one does not."""
    det = _FakeDetection([
        _finding("commitment", "commitment-c:overdue",
                 status=FindingStatus.OVERDUE,
                 due=PINNED_NOW - timedelta(days=1)),
        _finding("commitment", "commitment-c:open",
                 status=FindingStatus.OPEN,
                 due=PINNED_NOW + timedelta(days=1)),
        _finding("commitment", "commitment-c:resolved",
                 status=FindingStatus.PASSING,
                 due=PINNED_NOW - timedelta(days=2)),
    ])
    state = (await _compose(detection=det)).model_dump()
    assert [c["finding_id"] for c in state["open_commitments"]] == [
        "commitment-c:overdue", "commitment-c:open"]
    assert state["open_commitments"][0]["status"] == "OVERDUE"


# -- 7. router: GET /api/briefing/operational ------------------------------


@pytest.fixture()
def client():
    """A fresh app (lifespan wired; in-memory plane singletons reset)."""
    reset_detection_service()
    reset_waiting_on_service()
    reset_calendar_service()
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_router_returns_bare_operational_state(client):
    """The route returns the §52 rollup as a *bare* object (no ``data``/``meta``
    envelope), with all eight sections and the in-memory planes populated from
    the live singletons."""
    from backend.communications.detection import get_detection_service
    from backend.waiting_on import get_waiting_on_service
    from backend.calendar import get_calendar_service

    det = get_detection_service()
    _seed(det, "c:commit", OUT, "I'll send it tonight.")
    get_waiting_on_service().ingest({
        "request_id": "wo:1", "person_display_name": "Sam Patel",
        "description": "deployment credentials",
        "requested_at": (PINNED_NOW - timedelta(days=3)).isoformat(),
    })
    get_calendar_service().ingest({
        "event_id": "ev:1", "title": "Standup",
        "start": PINNED_NOW.replace(hour=9, minute=0).isoformat(),
        "end": PINNED_NOW.replace(hour=9, minute=30).isoformat(),
    })

    r = client.get("/api/briefing/operational")
    assert r.status_code == 200
    body = r.json()
    # Bare object — not the §46 ListResponse envelope.
    assert "data" not in body and "meta" not in body
    for key in SECTION_KEYS:
        assert key in body
    assert [c["finding_id"] for c in body["open_commitments"]] == [
        "commitment-c:commit"]
    assert body["waiting_on"][0]["person"] == "Sam Patel"
    assert [e["event_id"] for e in body["calendar_today"]] == ["ev:1"]
    # Persistence-backed sections are honestly empty (no NERVE_DB_URL).
    assert body["blocked_goals"] == []
    assert body["active_incidents"] == []
    assert body["pending_approvals"] == []


def test_router_reads_persistence_backed_services(client):
    """When ``app.state.services`` carries persistence-backed services, the
    route projects them (goals / incidents / approvals)."""
    client.app.state.services = {
        "goals": _GoalsService([
            {"id": "g:blocked", "status": "active",
             "deadline": (PINNED_NOW - timedelta(days=1)).isoformat()},
        ]),
        "incidents": _IncidentsService([
            {"id": "i:active", "status": "active"},
        ]),
        "policy": _ApprovalsService([
            {"approval_id": "ap:1", "state": "WAITING_FOR_APPROVAL"},
        ]),
    }
    body = client.get("/api/briefing/operational").json()
    assert [g["id"] for g in body["blocked_goals"]] == ["g:blocked"]
    assert [i["id"] for i in body["active_incidents"]] == ["i:active"]
    assert [a["approval_id"] for a in body["pending_approvals"]] == ["ap:1"]


def test_router_empty_state_is_honest(client):
    """A fresh backend (no seeded planes, no DB) returns 200 with every §52
    section an empty list and ``empty: True`` — never invented data."""
    body = client.get("/api/briefing/operational").json()
    assert all(body[k] == [] for k in SECTION_KEYS)
    assert body["empty"] is True
    assert body["as_of"] is not None
