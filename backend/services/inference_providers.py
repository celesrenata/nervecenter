"""Inference provider + combo persistence services (providers feature).

Backs the durable provider/combo surface that replaces the read-only,
env-driven ``GET /api/inference/providers``:

- :class:`InferenceProviderService` — CRUD over ``inference_providers`` with
  live test-connection and model-recall. The ``api_key`` column is stored
  in-band so test/recall can authenticate, but every read projection masks
  it (``"••••"``) so it never reaches the browser.
- :class:`InferenceComboService` — CRUD over ``inference_combos``. A combo
  binds a provider to a model; agents point at a combo via
  ``agents.combo_id``. Re-pointing a combo's provider or model
  automatically re-binds every bound agent on the next ask (lazy
  resolution, no cascade writes).

Conventions match :mod:`backend.services.incident_agent`: async, engine +
optional session factory, ``_factory()`` helper, ISO timestamp
normalization, and honest empty-list degradation when no engine is wired.
"""

from __future__ import annotations

import structlog
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import StaticPool

from backend.persistence.models import (
    Agent,
    InferenceCombo,
    InferenceProvider,
)

log = structlog.get_logger(__name__)

#: Masked-key sentinel returned on every read projection.
_MASKED_KEY = "••••••••"

#: httpx timeout for live provider calls (test / recall).
_HTTP_TIMEOUT = 10.0


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _mask_key(api_key: str | None) -> str | None:
    """Return the masked sentinel when a key is set, ``None`` otherwise."""
    return _MASKED_KEY if api_key else None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProviderNotFoundError(Exception):
    """Raised when a provider id does not resolve to a row."""

    def __init__(self, provider_id: str) -> None:
        super().__init__(f"Provider {provider_id!r} not found")
        self.provider_id = provider_id


class ProviderNameConflictError(Exception):
    """Raised when a provider name collides with an existing row."""

    def __init__(self, name: str) -> None:
        super().__init__(f"Provider name {name!r} already exists")
        self.name = name


class ComboNotFoundError(Exception):
    """Raised when a combo id does not resolve to a row."""

    def __init__(self, combo_id: str) -> None:
        super().__init__(f"Combo {combo_id!r} not found")
        self.combo_id = combo_id


class ComboNameConflictError(Exception):
    """Raised when a combo name collides with an existing row."""

    def __init__(self, name: str) -> None:
        super().__init__(f"Combo name {name!r} already exists")
        self.name = name


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------


def _provider_to_row(orm: InferenceProvider) -> dict[str, Any]:
    """Project an ORM provider onto the web row shape (key masked)."""
    return {
        "id": str(orm.id),
        "name": orm.name,
        "provider_type": orm.provider_type,
        "base_url": orm.base_url,
        "api_key": _mask_key(orm.api_key),
        "key_configured": bool(orm.api_key),
        "capabilities": list(orm.capabilities or []),
        "models": list(orm.models or []),
        "model_count": len(orm.models or []),
        "created_at": _iso(orm.created_at),
        "updated_at": _iso(orm.updated_at),
    }


def _combo_to_row(orm: InferenceCombo, provider_name: str | None = None) -> dict[str, Any]:
    """Project an ORM combo onto the web row shape."""
    return {
        "id": str(orm.id),
        "name": orm.name,
        "provider_id": orm.provider_id,
        "provider_name": provider_name,
        "model_id": orm.model_id,
        "note": orm.note,
        "created_at": _iso(orm.created_at),
        "updated_at": _iso(orm.updated_at),
    }


# ---------------------------------------------------------------------------
# ProviderService
# ---------------------------------------------------------------------------


