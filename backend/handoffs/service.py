"""Coding handoff queue (NS-ADR-106).

Routing: Menagerie, Mac-only repos (``NERVE_HANDOFF_MAC_REPOS``, default
``apple,menagerie``) and Mac capabilities (swift, xcode, macos, ios, menagerie)
go to the ``mac`` runner; everything else to ``central`` (in-cluster opencode).
Claims are atomic: a row moves queued -> claimed only once.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.persistence.models import CodingHandoff

TARGETS = ("opencode", "menagerie")
RUNNERS = ("central", "mac")
STATUSES = ("queued", "claimed", "done", "failed", "cancelled")
MAC_CAPABILITIES = frozenset({"swift", "xcode", "macos", "ios", "menagerie"})
MAC_REPOS_ENV = "NERVE_HANDOFF_MAC_REPOS"
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
MAX_BRIEF = 20_000
MAX_RESULT = 8000


class HandoffNotFoundError(Exception):
    pass


class HandoffInputError(ValueError):
    pass


class HandoffStateError(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def mac_repos(env: dict[str, str] | None = None) -> frozenset[str]:
    raw = (env if env is not None else os.environ).get(MAC_REPOS_ENV, "apple,menagerie")
    return frozenset(r.strip().lower() for r in raw.split(",") if r.strip())


def route(
    target: str,
    repo: str | None,
    capabilities: Iterable[str] = (),
    env: dict[str, str] | None = None,
) -> str:
    """Pick ``central`` or ``mac`` for a handoff."""
    if target == "menagerie":
        return "mac"
    caps = {c.strip().lower() for c in capabilities}
    if caps & MAC_CAPABILITIES:
        return "mac"
    if repo:
        name = repo.strip().lower().rstrip("/")
        first = name.split("/")[0]
        last = name.split("/")[-1].removesuffix(".git")
        if {first, last, name} & mac_repos(env):
            return "mac"
    return "central"


def to_dict(h: CodingHandoff) -> dict[str, Any]:
    return {
        "id": h.id,
        "target": h.target,
        "runner": h.runner,
        "title": h.title,
        "brief": h.brief,
        "project_slug": h.project_slug,
        "repo": h.repo,
        "requested_by": h.requested_by,
        "status": h.status,
        "push_approved": bool(h.push_approved),
        "claimed_by": h.claimed_by,
        "claimed_at": _iso(h.claimed_at),
        "finished_at": _iso(h.finished_at),
        "result_summary": h.result_summary,
        "error": h.error,
        "session_ref": h.session_ref,
        "created_at": _iso(h.created_at),
        "updated_at": _iso(h.updated_at),
    }


class HandoffService:
    def __init__(self, factory: async_sessionmaker, clock: Any = utcnow) -> None:
        self._factory = factory
        self._clock = clock

    async def enqueue(
        self,
        *,
        target: str,
        title: str,
        brief: str,
        repo: str | None = None,
        project_slug: str | None = None,
        capabilities: Iterable[str] = (),
        requested_by: str = "owner",
        runner: str | None = None,
    ) -> dict[str, Any]:
        if target not in TARGETS:
            raise HandoffInputError("target must be opencode or menagerie")
        title = (title or "").strip()
        brief = (brief or "").strip()
        if not title or len(title) > 200:
            raise HandoffInputError("title must be 1 to 200 characters")
        if not brief or len(brief) > MAX_BRIEF:
            raise HandoffInputError(f"brief must be 1 to {MAX_BRIEF} characters")
        if repo is not None and (not REPO_RE.match(repo) or ".." in repo):
            raise HandoffInputError("repo must be a plain repository name or relative path")
        chosen = runner or route(target, repo, capabilities)
        if chosen not in RUNNERS:
            raise HandoffInputError("runner must be central or mac")
        if target == "menagerie" and chosen != "mac":
            raise HandoffInputError("Menagerie runs only on the Mac")
        now = self._clock()
        row = CodingHandoff(
            target=target,
            runner=chosen,
            title=title,
            brief=brief,
            repo=repo,
            project_slug=project_slug,
            requested_by=requested_by,
            status="queued",
            push_approved=False,
            created_at=now,
            updated_at=now,
        )
        async with self._factory() as session:
            session.add(row)
            await session.commit()
            return to_dict(row)

    async def list(self, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        stmt = select(CodingHandoff).order_by(CodingHandoff.created_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(CodingHandoff.status == status)
        async with self._factory() as session:
            return [to_dict(r) for r in (await session.execute(stmt)).scalars().all()]

    async def get(self, handoff_id: str) -> dict[str, Any]:
        async with self._factory() as session:
            row = await session.get(CodingHandoff, handoff_id)
            if row is None:
                raise HandoffNotFoundError(handoff_id)
            return to_dict(row)

    async def claim_next(self, runner: str, claimer: str) -> dict[str, Any] | None:
        """Atomically claim the oldest queued handoff for ``runner``."""
        async with self._factory() as session:
            candidates = (
                (
                    await session.execute(
                        select(CodingHandoff.id)
                        .where(CodingHandoff.runner == runner, CodingHandoff.status == "queued")
                        .order_by(CodingHandoff.created_at)
                        .limit(5)
                    )
                )
                .scalars()
                .all()
            )
            now = self._clock()
            for hid in candidates:
                result = await session.execute(
                    update(CodingHandoff)
                    .where(CodingHandoff.id == hid, CodingHandoff.status == "queued")
                    .values(
                        status="claimed", claimed_by=claimer[:128], claimed_at=now, updated_at=now
                    )
                )
                if result.rowcount == 1:
                    await session.commit()
                    row = await session.get(CodingHandoff, hid)
                    await session.refresh(row)
                    return to_dict(row)
            await session.commit()
            return None

    async def finish(
        self,
        handoff_id: str,
        *,
        ok: bool,
        summary: str | None = None,
        error: str | None = None,
        claimer: str | None = None,
        session_ref: str | None = None,
    ) -> dict[str, Any]:
        async with self._factory() as session:
            row = await session.get(CodingHandoff, handoff_id)
            if row is None:
                raise HandoffNotFoundError(handoff_id)
            if row.status != "claimed":
                raise HandoffStateError("handoff is not claimed")
            if claimer is not None and row.claimed_by != claimer[:128]:
                raise HandoffNotFoundError(handoff_id)  # do not reveal other claimers
            now = self._clock()
            row.status = "done" if ok else "failed"
            row.result_summary = (summary or "")[:MAX_RESULT] or None
            row.error = (error or "")[:2000] or None
            row.session_ref = (session_ref or row.session_ref or "")[:128] or None
            row.finished_at = now
            row.updated_at = now
            await session.commit()
            return to_dict(row)

    async def approve_push(self, handoff_id: str) -> dict[str, Any]:
        return await self._set(handoff_id, push_approved=True, allowed=("queued",))

    async def cancel(self, handoff_id: str) -> dict[str, Any]:
        return await self._set(handoff_id, status="cancelled", allowed=("queued",))

    async def retry(self, handoff_id: str) -> dict[str, Any]:
        return await self._set(
            handoff_id,
            status="queued",
            claimed_by=None,
            claimed_at=None,
            finished_at=None,
            error=None,
            allowed=("failed", "cancelled", "claimed"),
        )

    async def _set(
        self, handoff_id: str, *, allowed: tuple[str, ...], **values: Any
    ) -> dict[str, Any]:
        async with self._factory() as session:
            row = await session.get(CodingHandoff, handoff_id)
            if row is None:
                raise HandoffNotFoundError(handoff_id)
            if row.status not in allowed:
                raise HandoffStateError(f"not allowed while {row.status}")
            for key, value in values.items():
                setattr(row, key, value)
            row.updated_at = self._clock()
            await session.commit()
            return to_dict(row)


def get_service(app: Any) -> HandoffService | None:
    existing = getattr(app.state, "handoff_service", None)
    if existing is not None:
        return existing
    engine = getattr(app.state, "db_engine", None)
    if engine is None:
        return None
    from backend.persistence.connection import make_session_factory

    service = HandoffService(make_session_factory(engine))
    app.state.handoff_service = service
    return service
