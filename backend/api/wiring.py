"""Startup wiring of durable dependencies (DB engine, NATS client, services) onto app.state.

The health probes in :mod:`backend.api.health` read ``app.state.db_engine`` and
``app.state.nats_client``.  Before this module, neither attribute was ever
populated, so ``GET /api/health`` always reported ``"not configured"`` even
when the deployment environment carried valid URLs.  Service-backed routers
(e.g. :mod:`backend.api.endpoints.evidence`) read
``app.state.services["evidence"]``; the evidence service is built here from
the same durable engine whenever a DB URL is configured.

Environment contract
--------------------
The deploy contract names the variables ``NERVE_DB_URL`` / ``NERVE_NATS_URL``;
the backend runtime historically read ``NERVECENTER_DATABASE_URL`` /
``NERVECENTER_NATS_URL`` (see :data:`backend.persistence.connection.ENV_DATABASE_URL`
and :data:`backend.events.nats_client.ENV_NATS_URL`).  The ``NERVE_*`` names
take precedence; the ``NERVECENTER_*`` names are the fallback.

Tolerance rules
---------------
- URL env missing  → the attribute is set to ``None``, health reports
  ``not configured`` (liveness and readiness keep returning HTTP 200).
- Dependency unreachable → the attribute is still set to a real object: the
  async engine is lazy (created without connecting) and the NATS client is
  stored after a *bounded* connect attempt, so health reports a real
  ``unreachable`` probe result instead of ``not configured``.
- Startup must never raise or block for more than a few seconds.

Known factory defect (worked around, not fixed — ``backend/persistence`` is
out of scope): :func:`backend.persistence.connection.create_engine` logs via
``redact_url``, which calls ``urlsplit(url)._replace(password="***")``.  Since
``SplitResult`` is a named tuple of ``(scheme, netloc, path, query,
fragment)`` and ``password`` is only a property, that call raises
``ValueError: Got unexpected field names: ['password']`` for **any** URL with
embedded credentials.  :func:`build_db_engine` therefore falls back to
equivalent direct engine construction (same pool class, knobs, and connect
args) whenever the factory raises.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import structlog
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from backend.events.nats_client import (
    ENV_NATS_URL,
    NatsClient,
)
from backend.events.nats_client import config_from_env as nats_config_from_env
from backend.evidence.service import EvidenceService
from backend.goals.service import GoalService
from backend.knowledge.entity import EntityService
from backend.knowledge.relationship import RelationshipService
from backend.persistence.connection import (
    ENV_DATABASE_URL,
    DatabaseConfig,
    create_engine,
    dispose_engine,
)
from backend.services.systems import SystemsService
from backend.tasks.service import TaskService

log = structlog.get_logger(__name__)

#: Deploy-contract DB URL name; takes precedence over ``ENV_DATABASE_URL``.
ENV_DB_URL_PRIMARY = "NERVE_DB_URL"

#: Deploy-contract NATS URL name; takes precedence over ``ENV_NATS_URL``.
ENV_NATS_URL_PRIMARY = "NERVE_NATS_URL"

#: Hard upper bound (seconds) for the eager NATS connect attempt at startup.
NATS_CONNECT_TIMEOUT_S = 5.0


def _safe_redact(url: str) -> str:
    """Mask the password in ``url`` for safe logging.

    Uses only real :class:`~urllib.parse.SplitResult` fields (``netloc``), so
    — unlike :func:`backend.persistence.connection.redact_url` — it cannot
    raise for URLs with embedded credentials.
    """

    parts = urlsplit(url)
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, _, host = netloc.rpartition("@")
        netloc = f"{userinfo.split(':', 1)[0]}:***@{host}"
    return urlunsplit(parts._replace(netloc=netloc))


def resolve_db_url() -> str | None:
    """Return the configured DB URL, or ``None`` when no URL env is set.

    Reads ``NERVE_DB_URL`` first, then the historical ``NERVECENTER_DATABASE_URL``
    (``ENV_DATABASE_URL``).  Unlike
    :func:`backend.persistence.connection.config_from_env`, this returns
    ``None`` instead of falling back to the local dev default, so missing
    configuration degrades to ``not configured`` rather than silently pointing
    at ``localhost:5432``.
    """

    return os.environ.get(ENV_DB_URL_PRIMARY) or os.environ.get(ENV_DATABASE_URL)


def resolve_nats_url() -> str | None:
    """Return the configured NATS URL, or ``None`` when no URL env is set.

    Reads ``NERVE_NATS_URL`` first, then the historical ``NERVECENTER_NATS_URL``
    (``ENV_NATS_URL``).
    """

    return os.environ.get(ENV_NATS_URL_PRIMARY) or os.environ.get(ENV_NATS_URL)


def _build_fallback_engine(url: str) -> AsyncEngine:
    """Build the engine directly when the standard factory is unusable.

    Mirrors :func:`backend.persistence.connection.create_engine` exactly
    (same pool class, knobs, and connect args) except for the URL-redaction
    logging step, which currently raises for URLs with embedded credentials.
    """

    cfg = DatabaseConfig(url=url)  # validates the asyncpg dialect
    connect_args: dict[str, Any] = {}
    if cfg.connect_timeout is not None:
        connect_args["timeout"] = cfg.connect_timeout
    if cfg.ssl_mode is not None:
        connect_args["ssl"] = cfg.ssl_mode == "require"

    engine = create_async_engine(
        cfg.url,
        poolclass=AsyncAdaptedQueuePool,
        pool_size=cfg.pool_size,
        max_overflow=cfg.max_overflow,
        pool_recycle=cfg.pool_recycle,
        pool_pre_ping=cfg.pool_pre_ping,
        echo=cfg.echo,
        connect_args=connect_args or None,
    )
    return engine


def build_db_engine() -> AsyncEngine | None:
    """Create the async engine from the resolved URL; ``None`` when unset.

    Uses the existing URL→engine factory
    :func:`backend.persistence.connection.create_engine`, which is lazy: no
    network I/O happens until the first query, so this cannot block startup
    even when the database is unreachable.  If the factory itself raises (see
    the known ``redact_url`` defect in the module docstring), an equivalent
    engine is built directly instead of letting startup fail.
    """

    url = resolve_db_url()
    if url is None:
        return None
    try:
        engine = create_engine(url=url)
    except Exception as exc:  # noqa: BLE001 - factory defect must not break startup
        log.warning("api.wiring.db_factory_failed", error=str(exc))
        engine = _build_fallback_engine(url)
    log.info("api.wiring.db_engine_created", url=_safe_redact(url))
    return engine


def build_nats_client() -> NatsClient | None:
    """Build the NATS client from the resolved URL; ``None`` when unset.

    The other ``NERVECENTER_NATS_*`` knobs still come from
    :func:`backend.events.nats_client.config_from_env`; only the URL is
    overridden when the deploy-contract variable wins the precedence.  No
    connection is opened here — see :func:`wire_dependencies`.
    """

    url = resolve_nats_url()
    if url is None:
        return None
    config = nats_config_from_env()
    if config.url != url:
        config = replace(config, url=url)
    return NatsClient(config)


async def build_services(engine: AsyncEngine) -> dict[str, Any]:
    """Construct the write-path application services over a live engine.

    Returns the service instances keyed by the names the endpoint modules
    read from ``app.state.services``:

    - ``goals`` — :class:`~backend.goals.service.GoalService`
    - ``tasks`` — :class:`~backend.tasks.service.TaskService`
    - ``entities`` — :class:`~backend.knowledge.entity.EntityService`
    - ``relationships`` — :class:`~backend.knowledge.relationship.RelationshipService`

    Only entities backed by a real persistence/service contract are wired
    here. Claims and agents have no such contract, so their write endpoints
    must (and do) answer ``501 Not Implemented`` rather than accept writes
    without storing anything.
    """

    return {
        "goals": GoalService(engine),
        "tasks": TaskService(engine),
        "entities": EntityService(engine),
        "relationships": RelationshipService(engine),
    }


async def wire_dependencies(target: FastAPI) -> None:
    """Populate ``target.state.db_engine`` / ``target.state.nats_client`` /
    ``target.state.services``.

    Every failure (bad URL, unreachable server, timeout) is logged, never
    raised: the Kubernetes liveness and readiness probes both hit
    ``/api/health`` and must keep receiving HTTP 200.
    """

    try:
        target.state.db_engine = build_db_engine()
    except Exception as exc:  # noqa: BLE001 - startup must not crash on config errors
        log.warning("api.wiring.db_engine_failed", error=str(exc))
        target.state.db_engine = None

    # Build the NATS client up front (bounded connect below) so that services
    # which need it — the systems status service probes it — can be registered
    # in the same pass as the DB-backed services.
    target.state.nats_client = await _build_and_connect_nats(target)

    # Service-backed routers read ``app.state.services``.  The evidence and
    # write-path services (goals, tasks, entities, relationships) are bound
    # to the same durable engine when one exists; when the engine is absent
    # (NERVE_DB_URL unset) their keys are dropped so the endpoints keep
    # their honest empty/503 degradation — never a fake 202.  The
    # ``systems`` service is always registered (it composes its report from
    # whatever dependencies are configured, omitting rows for the absent ones).
    services = dict(getattr(target.state, "services", None) or {})
    try:
        services["systems"] = SystemsService(
            target.state.db_engine, target.state.nats_client
        )
    except Exception as exc:  # noqa: BLE001 - startup must not crash on config errors
        log.warning("api.wiring.systems_service_failed", error=str(exc))
        services.pop("systems", None)
    if target.state.db_engine is not None:
        try:
            services["evidence"] = EvidenceService(target.state.db_engine)
        except Exception as exc:  # noqa: BLE001 - startup must not crash on config errors
            log.warning("api.wiring.evidence_service_failed", error=str(exc))
            services.pop("evidence", None)
        try:
            services.update(await build_services(target.state.db_engine))
        except Exception as exc:  # noqa: BLE001 - startup must not crash on config errors
            log.warning("api.wiring.services_failed", error=str(exc))
    else:
        services.pop("evidence", None)
        for key in ("goals", "tasks", "entities", "relationships"):
            services.pop(key, None)
    target.state.services = services


async def _build_and_connect_nats(target: FastAPI) -> NatsClient | None:
    """Build the NATS client, run a bounded connect attempt, then ensure the
    JetStream event stream exists.

    The client is stored on ``target.state.nats_client`` even when the
    connect attempt fails: health must report a real ``unreachable`` probe
    (``connected: false``) instead of ``"NATS is not configured"``.  The
    attempt is bounded so startup never blocks for more than a few seconds.
    Returns the client (or ``None`` when no NATS URL is configured / the
    factory fails) so callers can hand it to services.

    Gap-closure: the deploy path never created the ``NERVECENTER_EVENTS``
    JetStream stream (only the standalone ``backend.events`` entrypoint
    called ``ensure_stream``), so ``GET /api/events`` and
    ``WS /api/events/ws`` degraded to a permanent 503
    ``event_bus_unavailable``.  We now ensure the stream here, bounded and
    idempotent (``ensure_stream`` checks ``stream_info`` first); on failure
    the client is kept as-is and the read path keeps its honest 503
    degradation instead of crashing startup.
    """

    try:
        nats_client = build_nats_client()
    except Exception as exc:  # noqa: BLE001 - startup must not crash on config errors
        log.warning("api.wiring.nats_client_failed", error=str(exc))
        nats_client = None

    if nats_client is not None:
        try:
            await asyncio.wait_for(nats_client.connect(), timeout=NATS_CONNECT_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - unreachable NATS must not block startup
            log.warning("api.wiring.nats_connect_failed", error=str(exc))

        if nats_client.is_connected:
            try:
                await asyncio.wait_for(
                    nats_client.ensure_stream(), timeout=NATS_CONNECT_TIMEOUT_S
                )
            except Exception as exc:  # noqa: BLE001 - stream creation must not block startup
                log.warning(
                    "api.wiring.nats_ensure_stream_failed",
                    stream=nats_client.config.stream_name,
                    error=str(exc),
                )

    return nats_client


async def teardown_dependencies(target: FastAPI) -> None:
    """Dispose the engine and disconnect NATS; absence of either is a no-op."""

    engine = getattr(target.state, "db_engine", None)
    if engine is not None:
        try:
            await dispose_engine(engine)
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            log.warning("api.wiring.db_dispose_failed", error=str(exc))
        target.state.db_engine = None

    # Every service held a reference to the now-disposed engine; clear them
    # all so a later startup rebuilds fresh services from the new engine.
    services = getattr(target.state, "services", None)
    if isinstance(services, dict):
        services.clear()

    client = getattr(target.state, "nats_client", None)
    if client is not None:
        try:
            await client.disconnect()
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            log.warning("api.wiring.nats_disconnect_failed", error=str(exc))
        target.state.nats_client = None


def install_dependency_wiring(target: FastAPI) -> None:
    """Register the startup/shutdown lifecycle on ``target`` via ASGI lifespan.

    Applied to BOTH the module-level ``app`` and ``create_app()`` in
    :mod:`backend.api.app` so that both instances wire their dependencies the
    same way.  Uses a single ``@asynccontextmanager`` lifespan attached to
    ``target.router.lifespan_context``: startup runs :func:`wire_dependencies`,
    shutdown runs :func:`teardown_dependencies`.  This replaces the deprecated
    ``@target.on_event("startup"|"shutdown")`` registrations while preserving
    the exact ordering the handlers had (dependency wiring first on startup,
    teardown last on shutdown; no other startup/shutdown handlers are
    registered on the app, so nothing else is displaced).
    """

    @asynccontextmanager
    async def _wiring_lifespan(app: FastAPI):
        await wire_dependencies(target)
        try:
            yield
        finally:
            await teardown_dependencies(target)

    # Starlette keeps the ASGI lifespan handler on the app's router; FastAPI
    # (and the module-level ``app``) build their ``Router`` with the default
    # handler, so overwriting it here is the supported in-place upgrade that
    # keeps the public ``install_dependency_wiring(target)`` surface intact.
    target.router.lifespan_context = _wiring_lifespan
