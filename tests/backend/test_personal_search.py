"""R110 — cross-channel search + selective memory extraction (AC-R16).

Covers prompt2 §50 (Search) and §51 (Memory Extraction) over the R101/R102
unified communications plane:

- **§50 example 1** — ``"Who asked me for something this week"``:
  person-scoped request search over the last week, grouped per person,
  restricted to genuine requests when the detection plane's request
  findings are supplied (routine incoming chat is excluded).
- **§50 example 2** — ``"Find the voicemail about the prescription"``:
  channel (``voicemail``) + keyword (``prescription``) search through the
  transient-text keyword hook.
- **§51 memory extraction** — durable facts (commitments / requests /
  deadlines from R103 findings; appointments / decisions / project
  requirements / relationships from text markers) carry provenance
  ``source: communication:<id>`` on every fact and preserve the source
  communication's §36 sensitivity; they land as knowledge-graph entities
  (``source_evidence`` provenance) with an idempotent re-land and a
  fact→person ``related_to`` edge.
- **§51 negative** — routine conversation produces zero facts.
- **No-plaintext policy** — search results and extracted facts never
  embed the message body (echoed keywords / short matched fragments only).

House wiring: the communications registry and the detection service are
in-memory singletons (R101/R103), and the knowledge graph is exercised
against a private aiosqlite engine (house test convention, cf.
``tests/backend/test_write_api.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

import backend.api.app  # noqa: F401  (house convention: resolves the
# circular import chain backend.communications.detection → attention before
# detection submodules are imported directly)
from backend.attention.service import AttentionService
from backend.communications.detection.assist import DisabledAssist
from backend.communications.detection.noise import clear_subjects
from backend.communications.detection.service import (
    DetectionService,
    reset_detection_service,
)
from backend.communications.memory import (
    extract_durable_facts,
    land_facts_into_knowledge_graph,
)
from backend.communications.models import CommunicationChannel, SensitivityLabel
from backend.communications.registry import (
    get_communications_service,
    reset_communications_service,
)
from backend.communications.search import search_communications

NOW = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)

#: §50 example queries, verbatim from prompt2.
WHO_ASKED_ME = "Who asked me for something this week"
PRESCRIPTION_VM = "Find the voicemail about the prescription"

TEXTS: dict[str, str] = {
    "cr:m:alice-req": (
        "Can you confirm the design review for Friday? "
        "I need it before you leave."
    ),
    "cr:m:alice-old": "Can you send me the invoice from last week?",
    "cr:m:sarah-vm": (
        "Hi, this is Sarah calling about your prescription refill, "
        "can you call me back by tomorrow?"
    ),
    "cr:m:bob-chit": "hey, how was your weekend? that is cool",
    "cr:m:me-commit": "I'll send the report tomorrow.",
    "cr:m:me-appt": (
        "We decided on the flat-pack shelf unit. We need to book the tax "
        "appointment on 2026-10-24. This project must support offline mode. "
        "Dan is my accountant."
    ),
}


def _seed(detection: DetectionService) -> None:
    """Seed the R101 registry + R103 detection with the fixture corpus."""
    svc = get_communications_service()

    def identity(kind: str, value: str, name: str) -> None:
        svc.ingest_identity({"kind": kind, "value": value, "display_name": name})

    def comm(
        comm_id: str,
        *,
        channel: str,
        direction: str,
        person_id: str,
        peer: str,
        days_ago: int,
        sensitivity: str,
    ) -> None:
        svc.ingest_communication(
            {
                "communication_id": comm_id,
                "channel": channel,
                "direction": direction,
                "sender": peer if direction == "incoming" else "me@local",
                "recipients": (
                    [peer] if direction == "incoming" else ["me@local"]
                ),
                "person_id": person_id,
                "timestamp": NOW - timedelta(days=days_ago),
                "content_ref": f"cr:{comm_id}",
                "sensitivity": sensitivity,
                "source": "test-fixture",
            }
        )
        detection.register_content(f"cr:{comm_id}", TEXTS[f"cr:{comm_id}"])
        detection.detect(
            svc.get_communication(comm_id), now=NOW
        )

    identity("email", "alice@example.com", "Alice")
    identity("voicemail", "+15550001111", "Sarah")
    identity("email", "bob@example.com", "Bob")

    comm(
        "m:alice-req", channel="email", direction="incoming",
        person_id="alice", peer="alice@example.com", days_ago=2,
        sensitivity="WORK",
    )
    comm(
        "m:alice-old", channel="email", direction="incoming",
        person_id="alice", peer="alice@example.com", days_ago=10,
        sensitivity="WORK",
    )
    comm(
        "m:sarah-vm", channel="voicemail", direction="incoming",
        person_id="sarah", peer="+15550001111", days_ago=3,
        sensitivity="PERSONAL",
    )
    comm(
        "m:bob-chit", channel="imessage", direction="incoming",
        person_id="bob", peer="bob@example.com", days_ago=3,
        sensitivity="PERSONAL",
    )
    comm(
        "m:me-commit", channel="sms", direction="outgoing",
        person_id="bob", peer="bob@example.com", days_ago=1,
        sensitivity="WORK",
    )
    comm(
        "m:me-appt", channel="email", direction="outgoing",
        person_id="alice", peer="alice@example.com", days_ago=5,
        sensitivity="FINANCIAL",
    )


@pytest.fixture(autouse=True)
def _clean_state():
    clear_subjects()
    reset_communications_service()
    reset_detection_service()
    yield
    clear_subjects()
    reset_communications_service()
    reset_detection_service()


@pytest.fixture()
def detection() -> DetectionService:
    svc = DetectionService(attention=AttentionService(), assist=DisabledAssist())
    yield svc
    svc.reset()


@pytest.fixture()
def seeded(detection: DetectionService) -> dict[str, Any]:
    _seed(detection)
    service = get_communications_service()
    people = {p.person_id: p.display_name for p in service.list_people()}
    return {
        "detection": detection,
        "service": service,
        "people": people,
        "request_ids": {
            f.communication_id for f in detection.all_findings(kind="request")
        },
    }


def _resolver() -> dict[str, str]:
    return dict(TEXTS)


# ---------------------------------------------------------------------------
#
# §50 — search
#
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_voicemail_prescription_example(seeded: dict[str, Any]) -> None:
    """§50 example: 'Find the voicemail about the prescription'."""
    result = search_communications(
        PRESCRIPTION_VM,
        now=NOW,
        text_resolver=_resolver().get,
    )
    assert [c.communication_id for c in result.communications] == ["m:sarah-vm"]
    assert result.filters.channel is CommunicationChannel.VOICEMAIL
    assert result.filters.keywords == ["prescription"]
    assert result.matched_keywords == ["prescription"]
    assert result.total == 1


@pytest.mark.asyncio
async def test_search_keyword_without_resolver_is_honest_empty(
    seeded: dict[str, Any]
) -> None:
    """Without a text hook, a keyword can't match hidden bodies → 0 rows."""
    result = search_communications(PRESCRIPTION_VM, now=NOW)
    assert result.communications == []
    assert result.total == 0


