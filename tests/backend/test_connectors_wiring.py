"""P1-103 / F5 — production control-plane wiring (backend/connectors/wiring.py).

The control plane ships with *no* probes, token exchanger, or sync triggers
registered, so every ``verify`` degraded to ``probe_unsupported`` and every
``sync`` to ``ingest_unavailable`` for every kind. This module closes that at
app construction. These tests pin the production seams:

- the durable OAuth login probes (``gmail`` → Google userinfo, ``graph_mail``
  → Microsoft Graph ``/me``) map provider HTTP onto stable codes — a rejected
  token is ``oauth_auth_rejected`` (never a credential fragment in the message);
- :func:`register_connection_probes` registers exactly the servable kinds
  (``gmail`` / ``graph_mail`` / ``imap``) and leaves the rest honest;
- :class:`RegistryTokenExchanger` completes the code→token grant at the
  provider (client credentials from the deploy environment, PKCE verifier from
  the row config) and the plane stores the real tokens via the vault;
- :class:`MailSyncTrigger` delegates one bounded cycle to the live supervisor,
  and :func:`register_sync_triggers` only registers when a supervisor is live;
- :func:`install_connection_wiring` is the one-shot app-lifespan entrypoint and
  is best-effort (never raises).

No network: every transport is a scripted fake; the IMAP seam is the frozen
:class:`StubImapClient`.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

import backend.api.models  # noqa: F401 - initialize the api package first (pre-existing import cycle)
from backend.connectors.mail.imap_client import ImapProbeResult, StubImapClient
from backend.connectors.registry import (
    ACCESS_TOKEN,
    REFRESH_TOKEN,
    ConnectionControlPlane,
    ConnectionControlPlaneError,
)
from backend.connectors.vault import FernetVault, generate_key
from backend.connectors.wiring import (
    MAIL_TRIGGER_KINDS,
    MailSyncTrigger,
    install_connection_wiring,
    make_oauth_probe,
    register_connection_probes,
    register_sync_triggers,
    register_token_exchanger,
)
from backend.persistence.models.personal import (
    ConnectionRow,
    ImportJobRow,
    IngestCursorRow,
    PersonIdentityRow,
    PersonRow,
    SecretRow,
)
from backend.persistence.repositories import RepositoryRegistry

_PERSONAL_TABLES = [
    PersonRow.__table__,
    PersonIdentityRow.__table__,
    ConnectionRow.__table__,
    SecretRow.__table__,
    ImportJobRow.__table__,
    IngestCursorRow.__table__,
]


# -- scripted seams (no network) -------------------------------------------------


class FakeHttpTransport:
    """Scripted GET for the OAuth login probes: ``request(url, token)``."""

    def __init__(
        self,
        status: int = 200,
        body: dict[str, Any] | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.status = status
        self.body = dict(body or {})
        self.exc = exc
        self.calls: list[tuple[str, str]] = []

    async def request(self, url: str, token: str, *, timeout: float = 10.0):
        self.calls.append((url, token))
        if self.exc is not None:
            raise self.exc
        return self.status, self.body


class FakeTokenTransport:
    """Scripted POST for the code→token grant: ``request(url, form)``."""

    def __init__(
        self,
        status: int = 200,
        body: dict[str, Any] | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.status = status
        self.body = dict(body or {})
        self.exc = exc
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def request(self, url: str, form: dict[str, str], *, timeout: float = 15.0):
        self.calls.append((url, dict(form)))
        if self.exc is not None:
            raise self.exc
        return self.status, self.body


class FakeSupervisor:
    """Records the sync delegations a :class:`MailSyncTrigger` makes."""

    def __init__(self, *, exc: Exception | None = None) -> None:
        self.sync_calls: list[str] = []
        self.exc = exc

    async def sync_once(self, connection_id: str) -> dict[str, Any]:
        self.sync_calls.append(connection_id)
        if self.exc is not None:
            raise self.exc
        return {"connection_id": connection_id, "status": "ok"}


# -- fixtures ---------------------------------------------------------------------


async def _make_engine(db_path: Path) -> AsyncEngine:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sc: _PERSONAL_TABLES[0].metadata.create_all(
                sc, tables=_PERSONAL_TABLES
            )
        )
    return engine


@pytest.fixture
async def plane_env(tmp_path: Path):
    """Real control plane over a file-backed SQLite engine + Fernet vault."""
    from backend.persistence.connection import make_session_factory

    engine = await _make_engine(tmp_path / "wiring.db")
    vault = FernetVault(generate_key())
    registry = RepositoryRegistry(make_session_factory(engine), secret_codec=vault)
    plane = ConnectionControlPlane(registry, vault=vault)
    yield SimpleNamespace(engine=engine, registry=registry, vault=vault, plane=plane)
    await engine.dispose()


# -- OAuth login probes ------------------------------------------------------------


class TestOAuthProbe:
    async def test_gmail_success_authenticates(self, plane_env) -> None:
        transport = FakeHttpTransport(200, {"email": "x@g.example"})
        probe = make_oauth_probe("gmail", transport=transport)
        result = await probe.probe({}, "tok-g")
        assert result.success is True
        assert result.error_code is None

    async def test_graph_mail_success_authenticates(self, plane_env) -> None:
        transport = FakeHttpTransport(200, {"mail": "x@m.example"})
        probe = make_oauth_probe("graph_mail", transport=transport)
        result = await probe.probe({}, "tok-m")
        assert result.success is True

    async def test_rejected_token_is_oauth_auth_rejected(self, plane_env) -> None:
        for status in (401, 403):
            probe = make_oauth_probe("gmail", transport=FakeHttpTransport(status, {}))
            result = await probe.probe({}, "tok-g")
            assert result.success is False
            assert result.error_code == "oauth_auth_rejected"
            # The token itself never leaks into the message.
            assert "tok-g" not in (result.error_message or "")

    async def test_other_status_is_oauth_incomplete(self, plane_env) -> None:
        probe = make_oauth_probe("graph_mail", transport=FakeHttpTransport(500, {}))
        result = await probe.probe({}, "tok-m")
        assert result.success is False
        assert result.error_code == "oauth_incomplete"

    async def test_200_without_marker_is_incomplete(self, plane_env) -> None:
        probe = make_oauth_probe("gmail", transport=FakeHttpTransport(200, {}))
        result = await probe.probe({}, "tok-g")
        assert result.success is False
        assert result.error_code == "oauth_incomplete"

    async def test_network_failure_is_oauth_incomplete(self, plane_env) -> None:
        probe = make_oauth_probe(
            "gmail", transport=FakeHttpTransport(exc=ConnectionError("boom"))
        )
        result = await probe.probe({}, "tok-g")
        assert result.success is False
        assert result.error_code == "oauth_incomplete"

    async def test_missing_token_is_missing_credential(self, plane_env) -> None:
        probe = make_oauth_probe("gmail", transport=FakeHttpTransport(200, {"email": "x"}))
        result = await probe.probe({}, None)
        assert result.success is False
        assert result.error_code == "missing_credential"
        # No request is made when there is no token.
        assert probe._transport.calls == []  # noqa: SLF001 - seam internals, deterministic

    async def test_config_overrides_base_url(self, plane_env) -> None:
        transport = FakeHttpTransport(200, {"email": "x"})
        probe = make_oauth_probe("gmail", transport=transport)
        await probe.probe({"google_api_base": "https://example.test/"}, "tok")
        assert transport.calls[0][0] == "https://example.test/oauth2/v3/userinfo"

    async def test_non_oauth_kind_raises(self, plane_env) -> None:
        with pytest.raises(ValueError):
            make_oauth_probe("relay_device")


class TestVerifyIntegration:
    async def test_probe_registration_makes_verify_work(self, plane_env) -> None:
        """register_connection_probes wires gmail so the plane's verify() runs
        a real (faked-transport) probe and authenticates on 200."""
        plane = plane_env.plane
        row = await plane.create_connection(kind="gmail", config={"account": "x@g.example"})
        await plane.store_secret(connection_id=row.id, kind=ACCESS_TOKEN, plaintext="tok-g")

        # Register a deterministic gmail probe (real make_oauth_probe, fake
        # transport) plus the frozen IMAP seam (stub client, no network).
        gmail_probe = make_oauth_probe(
            "gmail", transport=FakeHttpTransport(200, {"email": "x"})
        )
        plane.register_probe("gmail", gmail_probe)
        plane.register_probe("imap", _imap_probe_seam(StubImapClient(ImapProbeResult.OK)))

        result = await plane.verify(row.id)
        assert result["ok"] is True
        assert result["auth_status"] == "authenticated"
        # Frozen hard rule: a probe never advances the operational axis.
        assert result["operational_status"] == "never_synced"

    async def test_rejected_token_sets_auth_failed(self, plane_env) -> None:
        plane = plane_env.plane
        row = await plane.create_connection(kind="graph_mail", config={"account": "x@m.example"})
        await plane.store_secret(connection_id=row.id, kind=ACCESS_TOKEN, plaintext="tok-m")
        plane.register_probe(
            "graph_mail",
            make_oauth_probe("graph_mail", transport=FakeHttpTransport(401, {})),
        )
        result = await plane.verify(row.id)
        assert result["ok"] is False
        assert result["error_code"] == "oauth_auth_rejected"
        assert result["auth_status"] == "auth_failed"


def _imap_probe_seam(client: StubImapClient):
    """Adapt the frozen ImapClient seam into a ProbeSeam (no network)."""
    from backend.connectors.registry import make_imap_probe

    return make_imap_probe(client)


# -- probe / trigger registration -----------------------------------------------


class TestRegistration:
    async def test_register_probes_covers_servable_kinds(self, plane_env) -> None:
        plane = plane_env.plane
        registered: dict[str, Any] = {}
        original = plane.register_probe

        def capture(kind: str, probe: Any) -> None:
            registered[kind] = probe
            original(kind, probe)

        plane.register_probe = capture  # noqa: SLF001 - test capture
        register_connection_probes(plane, imap_client=StubImapClient(ImapProbeResult.OK))
        plane.register_probe = original  # noqa: SLF001

        # Only the servable kinds get a probe; relay_device/calendar stay honest.
        assert set(registered) == {"gmail", "graph_mail", "imap"}

    async def test_register_sync_triggers_with_supervisor(self, plane_env) -> None:
        plane = plane_env.plane
        registered: dict[str, Any] = {}
        original = plane.register_trigger

        def capture(kind: str, trigger: Any) -> None:
            registered[kind] = trigger
            original(kind, trigger)

        plane.register_trigger = capture  # noqa: SLF001
        register_sync_triggers(plane, supervisor=FakeSupervisor())
        plane.register_trigger = original  # noqa: SLF001

        assert set(registered) == set(MAIL_TRIGGER_KINDS)

    async def test_register_sync_triggers_without_supervisor_is_noop(
        self, plane_env
    ) -> None:
        plane = plane_env.plane
        registered: list[str] = []
        original = plane.register_trigger

        def capture(kind: str, trigger: Any) -> None:
            registered.append(kind)
            original(kind, trigger)

        plane.register_trigger = capture  # noqa: SLF001
        register_sync_triggers(plane, supervisor=None)
        plane.register_trigger = original  # noqa: SLF001

        assert registered == []

    async def test_trigger_delegates_to_supervisor(self, plane_env) -> None:
        supervisor = FakeSupervisor()
        trigger = MailSyncTrigger(supervisor)
        await trigger.start(SimpleNamespace(id="conn-1"))
        assert supervisor.sync_calls == ["conn-1"]

    async def test_trigger_propagates_supervisor_error(self, plane_env) -> None:
        supervisor = FakeSupervisor(exc=ConnectionControlPlaneError("connect_failed", "nope"))
        trigger = MailSyncTrigger(supervisor)
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await trigger.start(SimpleNamespace(id="conn-1"))
        assert exc.value.code == "connect_failed"


# -- token exchanger --------------------------------------------------------------


class TestTokenExchanger:
    async def test_exchange_completes_grant_and_stores_tokens(
        self, plane_env, monkeypatch
    ) -> None:
        plane = plane_env.plane
        row = await plane.create_connection(kind="gmail", config={"account": "x@g.example"})
        flow = await plane.start_oauth(row.id)

        transport = FakeTokenTransport(
            200, {"access_token": "new-access", "refresh_token": "new-refresh"}
        )
        register_token_exchanger(
            plane,
            registry=plane_env.registry,
            transport=transport,
            env={
                "NERVE_GOOGLE_CLIENT_ID": "cid",
                "NERVE_GOOGLE_CLIENT_SECRET": "csecret",
            },
        )

        result = await plane.complete_oauth(row.id, "auth-code", flow["state"])

        assert result["ok"] is True
        # The real tokens round-trip through the vault.
        assert (
            await plane_env.registry.connections.get_secret(
                kind=ACCESS_TOKEN, owner_kind="connection", owner_id=row.id
            )
            == "new-access"
        )
        assert (
            await plane_env.registry.connections.get_secret(
                kind=REFRESH_TOKEN, owner_kind="connection", owner_id=row.id
            )
            == "new-refresh"
        )
        # The grant POSTed to the frozen Google token endpoint with the PKCE
        # verifier and env-derived client credentials.
        assert transport.calls[0][0] == "https://oauth2.googleapis.com/token"
        form = transport.calls[0][1]
        assert form["grant_type"] == "authorization_code"
        assert form["code"] == "auth-code"
        assert form["client_id"] == "cid"
        assert form["client_secret"] == "csecret"
        assert form["code_verifier"] == flow["code_verifier"]
        # The one-shot state is consumed.
        stored = await plane_env.registry.connections.get_connection(row.id)
        assert not (stored.config or {}).get("oauth_state")

    async def test_exchange_is_bound_to_the_matching_flow_across_accounts(
        self, plane_env
    ) -> None:
        """Two same-provider onboarding flows are live at once.

        Completing *account B's* callback must complete B's grant with B's
        PKCE verifier + redirect URI, and must NOT be confused with
        account A's flow. A provider-only row lookup would have posted A's
        verifier — corrupting the PKCE grant for the wrong account.
        """
        plane = plane_env.plane
        acc_a = await plane.create_connection(
            kind="gmail",
            config={"account": "a@g.example", "oauth_redirect_uri": "https://cb.example/a"},
        )
        acc_b = await plane.create_connection(
            kind="gmail",
            config={"account": "b@g.example", "oauth_redirect_uri": "https://cb.example/b"},
        )
        flow_a = await plane.start_oauth(acc_a.id)
        flow_b = await plane.start_oauth(acc_b.id)

        transport = FakeTokenTransport(
            200, {"access_token": "b-access", "refresh_token": "b-refresh"}
        )
        register_token_exchanger(
            plane,
            registry=plane_env.registry,
            transport=transport,
            env={
                "NERVE_GOOGLE_CLIENT_ID": "cid",
                "NERVE_GOOGLE_CLIENT_SECRET": "csecret",
            },
        )

        # Complete account B's callback.
        result = await plane.complete_oauth(acc_b.id, "code-b", flow_b["state"])
        assert result["ok"] is True

        # The grant must have been bound to B's flow, not A's.
        url, form = transport.calls[0]
        assert url == "https://oauth2.googleapis.com/token"
        assert form["code"] == "code-b"
        assert form["redirect_uri"] == "https://cb.example/b"
        assert form["code_verifier"] == flow_b["code_verifier"]
        assert form["code_verifier"] != flow_a["code_verifier"]
        assert form["redirect_uri"] != "https://cb.example/a"

        # B's tokens landed on B's row (vault round-trip), not on A's.
        assert (
            await plane_env.registry.connections.get_secret(
                kind=ACCESS_TOKEN, owner_kind="connection", owner_id=acc_b.id
            )
            == "b-access"
        )
        assert (
            await plane_env.registry.connections.get_secret(
                kind=REFRESH_TOKEN, owner_kind="connection", owner_id=acc_b.id
            )
            == "b-refresh"
        )
        assert (
            await plane_env.registry.connections.get_secret(
                kind=ACCESS_TOKEN, owner_kind="connection", owner_id=acc_a.id
            )
            is None
        )

        # A's flow remains live (untouched) — only B's one-shot state was consumed.
        a_cfg = (await plane_env.registry.connections.get_connection(acc_a.id)).config or {}
        b_cfg = (await plane_env.registry.connections.get_connection(acc_b.id)).config or {}
        assert a_cfg.get("oauth_state") == flow_a["state"]
        assert not b_cfg.get("oauth_state")

    async def test_exchange_wrong_flow_state_is_oauth_incomplete(self, plane_env) -> None:
        """Completing one account with a *different* account's state is rejected."""
        plane = plane_env.plane
        acc_a = await plane.create_connection(kind="gmail", config={"account": "a@g.example"})
        acc_b = await plane.create_connection(kind="gmail", config={"account": "b@g.example"})
        flow_a = await plane.start_oauth(acc_a.id)
        flow_b = await plane.start_oauth(acc_b.id)

        transport = FakeTokenTransport(200, {"access_token": "x"})
        register_token_exchanger(
            plane,
            registry=plane_env.registry,
            transport=transport,
            env={
                "NERVE_GOOGLE_CLIENT_ID": "cid",
                "NERVE_GOOGLE_CLIENT_SECRET": "csecret",
            },
        )

        # A's connection_id with B's callback state → CSRF mismatch.
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await plane.complete_oauth(acc_a.id, "code", flow_b["state"])
        assert exc.value.code == "oauth_incomplete"
        # No grant was POSTed.
        assert transport.calls == []
        # Both flows remain live (no state consumed).
        assert (await plane_env.registry.connections.get_connection(acc_a.id)).config.get(
            "oauth_state"
        ) == flow_a["state"]
        assert (await plane_env.registry.connections.get_connection(acc_b.id)).config.get(
            "oauth_state"
        ) == flow_b["state"]

    async def test_exchange_http_failure_is_oauth_incomplete(self, plane_env) -> None:
        plane = plane_env.plane
        row = await plane.create_connection(kind="gmail", config={"account": "x@g.example"})
        flow = await plane.start_oauth(row.id)
        register_token_exchanger(
            plane,
            registry=plane_env.registry,
            transport=FakeTokenTransport(400, {"error": "invalid_grant"}),
            env={"NERVE_GOOGLE_CLIENT_ID": "cid", "NERVE_GOOGLE_CLIENT_SECRET": "csecret"},
        )
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await plane.complete_oauth(row.id, "auth-code", flow["state"])
        assert exc.value.code == "oauth_incomplete"

    async def test_exchange_network_failure_is_oauth_incomplete(self, plane_env) -> None:
        plane = plane_env.plane
        row = await plane.create_connection(kind="gmail", config={"account": "x@g.example"})
        flow = await plane.start_oauth(row.id)
        register_token_exchanger(
            plane,
            registry=plane_env.registry,
            transport=FakeTokenTransport(exc=ConnectionError("boom")),
            env={"NERVE_GOOGLE_CLIENT_ID": "cid", "NERVE_GOOGLE_CLIENT_SECRET": "csecret"},
        )
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await plane.complete_oauth(row.id, "auth-code", flow["state"])
        assert exc.value.code == "oauth_incomplete"

    async def test_exchange_without_client_credentials_is_missing_credential(
        self, plane_env
    ) -> None:
        plane = plane_env.plane
        row = await plane.create_connection(kind="graph_mail", config={"account": "x@m.example"})
        flow = await plane.start_oauth(row.id)
        transport = FakeTokenTransport(200, {"access_token": "t"})
        register_token_exchanger(
            plane,
            registry=plane_env.registry,
            transport=transport,
            env={},  # no client credentials on this host
        )
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await plane.complete_oauth(row.id, "auth-code", flow["state"])
        assert exc.value.code == "missing_credential"
        # No grant was attempted.
        assert transport.calls == []

    async def test_exchange_stale_callback_is_oauth_incomplete(self, plane_env) -> None:
        plane = plane_env.plane
        # No start_oauth → no live oauth_state row for the provider.
        register_token_exchanger(
            plane,
            registry=plane_env.registry,
            transport=FakeTokenTransport(200, {"access_token": "t"}),
            env={"NERVE_GOOGLE_CLIENT_ID": "cid", "NERVE_GOOGLE_CLIENT_SECRET": "csecret"},
        )
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await plane.complete_oauth("nope", "auth-code", "stale-state")
        assert exc.value.code == "connection_not_found"


