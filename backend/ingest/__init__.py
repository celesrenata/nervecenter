"""Durable ingest plane (Personal-Ops Recovery §4 — workstream C).

The supervisor (spec §16) runs one :class:`MailIngestWorker` task per
enabled mail connection. Each poll funnels fetched mail through the
*existing* communication ingestion seam (``CommunicationsService.
ingest_communication`` + ``get_pipeline().ingest``), persists raw bytes to
the object store (the graph keeps only a ``content_ref``), advances the
durable ``ingest_cursors`` row, and records per-connection status.

Credential hygiene (§4/§6): the worker resolves the secret from the durable
``secrets`` store immediately before the transport call and passes it as the
transport's separate ``credentials`` parameter — it is never persisted in
``ingest_cursors`` or any column, and never logged.
"""

from backend.ingest.mail_worker import (
    CONNECTION_KINDS,
    CURSOR_KINDS,
    ENV_INGEST_ENABLED,
    IngestSeam,
    MailIngestWorker,
    default_ingest_seam,
    ingest_enabled,
)
from backend.ingest.seen import (
    MAX_SEEN_KEYS,
    cursor_with_seen,
    is_seen,
    mark_seen,
    seen_keys,
)
from backend.ingest.supervisor import (
    MailIngestSupervisor,
    install_ingest_supervisor,
    stop_ingest_supervisor,
)

__all__ = [
    "CONNECTION_KINDS",
    "CURSOR_KINDS",
    "ENV_INGEST_ENABLED",
    "IngestSeam",
    "MailIngestSupervisor",
    "MailIngestWorker",
    "MAX_SEEN_KEYS",
    "cursor_with_seen",
    "default_ingest_seam",
    "ingest_enabled",
    "install_ingest_supervisor",
    "is_seen",
    "mark_seen",
    "seen_keys",
    "stop_ingest_supervisor",
]
