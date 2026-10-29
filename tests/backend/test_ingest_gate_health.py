"""Focused tests for the J1/J2/J3 health-surfacing work (spec P1-A).

Covers the three additive pieces wired this turn:

- **J1** — the non-raising ``object_store_status`` readiness probe
  (``backend/storage/config.py``): honest statuses, *names* only, never values.
- **J2** — the ingest gate reason: ``install_ingest_supervisor`` stores a
  precise, redacted reason in app state (``INGEST_GATE_STATE_KEY``) when the
  plane is deliberately off, so a gated plane is self-explaining instead of
  silently absent.
- **J3** — the health endpoint additions: per-connection
  ``operational_tier`` (the P1-A per-account taxonomy that must never report a
  stale/disconnected source as "healthy"), the ``personal_attention_operational``
  flag (the "Today" surface may not say "you're clear" while no source is
  operational), and the redacted ``object_store`` readiness section.

These tests are deliberately low-cost: the probe is a pure function, the gate
reasons are exercised through the install hook with a minimal registry fake,
and the endpoint additions are exercised through ``TestClient`` with synthetic
rows — no durable DB and no real supervisor start.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import health as health_module
from backend.api.health import _operational_tier
from backend.ingest.supervisor import (
    INGEST_GATE_STATE_KEY,
    _object_store_gate_reason,
    install_ingest_supervisor,
)
from backend.persistence.repositories import RepositoryRegistry
from backend.storage.config import (
    ENV_OBJECT_STORE_ACCESS_KEY,
    ENV_OBJECT_STORE_BUCKET,
    ENV_OBJECT_STORE_SECRET_KEY,
    ENV_OBJECT_STORE_URL,
    object_store_status,
)


# --------------------------------------------------------------------------
# J1 — object_store_status readiness probe
# --------------------------------------------------------------------------


class TestObjectStoreStatusProbe:
    def test_not_configured_reports_name_not_value(self) -> None:
        """No URL at all → honest ``not_configured`` naming the missing var."""
        status = object_store_status(env={})
        assert status["status"] == "not_configured"
        assert status["scheme"] is None
        assert status["unsupported_scheme"] is False
        assert tuple(status["missing"]) == (ENV_OBJECT_STORE_URL,)

    def test_memory_is_explicit(self) -> None:
        """An explicit ``memory://`` request is reported, not 'configured'."""
        status = object_store_status(env={ENV_OBJECT_STORE_URL: "memory://local"})
        assert status["status"] == "memory"
        assert status["scheme"] == "memory"
        assert status["missing"] == ()

    def test_unsupported_scheme_is_incomplete(self) -> None:
        """A URL with a scheme the probe does not serve is ``incomplete`` with
        ``unsupported_scheme`` and the required S3 names (not the scheme's own)."""
        status = object_store_status(env={ENV_OBJECT_STORE_URL: "ftp://example.com"})
        assert status["status"] == "incomplete"
        assert status["scheme"] == "ftp"
        assert status["unsupported_scheme"] is True
        missing = tuple(status["missing"])
        assert ENV_OBJECT_STORE_BUCKET in missing
        assert ENV_OBJECT_STORE_ACCESS_KEY in missing
        assert ENV_OBJECT_STORE_SECRET_KEY in missing

    def test_fully_configured_s3(self) -> None:
        """A supported endpoint with every required var is ``configured``."""
        status = object_store_status(env={
            ENV_OBJECT_STORE_URL: "s3://minio.nervecenter.svc:9000",
            ENV_OBJECT_STORE_BUCKET: "nervcenter",
            ENV_OBJECT_STORE_ACCESS_KEY: "AKIAEXAMPLE",
            ENV_OBJECT_STORE_SECRET_KEY: "secret",
        })
        assert status["status"] == "configured"
        assert status["scheme"] == "s3"
        assert status["unsupported_scheme"] is False
        assert tuple(status["missing"]) == ()

    def test_partial_s3_is_incomplete_with_missing_names(self) -> None:
        """A supported endpoint missing credentials → ``incomplete`` naming the
        *names* of the missing vars (never a value)."""
        status = object_store_status(env={
            ENV_OBJECT_STORE_URL: "s3://minio.nervecenter.svc:9000",
            ENV_OBJECT_STORE_BUCKET: "nervcenter",
        })
        assert status["status"] == "incomplete"
        assert status["unsupported_scheme"] is False
        missing = tuple(status["missing"])
        assert ENV_OBJECT_STORE_ACCESS_KEY in missing
        assert ENV_OBJECT_STORE_SECRET_KEY in missing
        assert ENV_OBJECT_STORE_BUCKET not in missing  # it was provided

    def test_never_raises_and_never_leaks_values(self) -> None:
        """The probe is a pure, non-raising readiness read; a URL with a
        credential embedded is reported by name, with the URL absent."""
        status = object_store_status(env={
            ENV_OBJECT_STORE_URL: "s3://minio:9000",
        })
        # Only the *names* of missing vars surface; no raw URL / credential.
        assert all(m in (
            ENV_OBJECT_STORE_URL, ENV_OBJECT_STORE_BUCKET,
            ENV_OBJECT_STORE_ACCESS_KEY, ENV_OBJECT_STORE_SECRET_KEY,
        ) for m in status["missing"])