class InferenceProviderService:
    """Async persistence service over the ``inference_providers`` table."""

    def __init__(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._engine = engine
        self._session_factory = session_factory

    def _factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            from backend.persistence.connection import make_session_factory

            return make_session_factory(self._engine)
        return self._session_factory

    async def _load(self, session: AsyncSession, provider_id: str) -> InferenceProvider:
        orm = await session.get(InferenceProvider, provider_id)
        if orm is None:
            raise ProviderNotFoundError(provider_id)
        return orm

    # -- reads ---------------------------------------------------------------

    async def list(self) -> list[dict[str, Any]]:
        """Return all durable providers (key masked), newest first."""
        factory = self._factory()
        async with factory() as session:
            result = await session.execute(
                select(InferenceProvider).order_by(InferenceProvider.created_at.desc())
            )
            rows = result.scalars().all()
            return [_provider_to_row(r) for r in rows]

    async def get(self, provider_id: str) -> dict[str, Any]:
        """Return one durable provider by id, or raise ProviderNotFoundError."""
        factory = self._factory()
        async with factory() as session:
            orm = await self._load(session, provider_id)
            return _provider_to_row(orm)

    # -- writes ----------------------------------------------------------------

    async def create(
        self,
        *,
        name: str,
        provider_type: str,
        base_url: str | None = None,
        api_key: str | None = None,
        capabilities: list[str] | None = None,
        models: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create and persist a new provider. Returns the web row (201).

        Raises :class:`ProviderNameConflictError` when ``name`` collides.
        """
        factory = self._factory()
        async with factory() as session:
            orm = InferenceProvider(
                name=name,
                provider_type=provider_type,
                base_url=base_url,
                api_key=api_key or None,
                capabilities=list(capabilities or []),
                models=list(models or []),
                updated_at=_utcnow(),
            )
            session.add(orm)
            try:
                await session.commit()
            except Exception as exc:
                await session.rollback()
                if "UNIQUE" in str(exc).upper() or "unique" in str(exc).lower():
                    raise ProviderNameConflictError(name) from exc
                raise
            await session.refresh(orm)
            log.info("providers.created", provider_id=str(orm.id), name=name)
            return _provider_to_row(orm)

    async def update(
        self,
        provider_id: str,
        *,
        name: str | None = None,
        provider_type: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        capabilities: list[str] | None = None,
        models: list[str] | None = None,
    ) -> dict[str, Any]:
        """Partially update a provider; returns the web row.

        ``None`` leaves the field unchanged. A supplied empty-string
        ``api_key`` clears the stored key (the sentinel ``"••••"`` is
        ignored — it is a read-only mask, never a write value).
        """
        factory = self._factory()
        async with factory() as session:
            orm = await self._load(session, provider_id)
            if name is not None and name != orm.name:
                orm.name = name
            if provider_type is not None:
                orm.provider_type = provider_type
            if base_url is not None:
                orm.base_url = base_url
            if api_key is not None and api_key != _MASKED_KEY:
                orm.api_key = api_key or None
            if capabilities is not None:
                orm.capabilities = list(capabilities)
            if models is not None:
                orm.models = list(models)
            orm.updated_at = _utcnow()
            try:
                await session.commit()
            except Exception as exc:
                await session.rollback()
                if "UNIQUE" in str(exc).upper() or "unique" in str(exc).lower():
                    raise ProviderNameConflictError(name or orm.name) from exc
                raise
            await session.refresh(orm)
            log.info("providers.updated", provider_id=provider_id)
            return _provider_to_row(orm)

    async def delete(self, provider_id: str) -> None:
        """Delete a provider. Raises ProviderNotFoundError when unknown.

        Combos pointing at this provider are left intact (the combo's
        ``provider_id`` becomes a dangling reference that the UI can
        surface as "provider removed" — no cascade delete, so no silent
        agent unbinding).
        """
        factory = self._factory()
        async with factory() as session:
            orm = await self._load(session, provider_id)
            await session.delete(orm)
            await session.commit()
            log.info("providers.deleted", provider_id=provider_id)

    # -- live operations -------------------------------------------------------

    @staticmethod
    def _models_endpoint(provider_type: str, base_url: str | None) -> str | None:
        """Build the provider's models-list URL, or ``None`` when the
        provider type has no models-list endpoint (e.g. Anthropic)."""
        base = (base_url or "").strip().rstrip("/")
        ptype = provider_type.lower()
        if ptype == "local":
            # Ollama
            return f"{base}/api/tags" if base else "http://localhost:11434/api/tags"
        if ptype in ("openai", "anthropic"):
            # OpenAI-compatible /v1/models; Anthropic has no list endpoint
            if ptype == "anthropic":
                return None  # type: ignore[return-value]
            return f"{base}/models" if base else "https://api.openai.com/v1/models"
        # Unknown type — try /v1/models generically
        if base:
            return f"{base}/v1/models"
        return None

    async def _probe(
        self,
        provider_type: str,
        base_url: str | None,
        api_key: str | None,
    ) -> dict[str, Any]:
        """Run a live models-endpoint probe against given (possibly
        unpersisted) provider credentials.

        Returns ``{"success": bool, "models_found": int, "models": [...],
        "error": str|None, "latency_ms": int|None}``. Never raises for a
        network failure — the error is in the response body.
        """
        endpoint = self._models_endpoint(provider_type, base_url)
        if endpoint is None:
            return {
                "success": False,
                "models_found": 0,
                "models": [],
                "error": f"Provider type '{provider_type}' has no models-list endpoint",
                "latency_ms": None,
            }
        headers: dict[str, str] = {}
        if api_key:
            if provider_type.lower() == "anthropic":
                headers["x-api-key"] = api_key
            else:
                headers["Authorization"] = f"Bearer {api_key}"

        import time as _time

        t0 = _time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(endpoint, headers=headers)
                latency = int((_time.monotonic() - t0) * 1000)
                if resp.status_code >= 400:
                    return {
                        "success": False,
                        "models_found": 0,
                        "models": [],
                        "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
                        "latency_ms": latency,
                    }
                models = self._parse_models(provider_type, resp.json())
                return {
                    "success": True,
                    "models_found": len(models),
                    "models": models,
                    "error": None,
                    "latency_ms": latency,
                }
        except httpx.HTTPError as exc:
            latency = int((_time.monotonic() - t0) * 1000)
            return {
                "success": False,
                "models_found": 0,
                "models": [],
                "error": f"Connection error: {exc}",
                "latency_ms": latency,
            }

    async def test_connection(self, provider_id: str) -> dict[str, Any]:
        """Probe the provider's models endpoint; returns the result.

        Returns the shape produced by :meth:`_probe`. Never raises for a
        network failure — the error is in the response body.
        """
        factory = self._factory()
        async with factory() as session:
            orm = await self._load(session, provider_id)
            return await self._probe(orm.provider_type, orm.base_url, orm.api_key)

    async def probe_ad_hoc(
        self,
        *,
        provider_type: str,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """Probe a provider's models endpoint using transient (unpersisted)
        credentials.

        This is the pre-save probe: it validates a provider configuration
        and returns its model list without writing anything to the registry.
        Use it to pre-fetch a model list before saving a provider.
        """
        return await self._probe(provider_type, base_url, api_key)

    async def recall_models(self, provider_id: str) -> dict[str, Any]:
        """Recall the provider's model list and persist it to the ``models`` column.

        Returns the same shape as :meth:`test_connection` plus the updated
        web row.
        """
        result = await self.test_connection(provider_id)
        if result["success"]:
            # Persist the recalled models
            factory = self._factory()
            async with factory() as session:
                orm = await self._load(session, provider_id)
                orm.models = result["models"]
                orm.updated_at = _utcnow()
                await session.commit()
                await session.refresh(orm)
                log.info(
                    "providers.models_recalled",
                    provider_id=provider_id,
                    count=len(result["models"]),
                )
                result["row"] = _provider_to_row(orm)
        return result

    @staticmethod
    def _parse_models(provider_type: str, data: Any) -> list[str]:
        """Extract a sorted, de-duplicated model-id list from a raw API response."""
        ptype = provider_type.lower()
        ids: list[str] = []
        if ptype == "local":
            # Ollama: {"models": [{"name": "...", ...}, ...]}
            for m in data.get("models", []):
                name = m.get("name") or m.get("model")
                if name:
                    ids.append(name)
        else:
            # OpenAI-compatible: {"data": [{"id": "...", ...}, ...]}
            for m in data.get("data", []):
                mid = m.get("id") or m.get("model_id")
                if mid:
                    ids.append(mid)
        return sorted(set(ids))


# ---------------------------------------------------------------------------
# ComboService
# ---------------------------------------------------------------------------


class InferenceComboService:
    """Async persistence service over the ``inference_combos`` table."""

    def __init__(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._engine = engine
        self._session_factory = session_factory

    def _factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            from backend.persistence.connection import make_session_factory

            return make_session_factory(self._engine)
        return self._session_factory

    async def _load(self, session: AsyncSession, combo_id: str) -> InferenceCombo:
        orm = await session.get(InferenceCombo, combo_id)
        if orm is None:
            raise ComboNotFoundError(combo_id)
        return orm

    # -- reads ---------------------------------------------------------------

    async def list(self) -> list[dict[str, Any]]:
        """Return all combos with provider names resolved, newest first."""
        factory = self._factory()
        async with factory() as session:
            combos = (
                await session.execute(
                    select(InferenceCombo).order_by(InferenceCombo.created_at.desc())
                )
            ).scalars().all()
            providers = (
                (
                    await session.execute(select(InferenceProvider))
                ).scalars()
                .all()
            )
            pmap = {str(p.id): p.name for p in providers}
            return [_combo_to_row(c, pmap.get(c.provider_id)) for c in combos]

    async def get(self, combo_id: str) -> dict[str, Any]:
        """Return one combo with provider name, or raise ComboNotFoundError."""
        factory = self._factory()
        async with factory() as session:
            combo = await self._load(session, combo_id)
            provider = await session.get(InferenceProvider, combo.provider_id)
            return _combo_to_row(combo, provider.name if provider else None)

    async def list_bound_agents(self, combo_id: str) -> list[dict[str, Any]]:
        """Return the agents bound to this combo (for cascade preview)."""
        factory = self._factory()
        async with factory() as session:
            result = await session.execute(
                select(Agent).where(Agent.combo_id == combo_id)
            )
            rows = result.scalars().all()
            return [{"id": str(r.id), "name": r.name} for r in rows]

    # -- writes ---------------------------------------------------------------

    async def create(
        self,
        *,
        name: str,
        provider_id: str,
        model_id: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Create and persist a new combo. Returns the web row (201).

        Raises :class:`ComboNameConflictError` on name collision and
        :class:`ProviderNotFoundError` when the provider id is unknown.
        """
        factory = self._factory()
        async with factory() as session:
            provider = await session.get(InferenceProvider, provider_id)
            if provider is None:
                raise ProviderNotFoundError(provider_id)
            orm = InferenceCombo(
                name=name,
                provider_id=provider_id,
                model_id=model_id,
                note=note,
                updated_at=_utcnow(),
            )
            session.add(orm)
            try:
                await session.commit()
            except Exception as exc:
                await session.rollback()
                if "UNIQUE" in str(exc).upper() or "unique" in str(exc).lower():
                    raise ComboNameConflictError(name) from exc
                raise
            await session.refresh(orm)
            log.info("combos.created", combo_id=str(orm.id), name=name)
            return _combo_to_row(orm, provider.name)

    async def update(
        self,
        combo_id: str,
        *,
        name: str | None = None,
        provider_id: str | None = None,
        model_id: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Partially update a combo; returns the web row.

        When ``provider_id`` or ``model_id`` changes, every bound agent
        is automatically re-bound on the next ask (lazy resolution via
        :meth:`AgentService.get_inference`). No explicit cascade write is
        needed — the agent's ``combo_id`` still points at the same combo
        row, and the combo's ``provider_id`` / ``model_id`` are what
        changed.
        """
        factory = self._factory()
        async with factory() as session:
            orm = await self._load(session, combo_id)
            if name is not None and name != orm.name:
                orm.name = name
            if provider_id is not None:
                provider = await session.get(InferenceProvider, provider_id)
                if provider is None:
                    raise ProviderNotFoundError(provider_id)
                orm.provider_id = provider_id
            if model_id is not None:
                orm.model_id = model_id
            if note is not None:
                orm.note = note
            orm.updated_at = _utcnow()
            try:
                await session.commit()
            except Exception as exc:
                await session.rollback()
                if "UNIQUE" in str(exc).upper() or "unique" in str(exc).lower():
                    raise ComboNameConflictError(name or orm.name) from exc
                raise
            await session.refresh(orm)
            provider = await session.get(InferenceProvider, orm.provider_id)
            log.info("combos.updated", combo_id=combo_id)
            return _combo_to_row(orm, provider.name if provider else None)

    async def delete(self, combo_id: str) -> None:
        """Delete a combo and unbind all agents pointing at it.

        Agents that had ``combo_id == combo_id`` are set to ``combo_id = NULL``
        so they fall back to their per-agent ``inference`` binding or the
        service default — no silent agent deletion.
        """
        factory = self._factory()
        async with factory() as session:
            orm = await self._load(session, combo_id)
            # Unbind all agents pointing at this combo
            result = await session.execute(
                select(Agent).where(Agent.combo_id == combo_id)
            )
            bound = result.scalars().all()
            for agent in bound:
                agent.combo_id = None
            await session.delete(orm)
            await session.commit()
            log.info(
                "combos.deleted",
                combo_id=combo_id,
                agents_unbound=len(bound),
            )
