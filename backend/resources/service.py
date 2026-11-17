"""Owner resources: files in the encrypted object store, linked to agents (NS-ADR-125).

Bytes go to the object store under ``resources/<id>`` (on NFS in production, encrypted
client-side). The database keeps metadata and the agent links. ``secret`` resources are
for the owner and backend importers only: they cannot be linked and agents never read them.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
import uuid
from collections.abc import Callable
from typing import Any

import structlog
from sqlalchemy import delete, select

from backend.persistence.models.resources import AgentResourceLink, Resource
from backend.storage.errors import ObjectNotFoundError

log = structlog.get_logger(__name__)

MAX_BYTES = 20 * 1024 * 1024
SENSITIVITIES = ("normal", "secret")
KEY_PREFIX = "resources/"
MAX_LINKS_PER_AGENT = 50
_NAME_BAD = re.compile(r"[^A-Za-z0-9._ -]+")
_TEXT_TYPES = ("application/json", "application/xml", "application/yaml", "application/x-yaml",
               "application/toml", "application/x-sh", "application/javascript")
_TEXT_EXT = {".md": "text/markdown", ".yaml": "application/yaml", ".yml": "application/yaml",
             ".log": "text/plain", ".conf": "text/plain", ".toml": "application/toml",
             ".csv": "text/csv", ".xml": "application/xml", ".json": "application/json"}


class ResourceInputError(ValueError):
    pass


class ResourceTooLarge(ResourceInputError):
    pass


class ResourceStoreUnavailable(RuntimeError):
    """The object store is not configured (HTTP 503)."""


def clean_name(name: Any) -> str:
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = _NAME_BAD.sub("_", base).strip(" .")[:120]
    return base or "file"


def guess_content_type(name: str, data: bytes) -> str:
    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in _TEXT_EXT:
        return _TEXT_EXT[ext]
    guessed = mimetypes.guess_type(name)[0]
    if guessed:
        return guessed
    try:
        text = data[:4096].decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"
    return "application/octet-stream" if "\x00" in text else "text/plain"


def is_text_type(content_type: str) -> bool:
    return content_type.startswith("text/") or content_type in _TEXT_TYPES


def resource_view(row: Resource) -> dict[str, Any]:
    return {
        "id": row.id, "name": row.name, "content_type": row.content_type, "size": row.size,
        "sha256": row.sha256, "sensitivity": row.sensitivity, "uploaded_by": row.uploaded_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


class ResourceService:
    def __init__(self, session_factory: Any, store: Any | Callable[[], Any] | None = None) -> None:
        self._sessions = session_factory
        self._store_src = store

    def _store(self) -> Any:
        src = self._store_src
        if src is None:
            from backend.ingest.supervisor import _object_store_from_env

            store = _object_store_from_env()
        elif callable(src) and not hasattr(src, "put"):
            store = src()
        else:
            store = src
        if store is None:
            raise ResourceStoreUnavailable("object store is not configured")
        return store

    # -- files -------------------------------------------------------------------------
    async def put(self, name: Any, data: bytes, *, sensitivity: str = "normal",
                  uploaded_by: str | None = None) -> dict[str, Any]:
        if sensitivity not in SENSITIVITIES:
            raise ResourceInputError("sensitivity must be normal or secret")
        if len(data) > MAX_BYTES:
            raise ResourceTooLarge("file is larger than 20 MB")
        if not data:
            raise ResourceInputError("file is empty")
        store = self._store()
        safe = clean_name(name)
        rid = str(uuid.uuid4())
        ctype = guess_content_type(safe, data)
        key = f"{KEY_PREFIX}{rid}"
        await store.put(key, data, content_type=ctype)
        row = Resource(id=rid, name=safe, content_type=ctype, size=len(data),
                       sha256=hashlib.sha256(data).hexdigest(), sensitivity=sensitivity,
                       object_key=key, uploaded_by=uploaded_by)
        try:
            async with self._sessions() as s:
                s.add(row)
                await s.commit()
                await s.refresh(row)
        except Exception:
            await store.delete(key)
            raise
        log.info("resources.put", resource_id=rid, size=len(data), sensitivity=sensitivity)
        return resource_view(row)

    async def list_all(self) -> list[dict[str, Any]]:
        async with self._sessions() as s:
            rows = (await s.execute(select(Resource).order_by(Resource.created_at.desc()))
                    ).scalars().all()
        return [resource_view(r) for r in rows]

    async def _row(self, s: Any, rid: str) -> Resource:
        row = await s.get(Resource, rid)
        if row is None:
            raise KeyError(rid)
        return row

    async def get(self, rid: str) -> dict[str, Any]:
        async with self._sessions() as s:
            return resource_view(await self._row(s, rid))

    async def download(self, rid: str) -> tuple[dict[str, Any], bytes]:
        async with self._sessions() as s:
            row = await self._row(s, rid)
            view, key = resource_view(row), row.object_key
        try:
            return view, await self._store().get(key)
        except ObjectNotFoundError:
            raise KeyError(rid) from None

    async def delete(self, rid: str) -> None:
        async with self._sessions() as s:
            row = await self._row(s, rid)
            key = row.object_key
            await s.execute(delete(AgentResourceLink).where(AgentResourceLink.resource_id == rid))
            await s.delete(row)
            await s.commit()
        try:
            await self._store().delete(key)
        except ResourceStoreUnavailable:
            log.warning("resources.delete_file_skipped", resource_id=rid)

    # -- links -------------------------------------------------------------------------
    async def get_links(self, agent_id: str) -> list[dict[str, Any]]:
        async with self._sessions() as s:
            rows = (await s.execute(
                select(Resource)
                .join(AgentResourceLink, AgentResourceLink.resource_id == Resource.id)
                .where(AgentResourceLink.agent_id == agent_id).order_by(Resource.name)
            )).scalars().all()
        return [resource_view(r) for r in rows]

    async def set_links(self, agent_id: str, resource_ids: list[str]) -> list[dict[str, Any]]:
        ids = list(dict.fromkeys(resource_ids))
        if len(ids) > MAX_LINKS_PER_AGENT:
            raise ResourceInputError("too many resources for one agent")
        async with self._sessions() as s:
            if ids:
                found = (await s.execute(select(Resource).where(Resource.id.in_(ids)))
                         ).scalars().all()
                by_id = {r.id: r for r in found}
                missing = [i for i in ids if i not in by_id]
                if missing:
                    raise KeyError(missing[0])
                secret = [i for i in ids if by_id[i].sensitivity != "normal"]
                if secret:
                    raise ResourceInputError(
                        "secret resources cannot be linked to an agent: " + ", ".join(secret))
            await s.execute(delete(AgentResourceLink).where(AgentResourceLink.agent_id == agent_id))
            for rid in ids:
                s.add(AgentResourceLink(agent_id=agent_id, resource_id=rid))
            await s.commit()
        return await self.get_links(agent_id)

    async def read_linked(self, agent_id: str, rid: str) -> tuple[dict[str, Any], bytes]:
        """Bytes of a normal resource linked to ``agent_id``; KeyError otherwise."""
        async with self._sessions() as s:
            link = await s.get(AgentResourceLink, (agent_id, rid))
            row = await s.get(Resource, rid)
            if link is None or row is None or row.sensitivity != "normal":
                raise KeyError(rid)
            view, key = resource_view(row), row.object_key
        try:
            return view, await self._store().get(key)
        except ObjectNotFoundError:
            raise KeyError(rid) from None


def get_service(app: Any) -> ResourceService | None:
    svc = getattr(app.state, "resource_service", None)
    if svc is not None:
        return svc
    registry = getattr(app.state, "repository_registry", None)
    if registry is None:
        return None
    svc = ResourceService(registry.session_factory)
    app.state.resource_service = svc
    return svc


__all__ = [
    "ResourceInputError", "ResourceService", "ResourceStoreUnavailable", "ResourceTooLarge",
    "get_service", "is_text_type",
]
