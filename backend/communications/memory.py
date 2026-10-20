"""R110 — selective memory extraction into the knowledge graph (prompt2 §51, AC-R16).

"Not every message becomes permanent knowledge." (§51)

This plane turns *durable* information — appointments, decisions,
commitments, requests, deadlines, project requirements, and relationships
between entities — into knowledge-graph entities, and deliberately does
**not** create entities for routine conversation:

- :func:`extract_durable_facts` is a *pure, deterministic* function over the
  R103 typed :class:`~backend.communications.detection.findings.Finding`s
  (commitment / request / deadline) plus optional transient text (the same
  house-style content resolver detection uses). It yields
  :class:`MemoryFact` records that carry:

  - **provenance** — ``source == "communication:<id>"`` on every fact
    (AC-R16); the fact id is deterministic (``fact-<kind>-<comm_id>-<seq>``);
  - **preserved sensitivity** — the source communication's §36
    :class:`~backend.communications.models.SensitivityLabel` is copied
    verbatim onto the fact;
  - **no plaintext leakage** — the fact carries a summary / matched
    expression and metadata, never the message body (§7 / R107).

- :func:`land_facts_into_knowledge_graph` is the async seam that writes
  each fact as a :class:`schemas.entities.Entity` (``entity_type``
  ``"resource"``, structured ``attributes``, ``source_evidence`` = the
  ``communication:<id>`` provenance — so
  :meth:`EntityService.find_by_evidence` answers "where did this fact
  come from?"), and — when a :class:`RelationshipService` is supplied —
  links the fact to the canonical ``person`` entity so the
  fact→person *relationship* is itself a temporal edge in the graph.

Idempotent: fact ids are deterministic, so re-landing an already-landed
fact updates in place instead of duplicating.

Routine conversation — chit-chat with no findings, no appointment /
decision / requirement markers — yields zero facts (negative test, §51
"Routine conversation should not pollute the knowledge graph").
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Callable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from backend.communications.detection.dates import parse_due_expression
from backend.communications.detection.findings import Finding
from backend.communications.models import (
    Communication,
    SensitivityLabel,
)

#: Transient text accessor for the body behind a ``content_ref`` (same
#: shape as the R103 detection content resolver — never persisted).
TextResolver = Callable[[str], str | None]

#: Durable fact kinds (prompt2 §51 list + detected findings).
MemoryFactKind = Literal[
    "appointment",
    "decision",
    "commitment",
    "request",
    "deadline",
    "project_requirement",
    "relationship",
]

_APPOINTMENT_RE = re.compile(
    r"\b(?:appointment|meeting|booked|book|scheduled|schedule|checkup|check-up|dentist|physio|therapy session|dinner|lunch|call at|fly to)\b",
    re.IGNORECASE,
)
_DECISION_RE = re.compile(
    r"\b(?:decided|decision:|let's go with|lets go with|agreed to|we agreed|settled on)\b",
    re.IGNORECASE,
)
_REQUIREMENT_RE = re.compile(
    r"\b(?:requirement|requirements|we need to|must support|needs to support|it must|hard requirement)\b",
    re.IGNORECASE,
)
_RELATIONSHIP_RE = re.compile(
    r"\b(?:is my|works for|my (?:boss|manager|doctor|dentist|physiotherapist|accountant|landlord|supplier|client|contractor|coach|mechanic|lawyer|electrician))\b",
    re.IGNORECASE,
)


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _short(text: str, limit: int = 60) -> str:
    text = " ".join(str(text or "").split())
    return text[:limit] or "n/a"


class MemoryFact(BaseModel):
    """One durable fact extracted from a communication (§51).

    ``source`` is the provenance pointer (``communication:<id>``);
    ``sensitivity`` is the source communication's §36 label, preserved
    verbatim so the graph never *upgrades* a fact's sensitivity.
    """

    model_config = ConfigDict(frozen=True)

    fact_id: str
    kind: str
    name: str
    summary: str
    communication_id: str
    source: str = Field(description='Provenance: "communication:<id>" (AC-R16).')
    sensitivity: SensitivityLabel
    person_id: str | None = None
    display_name: str | None = None
    channel: str | None = None
    due: datetime | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    why: list[str] = Field(default_factory=list)


def _finding_fact(
    seq: int, finding: Finding, communication: Communication | None,
) -> MemoryFact:
    """Map a R103 typed finding to a durable fact (provenance + sensitivity)."""
    comm_id = finding.communication_id
    sensitivity = (
        communication.sensitivity
        if communication is not None
        else SensitivityLabel.PERSONAL
    )
    short = _short(finding.matched or (finding.why[0] if finding.why else comm_id))
    name = f"{finding.kind} on {comm_id} — {short}"
    attributes: dict[str, Any] = {
        "memory_kind": finding.kind,
        "content_ref": finding.content_ref,
        "confidence": finding.confidence,
        "model_used": finding.model_used,
        "why": list(finding.why),
    }
    if finding.due is not None:
        attributes["due"] = _utc(finding.due).isoformat()
    if communication is not None:
        attributes["sensitivity"] = str(sensitivity)
        attributes["channel"] = str(communication.channel)
    return MemoryFact(
        fact_id=f"fact-{finding.kind}-{comm_id}-{seq:02d}",
        kind=finding.kind,
        name=name,
        summary=f"{finding.kind.title()} detected on {comm_id}: {short}",
        communication_id=comm_id,
        source=f"communication:{comm_id}",
        sensitivity=sensitivity,
        person_id=communication.person_id if communication is not None else None,
        due=_utc(finding.due) if finding.due is not None else None,
        attributes=attributes,
        why=list(finding.why),
    )


def _text_fact(
    seq: int, kind: str, *, communication: Communication, summary: str,
    due: datetime | None, why: str,
) -> MemoryFact:
    comm_id = communication.communication_id
    return MemoryFact(
        fact_id=f"fact-{kind}-{comm_id}-{seq:02d}",
        kind=kind,
        name=f"{kind} on {comm_id} — {_short(why or summary)}",
        summary=summary,
        communication_id=comm_id,
        source=f"communication:{comm_id}",
        sensitivity=communication.sensitivity,
        person_id=communication.person_id,
        channel=str(communication.channel),
        due=_utc(due) if due is not None else None,
        attributes={
            "memory_kind": kind,
            "content_ref": communication.content_ref,
            "sensitivity": str(communication.sensitivity),
            "channel": str(communication.channel),
            "why": [why],
        },
        why=[why],
    )


def extract_durable_facts(
    findings: Sequence[Finding],
    communications: Sequence[Communication],
    *,
    person_names: Mapping[str, str] | None = None,
    text_resolver: TextResolver | None = None,
    now: datetime | None = None,
) -> tuple[MemoryFact, ...]:
    """Deterministically extract durable facts from detected findings + texts.

    Parameters
    ----------
    findings:
        R103 typed findings (commitment / request / deadline) from
        :class:`~backend.communications.detection.service.DetectionService`.
        Every finding becomes a durable fact of the same kind.
    communications:
        The normalized :class:`Communication` records the findings came
        from (metadata: sensitivity, person, channel, timestamp). Text
        markers (appointments / decisions / requirements) are only
        evaluated when a ``text_resolver`` is provided — metadata alone
        never manufactures a fact.
    person_names:
        Optional ``person_id → display_name`` map (used in summaries).
    text_resolver:
        Optional transient ``content_ref → text`` accessor.
    now:
        Deterministic clock anchor for due-date parsing (UTC).

    Returns
    -------
    A tuple of :class:`MemoryFact` — empty for routine conversation (the
    §51 negative case). Deterministic: same inputs → same facts.
    """
    person_names = dict(person_names or {})
    now = _utc(now) if now is not None else datetime.now(UTC)
    comms_by_id = {c.communication_id: c for c in communications}

    facts: list[MemoryFact] = []
    per_comm_seq: dict[str, int] = {}

    def _next_seq(comm_id: str) -> int:
        seq = per_comm_seq.get(comm_id, 0)
        per_comm_seq[comm_id] = seq + 1
        return seq

    # 1) Findings are already durable (R103 commitment / request / deadline).
    for finding in sorted(findings, key=lambda f: (f.communication_id, f.kind, f.finding_id)):
        comm = comms_by_id.get(finding.communication_id)
        fact = _finding_fact(_next_seq(finding.communication_id), finding, comm)
        if fact.person_id and person_names.get(fact.person_id):
            fact = fact.model_copy(update={"display_name": person_names[fact.person_id]})
        facts.append(fact)

    # 2) Text-marker facts (appointments / decisions / requirements) — only
    #    when transient text is available; metadata alone never creates one.
    if text_resolver is not None:
        for communication in sorted(
            communications, key=lambda c: (c.timestamp, c.communication_id)
        ):
            text = str(text_resolver(communication.content_ref) or "")
            if not text.strip():
                continue
            due_info = parse_due_expression(text, now=now)
            due_dt = _utc(due_info.due) if due_info is not None else None

            appt = _APPOINTMENT_RE.search(text)
            if appt:
                where = _short(appt.group(0), 40)
                facts.append(
                    _text_fact(
                        _next_seq(communication.communication_id),
                        "appointment",
                        communication=communication,
                        summary=f"Appointment mentioned on {communication.communication_id}: {where}",
                        due=due_dt,
                        why=f"appointment marker: {where}" + (f" due {due_dt.isoformat()}" if due_dt else ""),
                    )
                )
            if _DECISION_RE.search(text):
                facts.append(
                    _text_fact(
                        _next_seq(communication.communication_id),
                        "decision",
                        communication=communication,
                        summary=f"Decision recorded on {communication.communication_id}",
                        due=None,
                        why="decision marker in communication text",
                    )
                )
            if _REQUIREMENT_RE.search(text):
                facts.append(
                    _text_fact(
                        _next_seq(communication.communication_id),
                        "project_requirement",
                        communication=communication,
                        summary=f"Project requirement noted on {communication.communication_id}",
                        due=None,
                        why="requirement marker in communication text",
                    )
                )
            rel = _RELATIONSHIP_RE.search(text)
            if rel:
                facts.append(
                    _text_fact(
                        _next_seq(communication.communication_id),
                        "relationship",
                        communication=communication,
                        summary=f"Relationship noted on {communication.communication_id}: {_short(rel.group(0), 40)}",
                        due=None,
                        why=f"relationship marker: {_short(rel.group(0), 40)}",
                    )
                )

    return tuple(sorted(facts, key=lambda f: (f.kind, f.communication_id, f.fact_id)))


# ---------------------------------------------------------------------------
#
# Landing into the knowledge graph (EntityService / RelationshipService)
#
# ---------------------------------------------------------------------------


def _fact_entity_payload(fact: MemoryFact) -> dict[str, Any]:
    """Deterministic entity payload for a durable fact (AC-R16 provenance)."""
    attributes = dict(fact.attributes)
    attributes.setdefault("sensitivity", str(fact.sensitivity))
    attributes["provenance"] = fact.source
    attributes["memory_kind"] = fact.kind
    return {
        "id": fact.fact_id,  # deterministic → idempotent re-landing
        "entity_type": "resource",
        "name": fact.name,
        "description": fact.summary,
        "attributes": attributes,
        "confidence": float(attributes.get("confidence", 1.0)),
        "source_evidence": [fact.source],  # "communication:<id>" provenance
    }


async def land_facts_into_knowledge_graph(
    facts: Sequence[MemoryFact],
    entity_service: Any,
    relationship_service: Any | None = None,
    *,
    person_display_names: Mapping[str, str] | None = None,
) -> list[Any]:
    """Write durable facts as knowledge-graph entities (AC-R16).

    - Each fact becomes an ``Entity`` (``entity_type="resource"``) whose
      ``source_evidence`` carries the ``communication:<id>`` provenance and
      whose ``attributes`` preserve the communication's sensitivity.
    - Re-landing an existing fact id updates in place (idempotent).
    - When ``relationship_service`` is given, each fact with a
      ``person_id`` is linked to a canonical ``person`` entity
      (``person-<id>``) with a temporal ``related_to`` edge carrying the
      same provenance — the fact→person *relationship* is itself a
      durable graph edge. The edge is only created once (idempotent).

    Returns the landed :class:`schemas.entities.Entity` records.
    """
    from backend.knowledge.entity import EntityConflictError
    from backend.knowledge.relationship import UnknownEntityError

    person_display_names = dict(person_display_names or {})
    landed: list[Any] = []
    for fact in sorted(facts, key=lambda f: f.fact_id):
        payload = _fact_entity_payload(fact)
        try:
            entity = await entity_service.create(payload)
        except EntityConflictError:
            # Idempotent re-landing: update the existing row in place.
            entity = await entity_service.update(
                fact.fact_id,
                {
                    "description": payload["description"],
                    "attributes": payload["attributes"],
                    "source_evidence": payload["source_evidence"],
                },
            )
        landed.append(entity)

        if relationship_service is None or not fact.person_id:
            continue
        person_id = f"person-{fact.person_id}"
        properties = {
            "memory_kind": fact.kind,
            "sensitivity": str(fact.sensitivity),
        }
        # Idempotency: only create the edge once per (fact, person).
        existing = await relationship_service.between(
            fact.fact_id, person_id, relation_type="related_to"
        )
        if existing:
            continue
        # Ensure the person entity exists before linking.
        try:
            await entity_service.get(person_id)
        except Exception:
            try:
                await entity_service.create(
                    {
                        "id": person_id,
                        "entity_type": "person",
                        "name": person_display_names.get(fact.person_id)
                        or fact.person_id,
                        "attributes": {"sensitivity": str(fact.sensitivity)},
                    }
                )
            except EntityConflictError:
                pass
        try:
            await relationship_service.create(
                fact.fact_id,
                person_id,
                "related_to",
                properties=properties,
                evidence=[fact.source],
            )
        except UnknownEntityError:
            # Person not present and create raced — skip the edge; the fact
            # entity still lands (provenance is preserved either way).
            pass
    return landed


__all__ = [
    "MemoryFact",
    "TextResolver",
    "extract_durable_facts",
    "land_facts_into_knowledge_graph",
]
