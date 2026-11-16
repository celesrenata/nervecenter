"""Validation and idempotent storage for Apple Health batches (NS-ADR-100).

Only allowed kinds with their fixed units are accepted. Each sample gets a
server-side dedupe key, so replaying a batch stores nothing new. ``sleep``
samples (one per night, minutes asleep) are also mirrored into the existing
``sleep_records`` table with source ``apple_health`` so the wellness sleep
seam sees them. Logs carry counts only.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.persistence.connection import make_session_factory
from backend.persistence.models.health import HealthSampleRow
from backend.persistence.models.routines import SleepRecordRow

log = structlog.get_logger(__name__)

#: kind -> (unit, min value, max value)
ALLOWED_KINDS: dict[str, tuple[str, float, float]] = {
    "body_mass": ("kg", 1.0, 700.0),
    "sleep": ("min", 1.0, 1440.0),
    "step_count": ("count", 0.0, 200_000.0),
    "heart_rate": ("bpm", 10.0, 300.0),
    "resting_heart_rate": ("bpm", 10.0, 300.0),
    "hrv": ("ms", 0.0, 1000.0),
    "active_energy": ("kcal", 0.0, 20_000.0),
}
MAX_BATCH_SAMPLES = 500
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_FUTURE = timedelta(days=1)
MAX_SPAN = timedelta(days=2)


class HealthSampleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(max_length=32)
    start_at: datetime
    end_at: datetime
    value: float
    unit: str = Field(max_length=16)
    source: str = Field(default="apple_health", max_length=64)

    @field_validator("start_at", "end_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return v if v.tzinfo else v.replace(tzinfo=UTC)

    @model_validator(mode="after")
    def _check(self) -> HealthSampleIn:
        spec = ALLOWED_KINDS.get(self.kind)
        if spec is None:
            raise ValueError("kind not allowed")
        unit, lo, hi = spec
        if self.unit != unit:
            raise ValueError("unit mismatch")
        if not (lo <= self.value <= hi) or self.value != self.value:
            raise ValueError("value out of range")
        if self.end_at < self.start_at:
            raise ValueError("end before start")
        if self.end_at - self.start_at > MAX_SPAN:
            raise ValueError("span too long")
        if self.end_at > datetime.now(UTC) + MAX_FUTURE:
            raise ValueError("timestamp in the future")
        return self


class HealthBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(min_length=1, max_length=128)
    samples: list[HealthSampleIn] = Field(max_length=MAX_BATCH_SAMPLES)


@dataclass(frozen=True)
class IngestResult:
    received: int
    stored: int
    skipped: int
    sleep_records: int

    def as_dict(self) -> dict[str, int]:
        return {
            "received": self.received,
            "stored": self.stored,
            "skipped": self.skipped,
            "sleep_records": self.sleep_records,
        }


def dedupe_key(device_id: str, s: HealthSampleIn) -> str:
    raw = "|".join(
        (
            device_id,
            s.kind,
            s.start_at.astimezone(UTC).isoformat(),
            s.end_at.astimezone(UTC).isoformat(),
            s.source,
        )
    )
    return hashlib.sha256(raw.encode()).hexdigest()


async def ingest_batch(engine: AsyncEngine, batch: HealthBatch) -> IngestResult:
    """Store new samples; a replayed sample is skipped. Never raises on dupes."""
    factory = make_session_factory(engine)
    keyed: dict[str, HealthSampleIn] = {}
    for sample in batch.samples:
        keyed.setdefault(dedupe_key(batch.device_id, sample), sample)
    stored = 0
    sleep_added = 0
    async with factory() as session:
        existing: set[str] = set()
        if keyed:
            rows = await session.execute(
                select(HealthSampleRow.dedupe_key).where(
                    HealthSampleRow.dedupe_key.in_(list(keyed))
                )
            )
            existing = set(rows.scalars().all())
        for key, s in keyed.items():
            if key in existing:
                continue
            session.add(
                HealthSampleRow(
                    device_id=batch.device_id,
                    kind=s.kind,
                    start_at=s.start_at,
                    end_at=s.end_at,
                    value=s.value,
                    unit=s.unit,
                    source=s.source,
                    dedupe_key=key,
                )
            )
            stored += 1
            if s.kind == "sleep":
                dup = await session.execute(
                    select(SleepRecordRow.id).where(
                        SleepRecordRow.bed_at == s.start_at,
                        SleepRecordRow.source == "apple_health",
                    )
                )
                if dup.first() is None:
                    session.add(
                        SleepRecordRow(
                            bed_at=s.start_at,
                            wake_at=s.end_at,
                            source="apple_health",
                            duration_minutes=int(round(s.value)),
                            consent_granted=True,
                        )
                    )
                    sleep_added += 1
        await session.commit()
    result = IngestResult(
        received=len(batch.samples),
        stored=stored,
        skipped=len(batch.samples) - stored,
        sleep_records=sleep_added,
    )
    log.info("health.ingest", **result.as_dict())
    return result