@pytest.mark.asyncio
async def test_search_who_asked_me_this_week_example(seeded: dict[str, Any]) -> None:
    """§50 example: 'Who asked me for something this week' (request-scoped)."""
    result = search_communications(
        WHO_ASKED_ME,
        now=NOW,
        text_resolver=_resolver().get,
        request_communication_ids=seeded["request_ids"],
    )
    assert result.filters.request_intent is True
    assert result.filters.window_start == NOW - timedelta(days=7)
    group_by_person = {g.person_id: g for g in result.request_groups}
    assert set(group_by_person) == {"alice", "sarah"}
    assert group_by_person["alice"].communication_ids == ["m:alice-req"]
    assert group_by_person["sarah"].communication_ids == ["m:sarah-vm"]
    # The same person's older request (10 days ago) is outside the week.
    assert "m:alice-old" not in group_by_person["alice"].communication_ids
    # Routine incoming chat (Bob) is not a request → no group for him.
    assert "bob" not in group_by_person
    assert group_by_person["alice"].display_name == "Alice"
    # People are exposed (AC-R16: communications, people, conversations).
    assert {g.person_id for g in result.people} == {"alice", "sarah"}


@pytest.mark.asyncio
async def test_search_who_asked_me_fallback_without_request_ids(
    seeded: dict[str, Any]
) -> None:
    """Without detection ids, every in-week incoming record is a candidate
    ask (documented heuristic) — Bob's chit-chat is included."""
    result = search_communications(WHO_ASKED_ME, now=NOW, text_resolver=_resolver().get)
    assert {g.person_id for g in result.request_groups} == {"alice", "bob", "sarah"}


