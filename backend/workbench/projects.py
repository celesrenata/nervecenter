"""Workbench project catalogue (Requirements 1.6, 1.7, 2.x, 3.5, 7.13, 10.2).

``ProjectService`` records projects in PostgreSQL and manages their directories
through the per-kind ``StoreClient``. Archive, list-unregistered and import never
modify files. ``coding_handoffs`` is only read, never written (Req 10.2).
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.persistence.models import CodingHandoff
from backend.persistence.models.workbench import (
    WorkbenchIde,
    WorkbenchProject,
    WorkbenchSession,
)

from .store import StoreClient, StoreRejected, StoreUnavailable
from .validation import (
    KINDS,
    validate_git_url,
    validate_kind,
    validate_slug,
    validate_title,
)

logger = logging.getLogger("nervecenter.workbench.projects")

RECENT_HANDOFFS = 10


class ProjectValidationError(ValueError):
    """Input failed validation; nothing was created or changed."""


class ProjectConflictError(Exception):
    """A project with the same (slug, kind) already exists."""


class ProjectNotFoundError(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def project_to_dict(p: WorkbenchProject) -> dict[str, Any]:
    return {
        "id": p.id,
        "slug": p.slug,
        "kind": p.kind,
        "title": p.title,
        "created_at": _iso(p.created_at),
        "last_activity_at": _iso(p.last_activity_at),
        "size_bytes": p.size_bytes,
        "archived": p.archived,
        "push_approved": p.push_approved,
    }


def session_to_dict(s: WorkbenchSession) -> dict[str, Any]:
    return {
        "id": s.id,
        "title": s.title,
        "status": s.status,
        "updated_at": _iso(s.updated_at),
        "model": s.model,
        "small_model": s.small_model,
    }


def handoff_to_dict(h: CodingHandoff) -> dict[str, Any]:
    return {
        "id": str(h.id),
        "title": h.title,
        "status": h.status,
        "target": h.target,
        "runner": h.runner,
        "created_at": _iso(h.created_at),
        "updated_at": _iso(h.updated_at),
    }


class ProjectService:
    def __init__(
        self,
        factory: async_sessionmaker,
        stores: Mapping[str, StoreClient],
        clock: Any = utcnow,
    ) -> None:
        self._factory = factory
        self._stores = stores
        self._clock = clock

    def _store(self, kind: str) -> StoreClient:
        if not validate_kind(kind):
            raise ProjectValidationError(f"kind must be one of {KINDS}")
        return self._stores[kind]

    async def _find(self, session: Any, kind: str, slug: str) -> WorkbenchProject | None:
        return (
            await session.execute(
                select(WorkbenchProject).where(
                    WorkbenchProject.slug == slug, WorkbenchProject.kind == kind
                )
            )
        ).scalar_one_or_none()

    async def _require(self, session: Any, kind: str, slug: str) -> WorkbenchProject:
        project = await self._find(session, kind, slug)
        if project is None:
            raise ProjectNotFoundError(f"{kind}/{slug}")
        return project

    async def _record(
        self, *, slug: str, kind: str, title: str, size_bytes: int = 0
    ) -> dict[str, Any]:
        now = self._clock()
        project = WorkbenchProject(
            slug=slug,
            kind=kind,
            title=title,
            created_at=now,
            last_activity_at=now,
            size_bytes=size_bytes,
            archived=False,
            push_approved=False,
        )
        async with self._factory() as session:
            session.add(project)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ProjectConflictError(f"{kind}/{slug} already exists") from exc
            await session.refresh(project)
            return project_to_dict(project)

    async def create(
        self, *, slug: str, title: str, kind: str, git_url: str | None = None
    ) -> dict[str, Any]:
        """Create an empty project or clone one. Validates before touching storage."""
        if not validate_slug(slug):
            raise ProjectValidationError("slug must match ^[a-z0-9][a-z0-9-]{0,62}$")
        if not validate_title(title):
            raise ProjectValidationError("title must be 1 to 120 characters")
        if not validate_kind(kind):
            raise ProjectValidationError(f"kind must be one of {KINDS}")
        if git_url is not None and not validate_git_url(git_url):
            raise ProjectValidationError("git_url must be an https or ssh URL")
        store = self._store(kind)

        # Check for a duplicate before the store call so an existing tree is untouched.
        async with self._factory() as session:
            if await self._find(session, kind, slug) is not None:
                raise ProjectConflictError(f"{kind}/{slug} already exists")

        try:
            if git_url is None:
                await store.mkproject(slug)
            else:
                await store.clone(slug, git_url)
        except StoreRejected as exc:
            if exc.status == 409:
                raise ProjectConflictError(f"{kind}/{slug} already exists") from exc
            raise

        try:
            return await self._record(slug=slug, kind=kind, title=title)
        except ProjectConflictError:
            # A concurrent create won the race and owns the directory; leave it.
            raise
        except Exception:
            # No record was written; remove the directory this call created.
            try:
                await store.rmproject(slug, slug)
            except (StoreUnavailable, StoreRejected):
                logger.warning("cleanup of %s/%s failed after record error", kind, slug)
            raise

    async def list(self) -> list[dict[str, Any]]:
        """Every project, archived included, newest activity first."""
        async with self._factory() as session:
            rows = (
                (
                    await session.execute(
                        select(WorkbenchProject).order_by(
                            WorkbenchProject.last_activity_at.desc()
                        )
                    )
                )
                .scalars()
                .all()
            )
            return [project_to_dict(p) for p in rows]

    async def get(self, kind: str, slug: str) -> dict[str, Any]:
        """Project with its sessions and the 10 newest handoffs (read-only query)."""
        async with self._factory() as session:
            project = await self._require(session, kind, slug)
            sessions = (
                (
                    await session.execute(
                        select(WorkbenchSession)
                        .where(WorkbenchSession.project_id == project.id)
                        .order_by(WorkbenchSession.updated_at.desc().nulls_last())
                    )
                )
                .scalars()
                .all()
            )
            handoffs = (
                (
                    await session.execute(
                        select(CodingHandoff)
                        .where(
                            CodingHandoff.project_slug == slug,
                            CodingHandoff.target == kind,
                        )
                        .order_by(CodingHandoff.created_at.desc())
                        .limit(RECENT_HANDOFFS)
                    )
                )
                .scalars()
                .all()
            )
            out = project_to_dict(project)
            out["sessions"] = [session_to_dict(s) for s in sessions]
            out["handoffs"] = [handoff_to_dict(h) for h in handoffs]
            return out

    async def archive(self, kind: str, slug: str) -> dict[str, Any]:
        """Mark archived. Files are not touched."""
        async with self._factory() as session:
            project = await self._require(session, kind, slug)
            project.archived = True
            await session.commit()
            await session.refresh(project)
            return project_to_dict(project)

    async def delete(self, kind: str, slug: str, confirm: str | None) -> None:
        """Remove the directory and record only when ``confirm`` equals the slug."""
        if not isinstance(confirm, str) or confirm != slug:
            raise ProjectValidationError("confirm must equal the project slug")
        store = self._store(kind)
        async with self._factory() as session:
            project = await self._require(session, kind, slug)
            project_id = project.id
        # Remove files first: if the store fails, the record stays and nothing is lost.
        await store.rmproject(slug, confirm)
        async with self._factory() as session:
            await session.execute(
                delete(WorkbenchSession).where(WorkbenchSession.project_id == project_id)
            )
            await session.execute(
                delete(WorkbenchIde).where(WorkbenchIde.project_id == project_id)
            )
            await session.execute(
                delete(WorkbenchProject).where(WorkbenchProject.id == project_id)
            )
            await session.commit()

    async def list_unregistered(self) -> list[dict[str, str]]:
        """Directories in each store with no project record. Read-only."""
        async with self._factory() as session:
            rows = (
                await session.execute(select(WorkbenchProject.kind, WorkbenchProject.slug))
            ).all()
        known = {(k, s) for k, s in rows}
        out: list[dict[str, str]] = []
        for kind in KINDS:
            store = self._stores.get(kind)
            if store is None:
                continue
            for name in sorted(await store.dirs()):
                if (kind, name) not in known:
                    out.append({"kind": kind, "slug": name})
        return out

    async def import_dir(
        self, *, kind: str, slug: str, title: str | None = None
    ) -> dict[str, Any]:
        """Record an existing directory as a project without changing any file."""
        if not validate_slug(slug):
            raise ProjectValidationError("slug must match ^[a-z0-9][a-z0-9-]{0,62}$")
        title = slug if title is None else title
        if not validate_title(title):
            raise ProjectValidationError("title must be 1 to 120 characters")
        store = self._store(kind)
        async with self._factory() as session:
            if await self._find(session, kind, slug) is not None:
                raise ProjectConflictError(f"{kind}/{slug} already exists")
        if slug not in await store.dirs():
            raise ProjectNotFoundError(f"no directory {kind}/{slug}")
        return await self._record(slug=slug, kind=kind, title=title)

    async def set_push_approved(self, kind: str, slug: str, approved: bool) -> dict[str, Any]:
        """Owner-set flag that gates `git push` for sessions and the IDE (Req 7.13)."""
        async with self._factory() as session:
            project = await self._require(session, kind, slug)
            await session.execute(
                update(WorkbenchProject)
                .where(WorkbenchProject.id == project.id)
                .values(push_approved=bool(approved))
            )
            await session.commit()
            await session.refresh(project)
            return project_to_dict(project)


__all__ = [
    "ProjectConflictError",
    "ProjectNotFoundError",
    "ProjectService",
    "ProjectValidationError",
]
