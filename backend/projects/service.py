"""Project context store service (NS-ADR-104). NerveCenter is the source of truth."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.persistence.models import ProjectContext, ProjectContextNote

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
AUTHOR_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
MAX_NOTE_CHARS = 8000
MAX_SUMMARY_CHARS = 20_000
#: Bounds for the text injected into a model prompt.
CONTEXT_NOTES = 20
CONTEXT_CHARS = 8000


class ProjectNotFoundError(Exception):
    """No project has that slug."""


class ProjectInputError(ValueError):
    """Invalid slug, author or text."""


class ProjectExistsError(Exception):
    """A project with that slug already exists."""


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def project_to_dict(p: ProjectContext, note_count: int | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "slug": p.slug,
        "title": p.title,
        "summary": p.summary,
        "created_at": _iso(p.created_at),
        "updated_at": _iso(p.updated_at),
    }
    if note_count is not None:
        out["note_count"] = note_count
    return out


def note_to_dict(n: ProjectContextNote) -> dict[str, Any]:
    return {"id": n.id, "author": n.author, "body": n.body, "created_at": _iso(n.created_at)}


def check_slug(slug: str) -> str:
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise ProjectInputError("slug must be lowercase letters, digits and dashes (max 64)")
    return slug


class ProjectService:
    def __init__(self, factory: async_sessionmaker, clock: Any = None) -> None:
        self._factory = factory
        self._clock = clock or (lambda: datetime.now(UTC))

    async def _get(self, session: Any, slug: str) -> ProjectContext:
        check_slug(slug)
        row = (
            (await session.execute(select(ProjectContext).where(ProjectContext.slug == slug)))
            .scalars()
            .first()
        )
        if row is None:
            raise ProjectNotFoundError(slug)
        return row

    async def create(self, slug: str, title: str, summary: str = "") -> dict[str, Any]:
        check_slug(slug)
        title = (title or "").strip()
        if not title or len(title) > 200:
            raise ProjectInputError("title must be 1 to 200 characters")
        if len(summary) > MAX_SUMMARY_CHARS:
            raise ProjectInputError("summary is too long")
        async with self._factory() as session:
            existing = (
                (await session.execute(select(ProjectContext).where(ProjectContext.slug == slug)))
                .scalars()
                .first()
            )
            if existing is not None:
                raise ProjectExistsError(slug)
            now = self._clock()
            row = ProjectContext(
                slug=slug, title=title, summary=summary, created_at=now, updated_at=now
            )
            session.add(row)
            await session.commit()
            return project_to_dict(row, 0)

    async def list_projects(self) -> list[dict[str, Any]]:
        async with self._factory() as session:
            rows = (
                (await session.execute(select(ProjectContext).order_by(ProjectContext.slug)))
                .scalars()
                .all()
            )
            counts: dict[str, int] = {}
            notes = (await session.execute(select(ProjectContextNote.project_id))).scalars().all()
            for pid in notes:
                counts[pid] = counts.get(pid, 0) + 1
            return [project_to_dict(r, counts.get(r.id, 0)) for r in rows]

    async def get(self, slug: str, limit: int = 200) -> dict[str, Any]:
        async with self._factory() as session:
            row = await self._get(session, slug)
            notes = (
                (
                    await session.execute(
                        select(ProjectContextNote)
                        .where(ProjectContextNote.project_id == row.id)
                        .order_by(ProjectContextNote.created_at.desc(), ProjectContextNote.id)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            out = project_to_dict(row, len(notes))
            out["notes"] = [note_to_dict(n) for n in reversed(notes)]
            return out

    async def update(
        self, slug: str, title: str | None = None, summary: str | None = None
    ) -> dict[str, Any]:
        async with self._factory() as session:
            row = await self._get(session, slug)
            if title is not None:
                if not title.strip() or len(title) > 200:
                    raise ProjectInputError("title must be 1 to 200 characters")
                row.title = title.strip()
            if summary is not None:
                if len(summary) > MAX_SUMMARY_CHARS:
                    raise ProjectInputError("summary is too long")
                row.summary = summary
            row.updated_at = self._clock()
            await session.commit()
            return project_to_dict(row)

    async def add_note(self, slug: str, author: str, body: str) -> dict[str, Any]:
        if not isinstance(author, str) or not AUTHOR_RE.match(author):
            raise ProjectInputError("author must be a short lowercase client name")
        body = (body or "").strip()
        if not body or len(body) > MAX_NOTE_CHARS:
            raise ProjectInputError(f"note must be 1 to {MAX_NOTE_CHARS} characters")
        async with self._factory() as session:
            row = await self._get(session, slug)
            now = self._clock()
            note = ProjectContextNote(project_id=row.id, author=author, body=body, created_at=now)
            session.add(note)
            row.updated_at = now
            await session.commit()
            return note_to_dict(note)

    async def context_text(self, slug: str) -> str:
        """Bounded plain text for a model prompt: title, summary, newest notes last."""
        data = await self.get(slug, limit=CONTEXT_NOTES)
        parts = [f"Project: {data['title']} ({data['slug']})"]
        if data["summary"].strip():
            parts.append("Summary:\n" + data["summary"].strip())
        if data["notes"]:
            parts.append(
                "Notes (oldest first):\n"
                + "\n".join(f"- [{n['author']}] {n['body']}" for n in data["notes"])
            )
        text = "\n\n".join(parts)
        return text if len(text) <= CONTEXT_CHARS else text[:CONTEXT_CHARS] + "\n[truncated]"


def get_service(app: Any) -> ProjectService | None:
    existing = getattr(app.state, "project_service", None)
    if existing is not None:
        return existing
    engine = getattr(app.state, "db_engine", None)
    if engine is None:
        return None
    from backend.persistence.connection import make_session_factory

    service = ProjectService(make_session_factory(engine))
    app.state.project_service = service
    return service
