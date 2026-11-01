"""Focused tests for the durable Decisions endpoints (``/api/decisions``).

Covers the read-only :class:`backend.services.decisions_service.DecisionsService`
and the ``GET /api/decisions`` / ``GET /api/decisions/{id}`` routes against the
real ORM (``decisions`` table), so the assertions are about behaviour, not
stubs:

- **list** returns the durable rows (a seeded ``Decision`` appears);
- **list** on an empty table is an honest empty ``ListResponse`` (200,
  ``data == []``) — never a fabricated record;
- **get** returns a single durable decision;
- **get** with an unknown / malformed id is a stable ``404``;
- the response shape matches the web client contract
  (``web/src/api/client.ts``: ``{ data: [...], meta: {...} }`` for the list, a
  JSON object for the detail).

The DB-session fixture mirrors :mod:`tests.backend.test_evidence`: an in-memory
SQLite async engine with the ``decisions`` table created, and the router wired
to a real ``DecisionsService`` bound to that engine.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.api.endpoints import decisions as decisions_endpoints
from backend.persistence.models.claims import Decision as DecisionORM
from backend.services.decisions_service import DecisionsService

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_engine_cache():
    """Reset the endpoint's module-level engine-keyed service cache.

    The endpoint caches a :class:`DecisionsService` per engine id; clearing it
    between tests keeps each test's in-memory engine isolated from the cache.
    """
    decisions_endpoints._engine_keyed_services.clear()
    yield
    decisions_endpoints._engine_keyed_services.clear()


@pytest.fixture
async def engine():
    """In-memory SQLite async engine with the ``decisions`` table created.

    The ``Decision`` ORM has no foreign keys, so the table can be created
    standalone (matching how :mod:`tests.backend.test_evidence` creates its
    single table on SQLite).
    """
    eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with eng.begin() as conn:
        await conn.run_sync(DecisionORM.__table__.create)
    yield eng
    await eng.dispose()


@pytest.fixture
def svc(engine) -> DecisionsService:
    return DecisionsService(engine)


@pytest.fixture
def api_client(engine) -> TestClient:
    """Isolated FastAPI app with the decisions router wired to a real service."""
    app = FastAPI()
    app.include_router(decisions_endpoints.router)
    app.state.services = {"decisions": DecisionsService(engine)}
    with TestClient(app) as client:
        yield client


async def _seed_decision(
    engine,
    *,
    claim_id: str = "claim-1",
    statement: str = "Take action A",
    mechanism: str = "policy",
    rationale: str | None = "Policy rule matched",
    action: str = "task.schedule",
    supporting_evidence: list[str] | None = None,
    decided_at: datetime | None = None,
) -> str:
    """Insert a durable ``Decision`` row with a fresh id and return that id."""
    decision_id = str(uuid4())
    now = decided_at or datetime.now(UTC)
    sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with sessionmaker() as session:
        session.add(
            DecisionORM(
                id=decision_id,
                created_at=now,
                schema_version="v0",
                hash=None,
                claim_id=claim_id,
                statement=statement,
                mechanism=mechanism,
                rationale=rationale,
                action=action,
                supporting_evidence=supporting_evidence or [],
                planned_change_id=None,
                task_id=None,
                decided_at=now,
            )
        )
        await session.commit()
    return decision_id


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


class TestList:
    async def test_list_returns_durable_rows(self, api_client, engine):
        """Seeded Decisions show up in the list envelope."""
        await _seed_decision(engine, statement="First durable decision")
        await _seed_decision(
            engine,
            statement="Second durable decision",
            action="approve.planned_change",
        )
        await _seed_decision(
            engine, statement="Third durable decision", mechanism="human"
        )

        res = api_client.get("/api/decisions")
        assert res.status_code == 200
        body = res.json()
        # web client contract: { data: [...], meta: {...} }
        assert isinstance(body, dict)
        assert isinstance(body["data"], list)
        assert "meta" in body
        assert body["meta"]["pagination"]["total"] == 3
        assert len(body["data"]) == 3
        statements = {row["statement"] for row in body["data"]}
        assert statements == {
            "First durable decision",
            "Second durable decision",
            "Third durable decision",
        }

    async def test_list_empty_table_is_honest_empty(self, api_client):
        """No rows seeded → 200 with an empty list (no fabrication)."""
        res = api_client.get("/api/decisions")
        assert res.status_code == 200
        body = res.json()
        assert body["data"] == []
        assert body["meta"]["pagination"]["total"] == 0

    async def test_list_shape_matches_web_client(self, api_client, engine):
        """Each row carries the v0 Decision contract fields the view consumes."""
        await _seed_decision(
            engine,
            claim_id="claim-x",
            statement="Durable row",
            mechanism="model_inference",
            rationale="why",
            action="task.schedule",
            supporting_evidence=["ev-1", "ev-2"],
        )
        res = api_client.get("/api/decisions")
        assert res.status_code == 200
        row = res.json()["data"][0]
        assert row["claim_id"] == "claim-x"
        assert row["statement"] == "Durable row"
        assert row["mechanism"] == "model_inference"
        assert row["rationale"] == "why"
        assert row["action"] == "task.schedule"
        assert row["supporting_evidence"] == ["ev-1", "ev-2"]
        assert row["schema_version"] == "v0"
        assert row["id"]
        assert row["created_at"]
        assert row["decided_at"]

    async def test_list_pagination_limit(self, api_client, engine):
        """limit caps the returned page size."""
        await _seed_decision(engine, statement="Row one")
        await _seed_decision(engine, statement="Row two", mechanism="heuristic")
        res = api_client.get("/api/decisions?limit=1&offset=0")
        assert res.status_code == 200
        body = res.json()
        assert len(body["data"]) == 1
        assert body["meta"]["pagination"]["per_page"] == 1
        # With limit=1, the page reports the rows actually returned.
        assert body["meta"]["pagination"]["total"] == 1

    async def test_list_resolves_service_from_db_engine(
        self, engine
    ):
        """Live-deployment path: no explicit service, only ``db_engine`` set.

        Mirrors :func:`backend.api.wiring.install_dependency_wiring`, which
        sets ``app.state.db_engine`` (and never registers a ``decisions`` key).
        The endpoint must build the read-only service over that engine and
        return the durable rows — proving the route is durable, not a stub.
        """
        await _seed_decision(engine, statement="Durable via engine")
        app = FastAPI()
        app.include_router(decisions_endpoints.router)
        app.state.services = {}  # no explicit decisions service
        app.state.db_engine = engine
        with TestClient(app) as client:
            res = client.get("/api/decisions")
        assert res.status_code == 200
        body = res.json()
        assert body["meta"]["pagination"]["total"] == 1
        assert body["data"][0]["statement"] == "Durable via engine"

    async def test_list_degrades_to_empty_when_no_engine(self, engine):
        """No explicit service AND no db_engine → honest empty (never a stub)."""
        app = FastAPI()
        app.include_router(decisions_endpoints.router)
        app.state.services = {}
        app.state.db_engine = None
        with TestClient(app) as client:
            res = client.get("/api/decisions")
        assert res.status_code == 200
        body = res.json()
        assert body["data"] == []
        assert body["meta"]["pagination"]["total"] == 0


# ---------------------------------------------------------------------------
# get one
# ---------------------------------------------------------------------------


class TestGet:
    async def test_get_returns_one_durable_row(self, api_client, engine):
        """A seeded Decision is retrievable by id and returns the full record."""
        decision_id = await _seed_decision(
            engine,
            claim_id="claim-get",
            statement="The one decision",
            mechanism="orchestrator",
            rationale="Orchestrator chose it",
            action="execute.plan",
        )
        res = api_client.get(f"/api/decisions/{decision_id}")
        assert res.status_code == 200
        body = res.json()
        assert body["id"] == decision_id
        assert body["claim_id"] == "claim-get"
        assert body["statement"] == "The one decision"
        assert body["mechanism"] == "orchestrator"
        assert body["action"] == "execute.plan"

    async def test_get_missing_id_is_404(self, api_client, engine):
        """Unknown (valid UUID) id → stable 404, no fabricated record."""
        await _seed_decision(engine)  # table is non-empty
        res = api_client.get("00000000-0000-4000-8000-000000000000")
        assert res.status_code == 404

    async def test_get_malformed_id_is_404(self, api_client, engine):
        """Non-UUID id → 404 (treated as unknown), not a 500."""
        res = api_client.get("not-a-uuid")
        assert res.status_code == 404


# ---------------------------------------------------------------------------
# service (direct)
# ---------------------------------------------------------------------------


class TestService:
    async def test_service_list_empty(self, svc):
        rows = await svc.list_decisions()
        assert rows == []

    async def test_service_get_returns_schema_record(self, svc, engine):
        decision_id = await _seed_decision(engine, mechanism="policy")
        record = await svc.get(decision_id)
        assert record is not None
        assert record.id == decision_id
        assert record.mechanism == "policy"
        # It is a pydantic record, not an ORM row.
        dumped = record.model_dump(mode="json")
        assert dumped["id"] == decision_id

    async def test_service_get_unknown_is_none(self, svc):
        assert await svc.get("11111111-1111-4111-8111-111111111111") is None
        assert await svc.get("garbage") is None