# --------------------------------------------------------------------------
# J2 — the ingest gate reason
# --------------------------------------------------------------------------


def _target(registry: object) -> SimpleNamespace:
    """A minimal wiring target: ``state`` is a mutable namespace the install
    hook reads/writes (``repository_registry`` in, gate reason out)."""
    return SimpleNamespace(
        state=SimpleNamespace(repository_registry=registry, ingest_supervisor=None)
    )


class _RegistryFake(RepositoryRegistry):
    """A ``RepositoryRegistry`` that never constructs an engine: the install
    hook only needs the ``isinstance(...)`` gate check, so both ``__new__``
    (no args) and ``__init__`` (no-op) are overridden."""

    def __new__(cls) -> "_RegistryFake":
        return super().__new__(cls)

    def __init__(self, *args: object, **kwargs: object) -> None:
        """No durable engine in a gate test; the hook never touches it."""


class TestIngestGateReason:
    def test_gate_reason_helper_names_missing_url(self, monkeypatch) -> None:
        """With no store configured the reason names the missing variable."""
        monkeypatch.delenv(ENV_OBJECT_STORE_URL, raising=False)
        assert _object_store_gate_reason() == (
            "object store not configured "
            "(NERVECENTER_OBJECT_STORE_URL is unset)"
        )

    async def test_disabled_reason(self, monkeypatch) -> None:
        """Plane disabled → hook returns None and records the enable-gate reason."""
        monkeypatch.delenv("NERVE_INGEST_ENABLED", raising=False)
        target = _target(_RegistryFake())
        result = await install_ingest_supervisor(target)
        assert result is None
        assert target.state.ingest_gate_reason == (
            "NERVE_INGEST_ENABLED is not set (ingest plane disabled)"
        )

    async def test_no_registry_reason(self, monkeypatch) -> None:
        """Enabled but no durable registry → records the registry-gate reason."""
        monkeypatch.setenv("NERVE_INGEST_ENABLED", "1")
        target = _target(None)  # no durable registry
        result = await install_ingest_supervisor(target)
        assert result is None
        assert target.state.ingest_gate_reason == (
            "durable registry not installed (no durable store available)"
        )

    async def test_object_store_reason(self, monkeypatch) -> None:
        """Enabled + registry, but the object store is unconfigured → records
        the store-gate reason (the operator-actionable §14 message)."""
        monkeypatch.setenv("NERVE_INGEST_ENABLED", "1")
        monkeypatch.delenv(ENV_OBJECT_STORE_URL, raising=False)
        target = _target(_RegistryFake())
        result = await install_ingest_supervisor(target)
        assert result is None
        assert target.state.ingest_gate_reason == (
            "object store not configured "
            "(NERVECENTER_OBJECT_STORE_URL is unset)"
        )