@pytest.mark.asyncio
async def test_search_within_week_window_default(seeded: dict[str, Any]) -> None:
    """Default horizon excludes the 10-day-old record; within_week=False does
    not."""
    scoped = search_communications(
        "Who asked me for something this week",
        person_id="alice",
        now=NOW,
        request_communication_ids={"m:alice-old"},
    )
    assert scoped.request_groups == []  # outside the week

    unbounded = search_communications(
        "Who asked me for something last week",
        person_id="alice",
        within_week=False,
        now=NOW,
        request_communication_ids={"m:alice-old"},
    )
    assert [g.communication_ids for g in unbounded.request_groups] == [["m:alice-old"]]


@pytest.mark.asyncio
async def test_search_last_week_horizon(seeded: dict[str, Any]) -> None:
    """'last week' anchors the window to the 7-day span ending a week ago."""
    result = search_communications(
        "Who asked me for something last week",
        now=NOW,
        request_communication_ids={"m:alice-req", "m:alice-old"},
    )
    assert result.filters.window_start == NOW - 2 * timedelta(days=7)
    by_person = {g.person_id: g.communication_ids for g in result.request_groups}
    assert by_person == {"alice": ["m:alice-old"]}


@pytest.mark.asyncio
async def test_search_explicit_person_and_channel_filters(
    seeded: dict[str, Any]
) -> None:
    """Explicit arguments override / combine with the query parse."""
    result = search_communications(
        "confirm",
        person_id="alice",
        channel=CommunicationChannel.EMAIL,
        now=NOW,
        text_resolver=_resolver().get,
    )
    assert [c.communication_id for c in result.communications] == ["m:alice-req"]

    empty = search_communications(
        "prescription",
        channel=CommunicationChannel.EMAIL,  # overrides the query's voicemail
        now=NOW,
        text_resolver=_resolver().get,
    )
    assert empty.communications == []


@pytest.mark.asyncio
async def test_search_named_person_scopes_query(seeded: dict[str, Any]) -> None:
    """A person named in the query scopes the search by display name."""
    result = search_communications(
        "What did Alice tell me about the review this week",
        now=NOW,
        text_resolver=_resolver().get,
    )
    ids = {c.communication_id for c in result.communications}
    assert "m:alice-req" in ids
    assert "m:sarah-vm" not in ids and "m:bob-chit" not in ids


@pytest.mark.asyncio
async def test_search_empty_results(seeded: dict[str, Any]) -> None:
    """No match → honest empty envelope (not a failure, not invented rows)."""
    result = search_communications(
        "Find the voicemail about the cat litter",
        now=NOW,
        text_resolver=_resolver().get,
    )
    assert result.communications == []
    assert result.request_groups == []
    assert result.total == 0
    assert result.matched_keywords == []


