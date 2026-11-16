"""Gap-closure tests for the NerveCenter web audit (audit-gaps.md).

Covers the four repaired defects:

- **S-1**  ``GET /api/systems``  — 200 with live data when the ``systems``
  service is wired; the *precise* 501 when genuinely unwired (truthful banner).
- **OV-1** ``GET /api/overview`` — 200 with real counts when the goals/tasks
  services are wired; 501 when genuinely unwired.
- **AG-1** ``GET /api/agents``   — hardcoded ``agent-1``/``agent-2`` fixture is
  gone; the real 14 native agent types are served; unknown id → 404.
- **U-6 / CC-1** ``GET /api/events/ws`` — the realtime WebSocket route exists
  and degrades honestly (error frame + close) when the event bus is absent.

No live database / NATS is used: services are faked on ``app.state.services``
and the NATS client faked on ``app.state.nats_client`` so the routes are
exercised in isolation.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.services.systems import SystemsService

# ---------------------------------------------------------------------------
# Native agent-type ids the registry must serve (spec §26, 14 types).
# ---------------------------------------------------------------------------
NATIVE_AGENT_TYPES = {
    "planner",
    "repository_inspector",
    "runtime_inspector",
    "log_investigator",
    "infrastructure_inspector",
    "architecture_analyst",
    "incident_correlator",
    "code_reviewer",
    "test_runner",
    "benchmark_agent",
    "research_agent",
    "verifier",
    "context_critic",
    "route_critic",
}

# The two ids from the removed hardcoded fixture — must NOT appear now.
FIXTURE_AGENT_IDS = {"agent-1", "agent-2"}


@pytest.fixture
def app():
    """A fresh app per test (isolated startup/teardown wiring)."""
    return create_app()


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


# ===========================================================================
# AG-1: agents fixture removed, real registry served
# ===========================================================================
class TestAgentsRealRegistry:
    def test_list_agents_serves_real_registry(self, client):
        response = client.get("/api/agents/")
        assert response.status_code == 200
        ids = {a["id"] for a in response.json()["data"]}
        assert ids == NATIVE_AGENT_TYPES, f"unexpected agent ids: {ids}"

    def test_fixture_ids_gone(self, client):
        response = client.get("/api/agents/")
        ids = {a["id"] for a in response.json()["data"]}
        assert FIXTURE_AGENT_IDS.isdisjoint(ids), "hardcoded fixture agents still present"

    def test_get_real_agent_by_type(self, client):
        response = client.get("/agents/planner")
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == "planner"
        assert body["type"] == "planner"
        # Honest: telemetry fields have no backing store → None, not fabricated.
        assert body["lastHeartbeat"] is None
        assert body["cpuUsage"] is None

    def test_get_unknown_agent_404(self, client):
        assert client.get("/agents/agent-1").status_code == 404
        assert client.get("/agents/nope").status_code == 404

    def test_canonical_alias_matches(self, client):
        legacy = client.get("/agents/").json()["data"]
        canonical = client.get("/api/agents").json()["data"]
        assert [a["id"] for a in legacy] == [a["id"] for a in canonical]


# ===========================================================================
# S-1: /api/systems live vs unwired
# ===========================================================================
class TestSystemsEndpoint:
    def test_unwired_returns_precise_501(self, app):
        with TestClient(app) as c:
            # Genuinely unwired: no systems service registered.
            app.state.services = {}
            response = c.get("/api/systems")
        assert response.status_code == 501
        assert response.json()["detail"] == (
            "Systems list endpoint requires the systems service to be wired via app.state.services."
        )

    def test_wired_returns_live_rows(self, client, app):
        # No DB/NATS configured → the service still reports the real
        # nervecenter-api row (200), omitting the absent dependency rows.
        app.state.services = {"systems": SystemsService(None, None)}
        response = client.get("/api/systems")
        assert response.status_code == 200
        body = response.json()
        assert body["meta"]["pagination"]["total"] == len(body["data"])
        names = [s["name"] for s in body["data"]]
        assert "nervecenter-api" in names
        for row in body["data"]:
            assert row["status"] in {"healthy", "unhealthy", "degraded", "unknown"}

    def test_wired_reports_configured_deps(self, client, app, monkeypatch):
        # Fake a configured NATS that reports connected, so a nats row appears.
        class _Nats:
            async def health_check(self):
                return {"connected": True, "server_info": {"version": "2.10"}}

        class _Engine:
            pass

        import backend.persistence.connection as connection

        async def _fake_check_health(engine):
            class _Report:
                ok = True
                status = "healthy"

                def as_dict(self):
                    return {
                        "server_version": "16.2",
                        "pgvector_available": True,
                        "error": None,
                    }

            return _Report()

        monkeypatch.setattr(connection, "check_health", _fake_check_health)
        app.state.services = {"systems": SystemsService(_Engine(), _Nats())}
        response = client.get("/api/systems")
        assert response.status_code == 200
        by_name = {s["name"]: s for s in response.json()["data"]}
        assert by_name["postgres"]["status"] == "healthy"
        assert by_name["nats"]["status"] == "healthy"


# ===========================================================================
# OV-1: /api/overview real vs unwired
# ===========================================================================
class TestOverviewEndpoint:
    def test_unwired_returns_501(self, app):
        with TestClient(app) as c:
            app.state.services = {}
            response = c.get("/api/overview")
        assert response.status_code == 501
        assert "service wiring" in response.json()["detail"]

    def test_wired_returns_real_counts(self, client, app):
        class _FakeGoals:
            async def list_goals(self, *, limit=100, **kw):
                class _G:
                    def model_dump(self, mode="json"):
                        return {"id": "goal-1", "title": "Ship audit", "status": "active",
                                "progress": 0.72, "deadline": None}
                return [_G()]

        class _FakeTasks:
            async def count_by_status(self):
                return {"pending": 2, "in_progress": 1, "completed": 3}

        app.state.services = {"goals": _FakeGoals(), "tasks": _FakeTasks()}
        response = client.get("/api/overview")
        assert response.status_code == 200
        body = response.json()
        # Real counts aggregated from the (fake) services:
        assert body["counts"]["tasks"] == 6
        assert body["counts"]["goals"] == 1
        assert body["counts"]["activeGoals"] == 1
        assert body["taskConcurrency"]["total"] == 6
        assert body["taskConcurrency"]["running"] == 1
        assert body["taskConcurrency"]["pending"] == 2
        assert body["goals"][0]["progress"] == 72
        assert body["goals"][0]["id"] == "goal-1"
        # No backing stores → honest zeros, never fabricated.
        assert body["incidents"] == []
        assert body["computeUtilization"]["totalNodes"] == 0


# ===========================================================================
# N-4: overview counts.agents must equal the /api/agents total
#
# The walk recapture found the Overview "Active Agents" tile (served by
# ``/api/agents``) at a different number than ``/api/overview``
# ``counts.agents``: overview counted only the in-memory 14-native registry
# while the agents endpoint merges registry + persisted instances.
# ``get_overview`` now reuses :func:`agents.list_agents`, so the two must
# stay in lockstep — pinned here so a persisted agent instance can never
# silently re-desync them.
# ===========================================================================
class _FakePersistedAgentService:
    """Minimal stand-in for :class:`AgentService` with N persisted instances."""

    def __init__(self, count: int):
        self._count = count

    async def list_persisted(self):
        return [
            {
                "id": f"agent-{i}",
                "name": f"Persisted {i}",
                "type": "planner",
                "status": "active",
                "currentTask": None,
            }
            for i in range(self._count)
        ]


class TestOverviewAgentsCountMatchesAgentsEndpoint:
    def _wire(self, app, persisted_count: int):
        class _FakeGoals:
            async def list_goals(self, *, limit=100, **kw):
                return []

        class _FakeTasks:
            async def count_by_status(self):
                return {}

        app.state.services = {
            "goals": _FakeGoals(),
            "tasks": _FakeTasks(),
            "agents": _FakePersistedAgentService(persisted_count),
        }

    def test_counts_equal_agents_endpoint_total_zero_persisted(self, client, app):
        self._wire(app, persisted_count=0)
        agent_list = client.get("/api/agents")
        assert agent_list.status_code == 200
        assert len(agent_list.json()["data"]) == 14
        overview = client.get("/api/overview").json()
        assert overview["counts"]["agents"] == 14
        assert len(overview["runningAgents"]) == 14

    def test_counts_equal_agents_endpoint_total_with_persisted(self, client, app):
        # The walk-observed regression shape: 14 native + N persisted.
        self._wire(app, persisted_count=4)
        agent_list = client.get("/api/agents")
        assert agent_list.status_code == 200
        agent_total = len(agent_list.json()["data"])
        assert agent_total == 18  # 14 native registry + 4 persisted instances
        overview = client.get("/api/overview").json()
        assert overview["counts"]["agents"] == agent_total
        assert len(overview["runningAgents"]) == agent_total


# ===========================================================================
# U-6 / CC-1: /api/events/ws route exists + degrades honestly
# ===========================================================================
class _FakeSub:
    def __init__(self):
        self.closed = False

    async def fetch(self, n, timeout=None):
        # No events → forces the heartbeat branch; we only need the first frame.
        raise TimeoutError

    async def close(self):
        self.closed = True


class _FakeJetStream:
    def __init__(self):
        self.sub = _FakeSub()

    async def pull_subscribe(self, subject, *a, **k):
        return self.sub


class _FakeNats:
    is_connected = True

    def __init__(self):
        self._js = _FakeJetStream()

    @property
    def jetstream(self):
        return self._js

    @property
    def config(self):
        class _c:
            events_subject = "nerve.>"

        return _c()


class TestEventsWebSocket:
    def test_no_bus_error_then_close(self, app):
        """Event bus not configured → honest error frame, then the socket closes."""
        with TestClient(app) as c:
            with c.websocket_connect("/api/events/ws") as ws:
                ws.send_text('{"type": "auth", "token": "fake-test-token"}')
                frame = ws.receive_text()
        import json

        parsed = json.loads(frame)
        assert parsed["type"] == "error"
        assert "not configured" in parsed["payload"]["message"]

    def test_connected_bus_status_frame(self, app):
        """Connected bus → status frame; a live event stream is wired."""
        with TestClient(app) as c:
            app.state.nats_client = _FakeNats()
            with c.websocket_connect("/api/events/ws") as ws:
                ws.send_text('{"type": "auth", "token": "fake-test-token"}')
                frame = ws.receive_text()
        import json

        parsed = json.loads(frame)
        assert parsed["type"] == "status"
        assert parsed["payload"]["connected"] is True


# ===========================================================================
# Wiring: the systems service is registered on app.state.services
# ===========================================================================
class TestSystemsWiring:
    def test_systems_service_registered_on_startup(self, app):
        with TestClient(app):
            # Must assert *inside* the context: shutdown teardown clears
            # app.state.services (correct — a later startup rebuilds fresh).
            services = getattr(app.state, "services", {})
            assert "systems" in services
            assert isinstance(services["systems"], SystemsService)
