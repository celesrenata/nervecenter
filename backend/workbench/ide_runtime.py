"""Runtime wiring for the Browser_IDE (Phase 3): lazy IdeManager, session
validation for the proxy, allowed origins, and the rediscover/reaper lifecycle.

Everything here is inert unless ``WORKBENCH_PHASE >= 3`` and the IDE is enabled.
No secret is read into a response; the image digest comes from the environment.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from typing import Any

import structlog
from fastapi import FastAPI

from backend.workbench import phase
from backend.workbench.ide import IdeConfig, IdeManager
from backend.workbench.kubeclient import KubeClient

log = structlog.get_logger(__name__)

IMAGE_ENV = "WORKBENCH_IDE_IMAGE"
ORIGINS_ENV = "WORKBENCH_IDE_ALLOWED_ORIGINS"


def ide_active(env: Mapping[str, str] | None = None) -> bool:
    return phase.phase_at_least(3, env) and phase.ide_enabled(env)


def allowed_origins(env: Mapping[str, str] | None = None) -> list[str]:
    e = os.environ if env is None else env
    return [o.strip() for o in e.get(ORIGINS_ENV, "").split(",") if o.strip()]


def get_ide_manager(app: FastAPI) -> IdeManager:
    """Build the manager on first use. Raises RuntimeError if no image is set."""
    mgr = getattr(app.state, "workbench_ide_manager", None)
    if mgr is not None:
        return mgr
    image = (os.environ.get(IMAGE_ENV) or "").strip()
    if not image:
        raise RuntimeError(f"{IMAGE_ENV} is not configured")
    session_factory = getattr(app.state, "workbench_session_factory", None)
    if session_factory is None:
        engine = getattr(app.state, "db_engine", None)
        if engine is not None:
            from sqlalchemy.ext.asyncio import async_sessionmaker

            session_factory = async_sessionmaker(engine, expire_on_commit=False)
    mgr = IdeManager(
        KubeClient(),
        IdeConfig.from_env(image),
        session_factory=session_factory,
    )
    app.state.workbench_ide_manager = mgr
    return mgr


def make_session_validator(app: FastAPI):
    """Validate a request's SSO credentials from its headers (for the proxy)."""
    from starlette.requests import HTTPConnection

    from backend.api import auth
    from backend.api.endpoints.mail_security import _eligible_for_server_scopes

    async def validate(headers: Mapping[str, str]) -> bool:
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/ide/",
            "query_string": b"",
            "app": app,
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
        try:
            claims = await auth._authenticate_default(HTTPConnection(scope))
            return bool(_eligible_for_server_scopes(claims))
        except Exception:  # noqa: BLE001 - any failure means no session
            return False

    return validate


async def start_background(app: FastAPI) -> list[asyncio.Task[Any]]:
    """Rediscover running IDE pods, then start the idle reaper."""
    tasks: list[asyncio.Task[Any]] = []
    if not ide_active():
        return tasks
    try:
        mgr = get_ide_manager(app)
    except Exception as exc:  # noqa: BLE001
        log.warning("workbench.ide_manager_unavailable", error=type(exc).__name__)
        return tasks
    try:
        await mgr.rediscover()
    except Exception as exc:  # noqa: BLE001 - startup must not crash
        log.warning("workbench.ide_rediscover_failed", error=type(exc).__name__)
    tasks.append(asyncio.create_task(mgr.run_reaper()))
    return tasks


async def stop_background(tasks: list[asyncio.Task[Any]]) -> None:
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