@pytest.mark.asyncio
async def test_search_deterministic_repeat(seeded: dict[str, Any]) -> None:
    """Identical input → byte-identical output (AC determinism)."""
    a = search_communications(PRESCRIPTION_VM, now=NOW, text_resolver=_resolver().get)
    b = search_communications(PRESCRIPTION_VM, now=NOW, text_resolver=_resolver().get)
    assert a.model_dump_json() == b.model_dump_json()


@pytest.mark.asyncio
async def test_search_never_returns_plaintext(seeded: dict[str, Any]) -> None:
    """Results are metadata + content_ref; the body is never embedded."""
    result = search_communications(
        PRESCRIPTION_VM, now=NOW, text_resolver=_resolver().get
    )
    blob = result.model_dump_json()
    assert "prescription refill" not in blob
    assert "call me back by tomorrow" not in blob
    assert "cr:m:sarah-vm" in blob  # the pointer is present
    assert "prescription" in blob  # but only the echoed keyword


# ---------------------------------------------------------------------------
#
# §51 — memory extraction
#
# ---------------------------------------------------------------------------


def _facts_from_durable(detection: DetectionService) -> list:
    """Extract durable facts over the whole corpus: findings (typed) +
    text markers — the memory plane scans every conversation, not only the
    records a finding happened to attach to (m:me-appt carries markers but
    no findings)."""
    service = get_communications_service()
    findings = detection.all_findings()
    communications = service.list_communications()
    people = {p.person_id: p.display_name for p in service.list_people()}
    return list(
        extract_durable_facts(
            findings,
            communications,
            person_names=people,
            text_resolver=_resolver().get,
            now=NOW,
        )
    )


@pytest.mark.asyncio
async def test_memory_facts_carry_provenance_and_sensitivity(
    seeded: dict[str, Any]
) -> None:
    """Every extracted fact carries source: communication:<id> (AC-R16) and
    the source communication's sensitivity, verbatim."""
    facts = _facts_from_durable(seeded["detection"])
    assert facts, "fixture corpus must yield durable facts"
    by_comm = {f.communication_id: f for f in facts}
    for fact in facts:
        assert fact.source == f"communication:{fact.communication_id}"
        assert fact.source.startswith("communication:")
        assert fact.fact_id.startswith(f"fact-{fact.kind}-")
        assert fact.sensitivity is not None
    # Sensitivity preserved per source record (no upgrade/downgrade).
    assert by_comm["m:me-commit"].sensitivity is SensitivityLabel.WORK
    assert by_comm["m:sarah-vm"].sensitivity is SensitivityLabel.PERSONAL
    assert by_comm["m:me-appt"].sensitivity is SensitivityLabel.FINANCIAL
    # Kinds come from findings and text markers.
    kinds = {f.kind for f in facts}
    assert {"commitment", "request", "deadline", "appointment", "decision",
            "project_requirement", "relationship"} <= kinds


@pytest.mark.asyncio
async def test_memory_routine_conversation_creates_no_facts(seeded: dict[str, Any]) -> None:
    """§51 negative: chit-chat with no findings/markers yields zero facts."""
    service = get_communications_service()
    routine = [service.get_communication("m:bob-chit")]
    findings = [
        f
        for f in seeded["detection"].all_findings()
        if f.communication_id == "m:bob-chit"
    ]
    assert findings == []
    facts = extract_durable_facts(
        findings, routine, text_resolver=_resolver().get, now=NOW
    )
    assert facts == ()


@pytest.mark.asyncio
async def test_memory_durable_text_markers(seeded: dict[str, Any]) -> None:
    """Appointments / decisions / requirements / relationships extract from
    the transient text, with parsed dues and preserved sensitivity."""
    service = get_communications_service()
    comm = service.get_communication("m:me-appt")
    facts = [
        f
        for f in extract_durable_facts(
            [], [comm], text_resolver=_resolver().get, now=NOW
        )
    ]
    by_kind = {f.kind: f for f in facts}
    assert set(by_kind) == {"appointment", "decision", "project_requirement",
                            "relationship"}
    appt = by_kind["appointment"]
    assert appt.due == datetime(2026, 10, 24, 17, 0, tzinfo=UTC)
    assert appt.sensitivity is SensitivityLabel.FINANCIAL
    assert appt.source == "communication:m:me-appt"
    for fact in facts:
        assert fact.communication_id == "m:me-appt"


