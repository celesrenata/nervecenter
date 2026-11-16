"""Owner-set OpenAI escalation grants (NS-ADR-107).

A grant lets one subject (``agent``, ``project`` or ``handoff``) use the OpenAI
API: ``owner_requested`` means use it for that subject's calls; ``high_stakes``
means stay local first and escalate only after local attempts fail. Only SSO
routes write grants; agents cannot grant themselves.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select

from backend.persistence.models import EscalationGrant

KINDS = ("agent", "project", "handoff")
MODES = ("owner_requested", "high_stakes")


class GrantInputError(ValueError):
    pass


class GrantStore:
    def __init__(self, factory: Any) -> None:
        self._factory = factory

    async def get(self, kind: str, subject_id: str) -> str | None:
        async with self._factory() as session:
            row = (
                (
                    await session.execute(
                        select(EscalationGrant).where(
                            EscalationGrant.subject_kind == kind,
                            EscalationGrant.subject_id == subject_id,
                        )
                    )
                )
                .scalars()
                .first()
            )
            return row.mode if row else None

    async def list(self) -> list[dict[str, Any]]:
        async with self._factory() as session:
            rows = (
                await session.execute(select(EscalationGrant).order_by(EscalationGrant.created_at))
            ).scalars()
            return [
                {"subject_kind": r.subject_kind, "subject_id": r.subject_id, "mode": r.mode}
                for r in rows
            ]

    async def set(self, kind: str, subject_id: str, mode: str) -> dict[str, Any]:
        if kind not in KINDS or mode not in MODES or not subject_id or len(subject_id) > 128:
            raise GrantInputError("invalid grant")
        async with self._factory() as session:
            row = (
                (
                    await session.execute(
                        select(EscalationGrant).where(
                            EscalationGrant.subject_kind == kind,
                            EscalationGrant.subject_id == subject_id,
                        )
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                session.add(EscalationGrant(subject_kind=kind, subject_id=subject_id, mode=mode))
            else:
                row.mode = mode
            await session.commit()
        return {"subject_kind": kind, "subject_id": subject_id, "mode": mode}

    async def remove(self, kind: str, subject_id: str) -> None:
        async with self._factory() as session:
            await session.execute(
                delete(EscalationGrant).where(
                    EscalationGrant.subject_kind == kind, EscalationGrant.subject_id == subject_id
                )
            )
            await session.commit()


def get_grants(app: Any) -> GrantStore | None:
    existing = getattr(app.state, "escalation_grants", None)
    if existing is not None:
        return existing
    engine = getattr(app.state, "db_engine", None)
    if engine is None:
        return None
    from backend.persistence.connection import make_session_factory

    store = GrantStore(make_session_factory(engine))
    app.state.escalation_grants = store
    return store
