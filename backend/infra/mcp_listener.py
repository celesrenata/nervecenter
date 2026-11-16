"""A second, single-purpose listener for the infra MCP endpoint (NS-ADR-121).

The main API (port 8000) relies on the SSO ingress for many read routes, so a
pod that can reach port 8000 directly could read personal data. The central
opencode must reach ``/api/mcp/infra`` but nothing else, so that one route is
served on its own port (``NERVE_INFRA_MCP_PORT``; unset = off, the deployment
sets 8001) by a tiny app
that mounts only the infra MCP router and shares the main app's state. The
NetworkPolicy lets opencode reach port 8001 only.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import structlog
from fastapi import FastAPI

log = structlog.get_logger(__name__)

PORT_ENV = "NERVE_INFRA_MCP_PORT"
DEFAULT_PORT = 0  # off unless the deployment sets a port


def build_app(main: Any) -> FastAPI:
    from backend.infra.mcp_server import router

    sub = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    sub.include_router(router)
    sub.state = main.state  # same services, registry and verifiers as the main app
    return sub


async def start(main: Any) -> tuple[Any, asyncio.Task[Any]] | None:
    """Start the listener in the background; ``None`` when disabled (port 0)."""
    raw = (os.environ.get(PORT_ENV) or str(DEFAULT_PORT)).strip()
    try:
        port = int(raw)
    except ValueError:
        port = DEFAULT_PORT
    if port <= 0:
        return None
    import uvicorn

    config = uvicorn.Config(build_app(main), host="0.0.0.0", port=port,  # noqa: S104
                            log_level="warning", lifespan="off", access_log=False)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # the main server owns signals
    task = asyncio.create_task(server.serve())
    log.info("infra.mcp_listener_started", port=port)
    return server, task


async def stop(handle: tuple[Any, asyncio.Task[Any]] | None) -> None:
    if handle is None:
        return
    server, task = handle
    server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=5)
    except Exception:  # noqa: BLE001 - shutdown best effort
        task.cancel()
