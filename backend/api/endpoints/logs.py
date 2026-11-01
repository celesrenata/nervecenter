"""Live in-service logs endpoint for the web ``/logs`` view.

``GET /api/logs`` streams the most recent rows from the process-wide ring
buffer (see :mod:`backend.observability.log_buffer`), supporting the optional
``limit`` and ``level`` query parameters. Unlike the stub collection
endpoints, this router always returns real data — the ring buffer is
populated live from both the structlog and standard-library log paths, so
there is no "unwired service" branch.
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from backend.api.endpoints._helpers import list_response
from backend.api.models import LogsResponse, LogEntry
from backend.observability.log_buffer import (
    DEFAULT_LIMIT,
    get_log_buffer,
)

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("/", response_model=LogsResponse)
async def list_logs(
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=1000),
    level: str = Query(default="", description="Minimum level (debug|info|warning|error|critical)"),
):
    """Return the most recent captured log entries, newest first.

    Parameters
    ----------
    limit:
        Maximum rows to return. Clamped to the buffer capacity.
    level:
        Optional minimum severity filter (e.g. ``warning``).
    """
    buffer = get_log_buffer()
    records = buffer.get_entries(min_level=level, limit=limit)
    data = [LogEntry(**record.as_dict()) for record in records]
    return LogsResponse(
        data=data,
        secrets_scrubbed=True,
        meta=list_response(data).meta,
    )
