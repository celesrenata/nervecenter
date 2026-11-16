"""Apple Health bridge (NS-ADR-100): validated ingest and read summaries."""

from backend.health.ingest import (
    ALLOWED_KINDS,
    MAX_BATCH_SAMPLES,
    MAX_BODY_BYTES,
    HealthBatch,
    HealthSampleIn,
    IngestResult,
    ingest_batch,
)
from backend.health.service import list_samples, summarize

__all__ = [
    "ALLOWED_KINDS",
    "MAX_BATCH_SAMPLES",
    "MAX_BODY_BYTES",
    "HealthBatch",
    "HealthSampleIn",
    "IngestResult",
    "ingest_batch",
    "list_samples",
    "summarize",
]
