"""Mental health tracker (NS-ADR-101): forms, encryption, correlations, isolation."""
from __future__ import annotations

import base64
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from backend.api.endpoints import mental_health as ep
from backend.mental_health import correlations as corr
from backend.mental_health import instruments
from backend.persistence.connection import make_session_factory
from backend.persistence.models.bills import BillRow
from backend.persistence.models.health import HealthSampleRow
from backend.persistence.models.mental_health import MhFormRow
from backend.persistence.models.personal import CalendarAccountRow, CalendarEventRow

KEY = "k1:" + base64.b64encode(b"0" * 32).decode()
ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "backend" / "mental_health"
FILES = [*PKG.glob("*.py"), ROOT / "backend/api/endpoints/mental_health.py",
         ROOT / "web/src/views/MentalHealthView.tsx", ROOT / "web/src/api/mentalHealth.ts"]


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with eng.begin() as conn:
        for model in (MhFormRow, HealthSampleRow, BillRow, CalendarAccountRow, CalendarEventRow):
            await conn.run_sync(model.__table__.create)
    yield eng
    await eng.dispose()


@pytest.fixture
def client(engine, monkeypatch):
    monkeypatch.setenv("NERVECENTER_OBJECT_STORE_ENCRYPTION_KEYS", KEY)
    app = FastAPI()
    app.include_router(ep.router)
    app.state.db_engine = engine
    return TestClient(app)


def _form(inst="phq9", answers=None, comment=None, day=None):
    n = len(instruments.INSTRUMENTS[inst].items)
    body = {"instrument": inst, "answers": answers or [1] * n}
    if comment is not None:
        body["comment"] = comment
    if day:
        body["date"] = day
    return body


def test_scoring_and_bands():
    assert instruments.score([3] * 9) == 27
    assert instruments.PHQ9.band(27) == "severe"
    assert instruments.PHQ9.band(4) == "minimal"
    assert instruments.GAD7.band(15) == "severe"
    assert instruments.GAD7.max_score == 21


def test_item_nine_counts_like_any_other_item():
    assert instruments.score([0] * 8 + [3]) == 3


def test_form_saves_and_same_day_resubmit_updates(client):
    r1 = client.post("/api/mental-health/forms", json=_form(answers=[1] * 9, comment="first"))
    assert r1.status_code == 200 and r1.json()["created"] is True and r1.json()["total"] == 9
    r2 = client.post("/api/mental-health/forms", json=_form(answers=[2] * 9, comment="second"))
    assert r2.json()["created"] is False and r2.json()["total"] == 18
    entries = client.get("/api/mental-health/history").json()["entries"]
    assert len(entries) == 1
    assert entries[0]["total"] == 18 and entries[0]["comment"] == "second"
    assert entries[0]["answers"] == [2] * 9
    client.post("/api/mental-health/forms", json=_form("gad7"))
    assert len(client.get("/api/mental-health/history").json()["entries"]) == 2


def test_invalid_answers_rejected(client):
    assert client.post("/api/mental-health/forms", json=_form(answers=[1] * 8)).status_code == 422
    assert client.post("/api/mental-health/forms", json=_form(answers=[4] * 9)).status_code == 422
    assert client.post("/api/mental-health/forms", json=_form("gad7", answers=[1] * 9)).status_code == 422  # noqa: E501
    assert client.post("/api/mental-health/forms",
                       json=_form(comment="x" * 4001)).status_code == 422
    future = (datetime.now(UTC).date() + timedelta(days=5)).isoformat()
    assert client.post("/api/mental-health/forms", json=_form(day=future)).status_code == 422


async def test_stored_values_are_not_plaintext(client, engine):
    secret = "write-in marker 7f3a"
    assert client.post("/api/mental-health/forms",
                       json=_form(answers=[0, 1, 2, 3, 0, 1, 2, 3, 0], comment=secret)).status_code == 200  # noqa: E501
    async with engine.connect() as conn:
        rows = (await conn.execute(text("select answers_enc, comment_enc from mh_forms"))).all()
    assert len(rows) == 1
    answers_enc, comment_enc = rows[0]
    assert secret not in comment_enc and secret not in answers_enc
    assert "[0, 1, 2, 3" not in answers_enc
    assert comment_enc.startswith("mh1.") and answers_enc.startswith("mh1.")


def test_no_key_fails_closed(engine, monkeypatch):
    monkeypatch.delenv("NERVECENTER_OBJECT_STORE_ENCRYPTION_KEYS", raising=False)
    app = FastAPI()
    app.include_router(ep.router)
    app.state.db_engine = engine
    resp = TestClient(app).post("/api/mental-health/forms", json=_form(comment="x"))
    assert resp.status_code == 503