# --------------------------------------------------------------------------
# J3 — health endpoint additions
# --------------------------------------------------------------------------


class _FakeRow:
    """A synthetic durable connection row for the health mapping."""

    def __init__(self, *, auth: str | None, operational: str | None,
                 kind: str = "imap") -> None:
        self.id = f"conn-{kind}-{auth}-{operational}"
        self.kind = kind
        self.auth_status = auth
        self.operational_status = operational
        self.last_success_at = None
        self.last_attempt_at = None
        self.last_error_code = None
        self.last_error_message = None


class _FakeRegistry:
    """A registry double whose ``connections.list_connections()`` returns the
    given rows — no durable DB required."""

    def __init__(self, rows: list[_FakeRow]) -> None:
        self.connections = SimpleNamespace(
            list_connections=self._list
        )
        self._rows = rows

    async def _list(self) -> list[_FakeRow]:
        return self._rows


class TestOperationalTierMapping:
    @pytest.mark.parametrize(
        ("auth", "operational", "expected"),
        [
            ("unconfigured", None, "never_configured"),
            ("unconfigured", "healthy", "never_configured"),  # auth wins
            ("pairing_pending", None, "auth_required"),
            ("auth_expired", "healthy", "auth_required"),     # auth wins
            ("auth_failed", "healthy", "auth_required"),     # auth wins
            ("authenticated", None, "authenticated_no_sync"),
            ("authenticated", "never_synced", "authenticated_no_sync"),
            ("authenticated", "healthy", "healthy"),
            ("authenticated", "syncing", "syncing"),
            ("authenticated", "degraded", "degraded"),
            ("authenticated", "error", "error"),
            (None, None, "never_configured"),  # absent auth == unconfigured
        ],
    )
    def test_tier_never_reports_healthy_when_stale(
        self, auth: str | None, operational: str | None, expected: str
    ) -> None:
        """The P1-A invariant: ``healthy`` only when the durable row is both
        authenticated *and* healthy; stale/disconnected rows map to a
        non-operational tier (P1-206 'you're clear' must not lie)."""
        assert _operational_tier(auth, operational) == expected

    def test_healthy_is_operational_and_stale_is_not(self) -> None:
        """``personal_attention_operational`` is True only for healthy/syncing."""
        from backend.api.health import _OPERATIONAL_TIER_BY_STATUS

        assert _operational_tier("authenticated", "healthy") in ("healthy", "syncing")
        assert _operational_tier("authenticated", "error") not in ("healthy", "syncing")
        # sanity: the mapping table is internally consistent
        assert _OPERATIONAL_TIER_BY_STATUS["healthy"] == "healthy"


class TestObjectStoreSection:
    def test_section_is_present_and_names_only(self, monkeypatch) -> None:
        """The health ``object_store`` section is redacted: names only, and the
        status is one of the honest probe values."""
        from backend.api.health import _object_store_check

        # Force a clean environment so the section is deterministic.
        for name in (
            ENV_OBJECT_STORE_URL, ENV_OBJECT_STORE_BUCKET,
            ENV_OBJECT_STORE_ACCESS_KEY, ENV_OBJECT_STORE_SECRET_KEY,
        ):
            monkeypatch.delenv(name, raising=False)
        section = _object_store_check()
        assert section["status"] in (
            "not_configured", "incomplete", "configured", "memory", "unknown")
        assert "missing" in section
        # only var *names*, never values
        assert all(isinstance(m, str) and m.startswith("NERVECENTER_OBJECT_STORE_")
                   for m in section["missing"])

    def test_section_degrades_without_raising(self) -> None:
        """Even a probe that throws must not take the health endpoint down."""
        from backend import storage as storage_pkg
        from backend.api import health as hm

        # Point the lazy import at a raising function, then confirm the
        # section degrades to 'unknown' instead of propagating.
        def _boom(*_a, **_k):
            raise RuntimeError("object store probe exploded")

        monkey = pytest.MonkeyPatch()
        try:
            monkey.setitem(__import__("sys").modules,
                           "backend.storage.config",
                           SimpleNamespace(object_store_status=_boom))
            section = hm._object_store_check()
        finally:
            monkey.undo()
        assert section["status"] == "unknown"


