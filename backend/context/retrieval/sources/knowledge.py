"""Knowledge for Ask: project notes, entities, claims, and decisions.

Reads the durable knowledge plane through the repository session factory. A row matches when a
search term appears in its text or title, or in the name or aliases of the entity it is about
(a claim's subject entity). With no search terms (a domain-only question) the newest rows are
returned. Project notes are owner-written context; no secret store is read.
"""
from __future__ import annotations

from typing import Any

from backend.context.retrieval.intent import Intent
from backend.context.retrieval.sources._util import aware, make_all
from backend.context.retrieval.types import Record

_SCAN = 500  # newest rows scanned per kind
_BODY_CHARS = 600


def _hit(terms: tuple[str, ...], *texts: object) -> bool:
    """True when no terms are given, or any term appears in any text."""
    if not terms:
        return True
    blob = " ".join(str(t) for t in texts if t).lower()
    return any(t.lower() in blob for t in terms)


def _aliases(entity: Any) -> list[str]:
    return [str(a) for a in (entity.aliases or [])]


async def _fetch(factory: Any) -> tuple[list[Any], list[Any], list[Any], list[tuple[Any, str]]]:
    from sqlalchemy import select

    from backend.persistence.models import (
        Claim,
        Decision,
        Entity,
        ProjectContext,
        ProjectContextNote,
    )

    async with factory() as session:
        entities = list((await session.execute(
            select(Entity).order_by(Entity.created_at.desc()).limit(_SCAN))).scalars())
        claims = list((await session.execute(
            select(Claim).order_by(Claim.created_at.desc()).limit(_SCAN))).scalars())
        decisions = list((await session.execute(
            select(Decision).order_by(Decision.decided_at.desc()).limit(_SCAN))).scalars())
        note_rows = (await session.execute(
            select(ProjectContextNote, ProjectContext.title)
            .join(ProjectContext, ProjectContext.id == ProjectContextNote.project_id)
            .order_by(ProjectContextNote.created_at.desc()).limit(_SCAN))).all()
    return entities, claims, decisions, [(n, str(t)) for n, t in note_rows]


def _specs(
    terms: tuple[str, ...], entities: list[Any], claims: list[Any],
    decisions: list[Any], notes: list[tuple[Any, str]],
) -> list[tuple[Any, ...]]:
    by_id = {str(e.id): e for e in entities}
    specs: list[tuple[Any, ...]] = []
    for n, project in notes:
        if _hit(terms, n.body, project):
            specs.append(("knowledge", f"note:{n.id}", f"Note: {project}",
                          f"{n.author}: {str(n.body)[:_BODY_CHARS]}", n.created_at))
    for e in entities:
        if _hit(terms, e.name, e.description, *_aliases(e)):
            specs.append(("knowledge", f"entity:{e.id}", f"Entity: {e.name}",
                          f"{e.entity_type}. {e.description or ''}", e.created_at))
    for c in claims:
        subject = by_id.get(str(c.subject_entity_id)) if c.subject_entity_id else None
        names = [subject.name, *_aliases(subject)] if subject is not None else []
        if _hit(terms, c.statement, *names):
            about = f" About {subject.name}." if subject is not None else ""
            specs.append(("knowledge", f"claim:{c.id}", f"Claim: {str(c.statement)[:100]}",
                          f"{c.status}, confidence {float(c.confidence):.2f}.{about}",
                          c.created_at))
    claim_subjects = {str(c.id): by_id.get(str(c.subject_entity_id)) for c in claims}
    for d in decisions:
        subject = claim_subjects.get(str(d.claim_id))
        names = [subject.name, *_aliases(subject)] if subject is not None else []
        if _hit(terms, d.statement, d.rationale, d.action, *names):
            specs.append(("knowledge", f"decision:{d.id}", f"Decision: {str(d.statement)[:100]}",
                          f"{d.mechanism}, action {d.action}. {d.rationale or ''}", d.decided_at))
    return specs


async def knowledge(state: Any, intent: Intent, limit: int) -> list[Record] | None:
    registry = getattr(state, "repository_registry", None)
    factory = getattr(registry, "session_factory", None)
    if factory is None or not callable(factory):
        return None
    entities, claims, decisions, notes = await _fetch(factory)
    specs = _specs(intent.search_terms, entities, claims, decisions, notes)
    specs.sort(key=lambda s: str(aware(s[4]) or ""), reverse=True)
    return await make_all(specs[:limit])
