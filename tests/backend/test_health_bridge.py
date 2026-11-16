"""Apple Health bridge coverage (NS-ADR-100): auth, caps, idempotency, reads."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from backend.api.endpoints import health_samples as ep
from backend.health import MAX_BATCH_SAMPLES, MAX_BODY_BYTES
from backend.persistence.connection import make_session_factory
from backend.persistence.models.health import HealthSampleRow
from backend.persistence.models.routines import SleepRecordRow

DEVICE = "relay-abc"
TOKEN = "tok-secret"


class FakePlane:
    registry_configured = True

    async def relay_verify_token(self, *, device_id: str, token: str) -> bool:
        return device_id == DEVICE and token == TOKEN


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with eng.begin() as conn:
        await conn.run_sync(HealthSampleRow.__table__.create)
        await conn.run_sync(SleepRecordRow.__table__.create)
    yield eng
    await eng.dispose()


@pytest.fixture
def client(engine):
    app = FastAPI()
    app.include_router(ep.router)
    app.state.db_engine = engine
    app.state.connection_control_plane = FakePlane()
    return TestClient(app)


def _auth(token: str = TOKEN, device: str = DEVICE) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Nerve-Device": device}


def _sample(kind="step_count", unit="count", value=1000.0, hours_ago=3, span=1):
    end = datetime.now(UTC) - timedelta(hours=hours_ago)
    return {
        "kind": kind,
        "start_at": (end - timedelta(hours=span)).isoformat(),
        "end_at": end.isoformat(),
        "value": value,
        "unit": unit,
    }


def _batch(*samples):
    return {"device_id": DEVICE, "samples": list(samples)}


def test_unauthenticated_requests_get_one_generic_401(client, engine):
    bodies = []
    for headers in ({}, _auth(token="wrong"), _auth(device="other"), {"X-Nerve-Device": DEVICE}):
        r = client.post("/api/health/ingest", json=_batch(_sample()), headers=headers)
        assert r.status_code == 401
        bodies.append(r.json())
    assert all(b == bodies[0] for b in bodies)


async def _count(engine, model) -> int:
    async with make_session_factory(engine)() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def test_unauthenticated_stores_nothing(client, engine):
    client.post("/api/health/ingest", json=_batch(_sample()))
    assert await _count(engine, HealthSampleRow) == 0


def test_garbage_body_without_auth_is_401_not_422(client):
    r = client.post("/api/health/ingest", content=b"{not json", headers={})
    assert r.status_code == 401


def test_oversized_body_is_413(client):
    r = client.post("/api/health/ingest", content=b"x" * (MAX_BODY_BYTES + 1), headers=_auth())
    assert r.status_code == 413


def test_too_many_samples_rejected(client):
    r = client.post(
        "/api/health/ingest",
        json=_batch(
            *[_sample(hours_ago=i % 40 + 1, value=float(i)) for i in range(MAX_BATCH_SAMPLES + 1)]
        ),
        headers=_auth(),
    )
    assert r.status_code == 422


@pytest.mark.parametrize(
    "bad",
    [
        _sample(kind="blood_glucose", unit="mg"),
        _sample(unit="kg"),
        _sample(value=-5.0),
        _sample(kind="heart_rate", unit="bpm", value=9999.0),
    ],
)
def test_invalid_samples_rejected(client, bad):
    r = client.post("/api/health/ingest", json=_batch(bad), headers=_auth())
    assert r.status_code == 422


def test_device_mismatch_rejected(client):
    body = {"device_id": "someone-else", "samples": [_sample()]}
    assert client.post("/api/health/ingest", json=body, headers=_auth()).status_code == 422


async def test_ingest_is_idempotent_and_maps_sleep(client, engine):
    batch = _batch(
        _sample(),
        _sample(kind="body_mass", unit="kg", value=82.5),
        _sample(kind="sleep", unit="min", value=430.0, hours_ago=10, span=8),
    )
    first = client.post("/api/health/ingest", json=batch, headers=_auth())
    assert first.status_code == 200
    assert first.json() == {"received": 3, "stored": 3, "skipped": 0, "sleep_records": 1}
    replay = client.post("/api/health/ingest", json=batch, headers=_auth())
    assert replay.json() == {"received": 3, "stored": 0, "skipped": 3, "sleep_records": 0}
    assert await _count(engine, HealthSampleRow) == 3
    assert await _count(engine, SleepRecordRow) == 1
    async with make_session_factory(engine)() as s:
        rec = (await s.execute(select(SleepRecordRow))).scalars().one()
    assert rec.source == "apple_health" and rec.duration_minutes == 430


def test_reads_summarize_and_list(client):
    client.post(
        "/api/health/ingest",
        json=_batch(
            _sample(kind="body_mass", unit="kg", value=80.0, hours_ago=30),
            _sample(kind="body_mass", unit="kg", value=81.0, hours_ago=3),
            _sample(value=500.0, hours_ago=2),
        ),
        headers=_auth(),
    )
    summary = client.get("/api/health/summary?days=7").json()
    assert summary["total_samples"] == 3 and summary["configured"] is True
    assert summary["kinds"]["body_mass"]["count"] == 2
    assert summary["kinds"]["body_mass"]["latest"]["value"] == 81.0
    assert summary["kinds"]["hrv"]["count"] == 0
    assert summary["last_sync_at"]
    listed = client.get("/api/health/samples?kind=body_mass").json()
    assert len(listed["samples"]) == 2


def test_reads_degrade_without_engine():
    app = FastAPI()
    app.include_router(ep.router)
    c = TestClient(app)
    assert c.get("/api/health/summary").json()["configured"] is False
    assert c.get("/api/health/samples").json()["samples"] == []
    r = c.post("/api/health/ingest", json=_batch(_sample()), headers=_auth())
    assert r.status_code == 503