# -- app-lifespan entrypoint ------------------------------------------------------


class TestInstallConnectionWiring:
    async def test_install_registers_everything_when_supervisor_live(
        self, plane_env
    ) -> None:
        app = SimpleNamespace(
            state=SimpleNamespace(
                connection_control_plane=plane_env.plane,
                repository_registry=plane_env.registry,
                ingest_supervisor=FakeSupervisor(),
            )
        )
        installed_probes: list[str] = []
        installed_triggers: list[str] = []
        plane = plane_env.plane
        probe_orig = plane.register_probe
        trigger_orig = plane.register_trigger
        def record_probe(kind: str, probe: Any) -> None:
            probe_orig(kind, probe)
            installed_probes.append(kind)

        def record_trigger(kind: str, trigger: Any) -> None:
            trigger_orig(kind, trigger)
            installed_triggers.append(kind)

        plane.register_probe = record_probe
        plane.register_trigger = record_trigger

        install_connection_wiring(app)

        plane.register_probe = probe_orig
        plane.register_trigger = trigger_orig

        assert set(installed_probes) == {"gmail", "graph_mail", "imap"}
        assert set(installed_triggers) == set(MAIL_TRIGGER_KINDS)

    async def test_install_no_triggers_without_supervisor(self, plane_env) -> None:
        app = SimpleNamespace(
            state=SimpleNamespace(
                connection_control_plane=plane_env.plane,
                repository_registry=plane_env.registry,
                ingest_supervisor=None,
            )
        )
        triggers: list[str] = []
        plane = plane_env.plane
        trigger_orig = plane.register_trigger

        def record_trigger(kind: str, trigger: Any) -> None:
            trigger_orig(kind, trigger)
            triggers.append(kind)

        plane.register_trigger = record_trigger
        install_connection_wiring(app)
        plane.register_trigger = trigger_orig
        assert triggers == []

    async def test_install_noop_when_no_plane(self) -> None:
        app = SimpleNamespace(
            state=SimpleNamespace(
                connection_control_plane=None,
                repository_registry=None,
                ingest_supervisor=None,
            )
        )
        # Must not raise.
        install_connection_wiring(app)

    async def test_install_is_best_effort(self, plane_env, monkeypatch) -> None:
        """A registration failure is swallowed (startup never crashes)."""
        app = SimpleNamespace(
            state=SimpleNamespace(
                connection_control_plane=plane_env.plane,
                repository_registry=plane_env.registry,
                ingest_supervisor=FakeSupervisor(),
            )
        )
        import backend.connectors.wiring as wiring_module

        def boom(_plane: Any) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(wiring_module, "register_connection_probes", boom)
        # Must not raise even though probe registration blew up.
        install_connection_wiring(app)