@pytest.mark.asyncio
async def test_memory_extraction_deterministic(seeded: dict[str, Any]) -> None:
    """Same inputs → same facts, including ids."""
    a = _facts_from_durable(seeded["detection"])
    b = _facts_from_durable(seeded["detection"])
    assert [f.model_dump(mode="json") for f in a] == [f.model_dump(mode="json") for f in b]


@pytest.mark.asyncio
async def test_memory_facts_never_embed_plaintext(seeded: dict[str, Any]) -> None:
    """Facts carry short fragments, never the message body (§7 / R107)."""
    facts = _facts_from_durable(seeded["detection"])
    blob = str([f.model_dump(mode="json") for f in facts])
    assert TEXTS["cr:m:sarah-vm"] not in blob
    assert TEXTS["cr:m:me-appt"] not in blob


# ---------------------------------------------------------------------------
#
# §51 — landing into the knowledge graph
#
# ---------------------------------------------------------------------------


class _Fact(BaseModel):
    pass


@pytest.fixture()
async def graph():
    from backend.knowledge.entity import EntityService
    from backend.knowledge.relationship import RelationshipService
    from backend.persistence.models import (
        Entity as EntityORM,
        Relationship as RelationshipORM,
    )

    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(EntityORM.__table__.create)
        await conn.run_sync(RelationshipORM.__table__.create)
    yield {
        "entities": EntityService(engine),
        "relationships": RelationshipService(engine),
    }
    await engine.dispose()


def _single_fact(detection: DetectionService) -> Any:
    """One fact (the outgoing commitment) with its source record."""
    service = get_communications_service()
    finding = next(
        f
        for f in detection.all_findings(kind="commitment")
        if f.communication_id == "m:me-commit"
    )
    comm = service.get_communication("m:me-commit")
    return extract_durable_facts(
        [finding], [comm], text_resolver=_resolver().get, now=NOW
    )[0]


@pytest.mark.asyncio
async def test_memory_landing_provenance_and_idempotency(
    seeded: dict[str, Any], graph: dict[str, Any]
) -> None:
    """Facts land as entities with communication:<id> provenance; re-landing
    updates in place (no duplicates)."""
    fact = _single_fact(seeded["detection"])
    entities = await land_facts_into_knowledge_graph(
        [fact], graph["entities"], graph["relationships"]
    )
    assert len(entities) == 1
    landed = entities[0]
    assert landed.source_evidence == ["communication:m:me-commit"]
    assert landed.attributes["sensitivity"] == "WORK"
    assert landed.attributes["provenance"] == "communication:m:me-commit"
    assert landed.entity_type == "resource"

    # Re-land the same fact (same deterministic id) → no duplicate row.
    await land_facts_into_knowledge_graph(
        [fact], graph["entities"], graph["relationships"]
    )
    all_entities = await graph["entities"].list_entities(limit=100)
    assert len(all_entities) == 2  # fact entity + person-alice/bob target
    assert sum(
        1
        for e in all_entities
        if "communication:m:me-commit" in e.source_evidence
    ) == 1
    # The fact→person relationship edge exists and carries provenance.
    edges = await graph["relationships"].active_at(
        fact.fact_id, NOW, direction="outgoing"
    )
    assert len(edges) == 1
    assert edges[0].relation_type == "related_to"
    assert edges[0].evidence == ["communication:m:me-commit"]
    assert edges[0].properties["sensitivity"] == "WORK"
    # No plaintext leaked into the landed entity.
    assert TEXTS["cr:m:me-commit"] not in str(landed.model_dump(mode="json"))