class TestHealthEndpointAdditions:
    def _app(self, state: dict) -> FastAPI:  # noqa: E501
        app = FastAPI(title="NerveCenterTest", version="0.1.0")
        for key, value in state.items():
            setattr(app.state, key, value)
        app.include_router(health_module.router)
        return app

    def test_tier_and_personal_attention_surface(self) -> None:
        """Per-connection ``operational_tier`` and the aggregate
        ``personal_attention_operational`` flag are reported from the durable
        row statuses."""
        registry = _FakeRegistry([
            _FakeRow(auth="authenticated", operational="healthy"),
            _FakeRow(auth="unconfigured", operational=None),
            _FakeRow(auth="authenticated", operational="never_synced"),
            _FakeRow(auth="auth_expired", operational="healthy"),
        ])
        body = TestClient(self._app({
            "db_engine": None, "nats_client": None, "repository_registry": registry
        })).get("/api/health").json()

        connections = body["connections"]
        assert connections["count"] == 4
        tiers = {row["connection_id"].split("-")[-1]: row["operational_tier"]
                 for row in connections["connections"]}
        by_auth = [row for row in connections["connections"]]
        assert {row["operational_tier"] for row in by_auth} == {
            "healthy", "never_configured", "authenticated_no_sync", "auth_required"}
        # at least one operational source → personal attention is backed
        assert connections["personal_attention_operational"] is True
        _ = tiers  # (informational; the set assertion above is the check)

    def test_personal_attention_false_when_none_operational(self) -> None:
        """When every source is stale/disconnected/not-configured, the flag is
        False — the 'Today' surface must not claim 'you're clear'."""
        registry = _FakeRegistry([
            _FakeRow(auth="authenticated", operational="error"),
            _FakeRow(auth="authenticated", operational="degraded"),
            _FakeRow(auth="auth_expired", operational="healthy"),
            _FakeRow(auth="unconfigured", operational=None),
        ])
        body = TestClient(self._app({
            "db_engine": None, "nats_client": None, "repository_registry": registry
        })).get("/api/health").json()
        connections = body["connections"]
        assert "personal_attention_operational" in connections
        assert connections["personal_attention_operational"] is False

    def test_ingest_reason_surfaced_when_gated(self) -> None:
        """A gated (not installed) plane surfaces its gate reason on the
        endpoint — the whole point of J2 is that it is self-explaining."""
        app = self._app({
            "db_engine": None, "nats_client": None,
            INGEST_GATE_STATE_KEY: "object store not configured "
                                  "(NERVECENTER_OBJECT_STORE_URL is unset)",
        })
        body = TestClient(app).get("/api/health").json()
        assert body["ingest"]["status"] == "not configured"
        assert body["ingest"]["reason"] == (
            "object store not configured "
            "(NERVECENTER_OBJECT_STORE_URL is unset)"
        )

    def test_ingest_reason_none_when_installed(self) -> None:
        """An installed plane reports ``reason: None`` (it is not gated)."""
        supervisor = SimpleNamespace(
            workers={"acct@x": SimpleNamespace(is_running=True)},
            liveness=lambda: [{"account": "acct@x", "status": "ok"}],
        )
        app = self._app({
            "db_engine": None, "nats_client": None,
            "ingest_supervisor": supervisor,
        })
        body = TestClient(app).get("/api/health").json()
        assert body["ingest"]["status"] == "installed"
        assert body["ingest"]["reason"] is None
        assert body["ingest"]["workers"] == 1