async def test_ciphertext_bound_to_its_row(client, engine):
    client.post("/api/mental-health/forms", json=_form(comment="a", day="2026-01-01"))
    client.post("/api/mental-health/forms", json=_form(comment="b", day="2026-01-02"))
    async with make_session_factory(engine)() as s:
        rows = (await s.execute(select(MhFormRow).order_by(MhFormRow.form_date))).scalars().all()
        rows[0].comment_enc = rows[1].comment_enc
        await s.commit()
    resp = client.get("/api/mental-health/history?days=730")
    assert resp.status_code == 500


# ---- correlations -------------------------------------------------------

def _days(n, start=date(2026, 1, 1)):
    return [start + timedelta(days=i) for i in range(n)]


def test_known_lag_is_recovered():
    import random
    rng = random.Random(7)
    ds = _days(60)
    factor = {d: rng.random() * 10 for d in ds}
    target = {d: 20 - 1.5 * factor[d - timedelta(days=2)] for d in ds[2:]}
    res = corr.lagged_correlations(target, factor)
    assert res["state"] == "ok"
    assert res["lag"] == 2
    assert res["r"] < -0.95
    assert res["n"] == 58
    assert {item["lag"] for item in res["lags"]} == set(range(8))


def test_refuses_below_minimum_samples():
    ds = _days(10)
    series = {d: float(i) for i, d in enumerate(ds)}
    res = corr.lagged_correlations(series, series)
    assert res["state"] == "too_few_samples"
    assert res["r"] is None and res["lag"] is None
    assert res["n"] == 10 and res["min_samples"] == 14


def test_constant_series_gives_no_r():
    ds = _days(30)
    res = corr.lagged_correlations({d: 5.0 for d in ds}, {d: float(i) for i, d in enumerate(ds)})
    assert res["state"] == "too_few_samples"


def test_trendline_change_point_and_streaks():
    ds = _days(30)
    s = {d: (4.0 if i < 15 else 12.0) for i, d in enumerate(ds)}
    assert corr.rolling_mean(s, 7)[-1]["value"] == 12.0
    cp = corr.change_point(s)
    assert cp["date"] == ds[15].isoformat() and cp["shift"] == 8.0
    st = corr.streaks(s, threshold=10)
    assert st["longest"] == 15 and st["current"] == 15
    gap = {ds[0]: 11.0, ds[1]: 11.0, ds[3]: 11.0}
    assert corr.streaks(gap, threshold=10)["longest"] == 2


async def test_endpoint_correlations_and_trends(client, engine):
    ds = _days(30, start=datetime.now(UTC).date() - timedelta(days=29))
    async with make_session_factory(engine)() as s:
        for i, d in enumerate(ds):
            s.add(MhFormRow(form_date=d, instrument="phq9", total_score=i % 10,
                            answers_enc="x", comment_enc=None))
            at = datetime(d.year, d.month, d.day, 23, tzinfo=UTC)
            s.add(HealthSampleRow(device_id="d", kind="step_count", start_at=at - timedelta(hours=1),  # noqa: E501
                                  end_at=at, value=1000.0 + 100 * (i % 10), unit="count",
                                  source="apple_health", dedupe_key=f"k{i}"))
        await s.commit()
    body = client.get("/api/mental-health/correlations?days=60").json()
    assert "cause" in body["note"]
    steps = next(r for r in body["rows"] if r["instrument"] == "phq9" and r["factor"] == "steps")
    assert steps["state"] == "ok" and steps["n"] >= 14 and steps["lag"] is not None
    spend = next(r for r in body["rows"] if r["instrument"] == "phq9" and r["factor"] == "spend_dollars")  # noqa: E501
    assert spend["state"] == "too_few_samples"
    trends = client.get("/api/mental-health/trends?days=60").json()
    assert trends["instruments"]["phq9"]["count"] == 30
    assert trends["instruments"]["gad7"]["latest"] is None


# ---- hard rules ----------------------------------------------------------

FORBIDDEN = re.compile(
    r"crisis|hotline|helpline|lifeline|\b988\b|seek (professional )?help|call (someone|a friend|your doctor)"  # noqa: E501
    r"|reach out to|get help|you are not alone|emergency (services|room)|suicide prevention",
    re.IGNORECASE,
)


def test_no_crisis_wording_in_code_or_fixtures():
    sources = [f for f in FILES if f.exists()]
    assert len(sources) >= 6
    test_fixture = Path(__file__).read_text()
    # the pattern itself lives in this file; check every other string literally
    for f in sources:
        assert not FORBIDDEN.search(f.read_text()), f"crisis wording in {f.name}"
    body = test_fixture.split("FORBIDDEN = re.compile(")[0]
    assert not FORBIDDEN.search(body)


def test_package_imports_no_inference():
    banned = re.compile(r"^\s*(from|import)\s+[\w.]*(inference|omniroute|openai|anthropic|httpx|requests)",  # noqa: E501
                        re.MULTILINE)
    for f in PKG.glob("*.py"):
        assert not banned.search(f.read_text()), f"inference/network import in {f.name}"
    for f in [ROOT / "backend/api/endpoints/mental_health.py"]:
        assert not banned.search(f.read_text())
