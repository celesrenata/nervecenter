"""Workstream B: the durable connection control plane (backend/connectors/registry.py).

Service-level tests on the frozen
:class:`~backend.connectors.registry.ConnectionControlPlane` over a
file-backed SQLite engine — no network, no real IMAP:

- the two axes ``auth_status`` × ``operational_status`` and the frozen
  five-state derivation (:func:`derive_state`);
- the frozen hard rule: a successful login probe sets ``auth_status``
  but **never** advances ``operational_status`` past ``never_synced``;
  only :meth:`record_sync_success` marks a connection ``healthy``;
- secret handling: vault round-trip through the durable boundary, stable
  ``vault_unavailable`` when no vault, secret keys stripped from every
  response;
- sync/import: no trigger → honest ``ingest_unavailable`` (operational
  ``error``); trigger success/failure → ``syncing`` bookkeeping, import-job
  lifecycle, ``degraded`` then ``error`` on repeated failure;
- OAuth start/complete with an injectable token exchanger, including the
  PKCE (S256) challenge/verifier pair and the callback-state binding that
  rejects a forged ``state`` (CSRF guard, P1-106);
- the relay plane: pair (token returned once), register (constant-time
  check), deduped batch ingest, health, and the consent gate on
  initial-history import.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

import backend.api.app  # noqa: F401  (circular-import-safe entry, like the rest of the suite)

from backend.connectors.registry import (
    APP_PASSWORD,
    ACCESS_TOKEN,
    CURSOR_RELAY,
    REFRESH_TOKEN,
    RELAY_TOKEN,
    ConnectionControlPlane,
    ConnectionControlPlaneError,
    ProbeResultTyped,
    derive_state,
)
from backend.connectors.vault import FernetVault, generate_key
from backend.persistence.models.personal import (
    CommunicationRow,
    ConnectionRow,
    ConversationRow,
    ImportJobRow,
    IngestCursorRow,
    PersonIdentityRow,
    PersonRow,
    RelaySeenMessageRow,
    SecretRow,
)
from backend.persistence.repositories import RepositoryRegistry

PERSONAL_TABLES = [
    PersonRow.__table__,
    PersonIdentityRow.__table__,
    ConnectionRow.__table__,
    SecretRow.__table__,
    CommunicationRow.__table__,
    ConversationRow.__table__,
    ImportJobRow.__table__,
    IngestCursorRow.__table__,
    RelaySeenMessageRow.__table__,
]

IMAP_CONFIG = {"host": "imap.example.org", "port": 993, "account": "me@example.org"}


# -- seams --------------------------------------------------------------------


class FakeImapProbe:
    """A :class:`ProbeSeam` with a scripted outcome."""

    def __init__(self, *, success: bool, error_code: str | None = None):
        self._result = (
            ProbeResultTyped(True, None, None, ())
            if success
            else ProbeResultTyped(False, error_code or "imap_auth_rejected", "denied", ())
        )
        self.calls: list[tuple[dict[str, Any], str | None]] = []

    async def probe(self, config: dict[str, Any], secret: str | None) -> ProbeResultTyped:
        self.calls.append((dict(config), secret))
        return self._result


class FakeTrigger:
    """An :class:`IngestTrigger` that records calls and can fail on purpose."""

    def __init__(self, *, fail: Exception | None = None):
        self.started: list[str] = []
        self._fail = fail

    async def start(self, connection_row: Any) -> None:
        self.started.append(str(connection_row.id))
        if self._fail is not None:
            raise self._fail


def _s256(verifier: str) -> str:
    """RFC 7636 §4.1: base64url(SHA-256(verifier)) without padding."""
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _url_query(query: str) -> dict[str, str]:
    """Parse a URL query string into a dict (test helper for authorize URLs)."""
    from urllib.parse import parse_qs

    return {k: v[0] for k, v in parse_qs(query).items()}


class FakeExchanger:
    """A :class:`TokenExchanger` returning a fixed token.

    Accepts either a bare string (legacy access-token shape) or a dict
    grant mapping (``{"access_token": …, "refresh_token": …}``) to mirror
    the production relay's code-exchange result."""

    def __init__(
        self, token: str | dict[str, str] = "oauth-access-token"
    ) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._token = token

    async def exchange(
        self, provider: str, code: str, state: str
    ) -> str | dict[str, str]:
        self.calls.append((provider, code, state))
        return self._token


async def _engine(db_path: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(PersonRow.__table__.metadata.create_all, tables=PERSONAL_TABLES)
    return engine


@pytest.fixture
async def env(tmp_path: Path):
    """File-backed engine + Fernet vault + control plane (all real seams)."""
    engine = await _engine(tmp_path / "plane.db")
    try:
        from backend.persistence.connection import make_session_factory

        vault = FernetVault(generate_key())
        registry = RepositoryRegistry(make_session_factory(engine), secret_codec=vault)
        plane = ConnectionControlPlane(registry, vault=vault)
        yield plane, registry, vault, engine
    finally:
        await engine.dispose()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# -- frozen derivation ---------------------------------------------------------


class TestDeriveState:
    @pytest.mark.parametrize(
        ("auth", "op", "has_error", "expected"),
        [
            ("unconfigured", "never_synced", False, "not_configured"),
            (None, "never_synced", False, "not_configured"),
            ("auth_expired", "healthy", False, "auth_required"),
            ("auth_failed", "healthy", False, "auth_required"),
            ("pairing_pending", "never_synced", False, "auth_required"),
            ("authenticated", "syncing", False, "syncing"),
            ("authenticated", "healthy", False, "connected"),
            ("authenticated", "never_synced", False, "connected"),
            ("authenticated", "degraded", False, "degraded"),
            ("authenticated", "error", False, "degraded"),
            ("authenticated", "never_synced", True, "degraded"),
        ],
    )
    def test_frozen_table(self, auth, op, has_error, expected):
        assert derive_state(auth, op, has_error=has_error) == expected


# -- CRUD + config hygiene ------------------------------------------------------


class TestCrud:
    async def test_create_strips_secret_keys_from_config(self, env):
        plane, *_ = env
        row = await plane.create_connection(
            kind="imap",
            label="Mail",
            config={"host": "imap.example.org", "password": "top-secret", "ssl": True},
        )
        assert row.config.get("host") == "imap.example.org"
        assert "password" not in row.config
        public = await plane.get_connection(row.id)
        assert "password" not in public["config"]

    async def test_create_rejects_unknown_kind(self, env):
        plane, *_ = env
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await plane.create_connection(kind="telegram")
        assert exc.value.code == "kind_not_supported"

    async def test_update_merges_config_and_strips_secrets(self, env):
        plane, *_ = env
        row = await plane.create_connection(kind="imap", config=IMAP_CONFIG)
        updated = await plane.update_connection(
            row.id, label="Renamed", config={"port": 143, "token": "nope"}
        )
        assert updated["label"] == "Renamed"
        assert updated["config"]["host"] == IMAP_CONFIG["host"]
        assert updated["config"]["port"] == 143
        assert "token" not in updated["config"]

    async def test_unknown_id_raises_stable_code(self, env):
        plane, *_ = env
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await plane.update_connection("missing", label="x")
        assert exc.value.code == "connection_not_found"
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await plane.verify("missing")
        assert exc.value.code == "connection_not_found"

    async def test_get_connection_absent_is_none(self, env):
        plane, *_ = env
        assert await plane.get_connection("missing") is None

    async def test_delete_purges_secrets_and_disables(self, env):
        plane, registry, vault, _ = env
        row = await plane.create_connection(kind="imap", config=IMAP_CONFIG)
        await plane.store_secret(
            connection_id=row.id, kind=APP_PASSWORD, plaintext="pw"
        )
        assert await plane.delete_connection(row.id) is True
        # Soft delete: the row is never hard-removed, so a repeat is idempotent.
        assert await plane.delete_connection(row.id) is True
        # The row stays (durable layer has no hard delete) but is disabled…
        remaining = await registry.connections.get_connection(row.id)
        assert remaining.enabled is False
        # …and every secret kind is purged.
        assert (
            await registry.connections.get_secret(
                kind=APP_PASSWORD, owner_kind="connection", owner_id=row.id
            )
            is None
        )

    async def test_public_surface_always_has_both_axes(self, env):
        plane, *_ = env
        row = await plane.create_connection(kind="calendar")
        public = await plane.get_connection(row.id)
        for key in (
            "id",
            "kind",
            "label",
            "enabled",
            "config",
            "auth_status",
            "operational_status",
            "state",
            "last_success_at",
            "last_attempt_at",
            "last_error_code",
            "last_error_message",
            "import_progress",
            "created_at",
            "updated_at",
        ):
            assert key in public
        assert public["state"] == "not_configured"
        assert public["operational_status"] == "never_synced"


# -- secrets --------------------------------------------------------------------


class TestSecrets:
    async def test_store_secret_roundtrips_through_vault(self, env):
        plane, registry, vault, _ = env
        row = await plane.create_connection(kind="imap", config=IMAP_CONFIG)
        await plane.store_secret(
            connection_id=row.id, kind=APP_PASSWORD, plaintext="s3cret"
        )
        assert (
            await registry.connections.get_secret(
                kind=APP_PASSWORD, owner_kind="connection", owner_id=row.id
            )
            == "s3cret"
        )
        # The connection's config remembers the secret_ref (non-secret).
        public = await plane.get_connection(row.id)
        # The frozen public surface exposes the kind as a top-level secret_ref…
        assert public["secret_ref"] == APP_PASSWORD
        # …and redacts it from the config projection (denied key).
        assert "secret_ref" not in public["config"]
        # Raw durable row still carries only the opaque payload.
        async with registry.session_factory() as session:
            stored = (
                await session.execute(
                    select(SecretRow).where(SecretRow.owner_id == row.id)
                )
            ).scalar_one()
        assert stored.payload["alg"] == "fernet"
        assert "s3cret" not in str(stored.payload)
        assert vault.decrypt_secret(stored.payload) == "s3cret"

    async def test_store_secret_unknown_connection(self, env):
        plane, *_ = env
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await plane.store_secret(
                connection_id="missing", kind=APP_PASSWORD, plaintext="x"
            )
        assert exc.value.code == "connection_not_found"

    async def test_verify_loads_stored_secret_into_probe(self, env):
        plane, *_ = env
        row = await plane.create_connection(kind="imap", config=IMAP_CONFIG)
        await plane.store_secret(
            connection_id=row.id, kind=APP_PASSWORD, plaintext="stored-pw"
        )
        probe = FakeImapProbe(success=True)
        plane.register_probe("imap", probe)
        result = await plane.verify(row.id)
        assert result["ok"] is True
        assert result["auth_status"] == "authenticated"
        # The probe received the *decrypted* stored credential, not the ref.
        assert probe.calls[0][1] == "stored-pw"

    async def test_store_secret_without_vault_is_stable_503_code(self, tmp_path: Path):
        engine = await _engine(tmp_path / "novault.db")
        try:
            from backend.persistence.connection import make_session_factory

            plane = ConnectionControlPlane(
                RepositoryRegistry(make_session_factory(engine))
            )
            row = await plane.create_connection(kind="imap")
            with pytest.raises(ConnectionControlPlaneError) as exc:
                await plane.store_secret(
                    connection_id=row.id, kind=APP_PASSWORD, plaintext="pw"
                )
            assert exc.value.code == "vault_unavailable"
        finally:
            await engine.dispose()


# -- verify / hard rule ---------------------------------------------------------


class TestVerify:
    async def test_probe_success_authenticates_but_does_not_sync(self, env):
        plane, *_ = env
        row = await plane.create_connection(kind="imap", config=IMAP_CONFIG)
        plane.register_probe("imap", FakeImapProbe(success=True))
        result = await plane.verify(row.id)
        assert result["ok"] is True
        assert result["auth_status"] == "authenticated"
        # Frozen hard rule: the probe never advances the operational axis.
        assert result["operational_status"] == "never_synced"
        assert result["state"] == "connected"
        assert result["last_success_at"] is None

    async def test_probe_failure_records_stable_error(self, env):
        plane, *_ = env
        row = await plane.create_connection(kind="imap", config=IMAP_CONFIG)
        plane.register_probe("imap", FakeImapProbe(success=False, error_code="imap_unreachable"))
        result = await plane.verify(row.id)
        assert result["ok"] is False
        assert result["error_code"] == "imap_unreachable"
        assert result["auth_status"] == "auth_failed"
        assert result["operational_status"] == "never_synced"  # untouched
        assert result["state"] == "auth_required"
        public = await plane.get_connection(row.id)
        assert public["last_error_code"] == "imap_unreachable"
        assert public["last_error_message"] == "denied"

    async def test_probe_without_registered_seam(self, env):
        plane, *_ = env
        row = await plane.create_connection(kind="calendar")
        result = await plane.verify(row.id)
        assert result["ok"] is False
        assert result["error_code"] == "probe_unsupported"

    async def test_reconnect_reverifies_then_syncs(self, env):
        plane, *_ = env
        row = await plane.create_connection(kind="imap", config=IMAP_CONFIG)
        probe = FakeImapProbe(success=True)
        plane.register_probe("imap", probe)
        trigger = FakeTrigger()
        plane.register_trigger("imap", trigger)
        result = await plane.reconnect(row.id)
        assert result["reconnect"] is True
        assert result["ok"] is True
        assert len(trigger.started) == 1
        # Frozen rule: a trigger start only advances to syncing; only
        # record_sync_success (workstream C) marks healthy.
        public = await plane.get_connection(row.id)
        assert public["operational_status"] == "syncing"
        assert public["state"] == "syncing"


# -- sync / import --------------------------------------------------------------


class TestSync:
    async def test_sync_without_trigger_is_honest(self, env):
        plane, registry, *_ = env
        row = await plane.create_connection(kind="imap", config=IMAP_CONFIG)
        result = await plane.sync_now(row.id)
        assert result["ok"] is False
        assert result["error_code"] == "ingest_unavailable"
        public = await plane.get_connection(row.id)
        assert public["operational_status"] == "error"
        assert public["last_error_code"] == "ingest_unavailable"
        # derive_state rule 1: unconfigured auth wins over the operational axis.
        assert public["state"] == "not_configured"
        # No import job was opened (the trigger never ran).
        async with registry.session_factory() as session:
            jobs = (
                await session.execute(select(ImportJobRow))
            ).scalars().all()
        assert jobs == []

    async def test_sync_then_success_marks_healthy_and_advances_cursor(self, env):
        plane, registry, *_ = env
        row = await plane.create_connection(kind="imap", config=IMAP_CONFIG)
        trigger = FakeTrigger()
        plane.register_trigger("imap", trigger)
        result = await plane.sync_now(row.id)
        assert result["ok"] is True
        assert result["import_job_id"]
        assert len(trigger.started) == 1
        # Frozen hard rule: the trigger start only advances to syncing.
        public = await plane.get_connection(row.id)
        assert public["operational_status"] == "syncing"
        assert public["state"] == "syncing"
        assert public["last_success_at"] is None
        job = await registry.imports.get_import_job(result["import_job_id"])
        assert job.state == "running"
        # Only the worker's success report marks healthy + advances the cursor.
        await plane.record_sync_success(row.id, cursor_value={"seq": 42})
        healthy = await plane.get_connection(row.id)
        assert healthy["operational_status"] == "healthy"
        # Frozen two-axis rule: a real ingest marks the operational axis
        # healthy, but it never fabricates authentication. The connection was
        # never probed (auth_status stays unconfigured), so only "syncing"
        # outranks the auth axis — a healthy-but-unconfigured connection is
        # honestly "not_configured", not "connected".
        assert healthy["state"] == "not_configured"
        assert healthy["last_success_at"] is not None
        cursor = await registry.imports.get_cursor(row.id, "imap_seq")
        assert cursor is not None
        assert cursor.cursor_value == {"seq": 42}

    async def test_record_sync_success_sets_cursor(self, env):
        plane, registry, *_ = env
        row = await plane.create_connection(kind="relay_device")
        await plane.record_sync_success(row.id, cursor_value={"seq": 42})
        cursor = await registry.imports.get_cursor(row.id, CURSOR_RELAY)
        assert cursor is not None
        assert cursor.cursor_value == {"seq": 42}

    async def test_record_sync_failure_escalates_degraded_then_error(self, env):
        plane, *_ = env
        row = await plane.create_connection(kind="imap")
        first = await plane.record_sync_failure(row.id, "imap_auth_rejected", "bad pw")
        assert first["operational_status"] == "degraded"
        second = await plane.record_sync_failure(row.id, "imap_auth_rejected", "bad pw")
        assert second["operational_status"] == "error"

    async def test_trigger_failure_fails_job_and_degrades(self, env):
        plane, registry, *_ = env
        row = await plane.create_connection(kind="imap", config=IMAP_CONFIG)
        trigger = FakeTrigger(fail=RuntimeError("boom"))
        plane.register_trigger("imap", trigger)
        result = await plane.import_now(row.id)
        assert result["ok"] is False
        assert result["error_code"] == "ingest_failed"
        # The failure response omits the job id — find the job row directly.
        async with registry.session_factory() as session:
            job = (
                await session.execute(
                    select(ImportJobRow).where(ImportJobRow.connection_id == row.id)
                )
            ).scalars().one()
        assert job.state == "failed"
        assert job.error_code == "ingest_failed"
        public = await plane.get_connection(row.id)
        assert public["operational_status"] == "degraded"

    async def test_import_now_records_history_job(self, env):
        plane, registry, *_ = env
        row = await plane.create_connection(kind="imap", config=IMAP_CONFIG)
        plane.register_trigger("imap", FakeTrigger())
        result = await plane.import_now(row.id)
        job = await registry.imports.get_import_job(result["import_job_id"])
        assert job.kind == "history"


# -- permissions ----------------------------------------------------------------


class TestPermissions:
    async def test_permissions_surface_per_kind(self, env):
        plane, *_ = env
        surface = plane.permissions_for("imap")
        assert surface["kind"] == "imap"
        assert surface["editable_keys"] == ["account", "host", "port", "ssl"]
        assert "password" in surface["denied_keys"]
        assert "secret_ref" in surface["denied_keys"]

    async def test_permissions_unknown_kind(self, env):
        plane, *_ = env
        with pytest.raises(ConnectionControlPlaneError) as exc:
            plane.permissions_for("telegram")
        assert exc.value.code == "kind_not_supported"

    async def test_patch_permissions_apply_only_allowlisted(self, env):
        plane, *_ = env
        row = await plane.create_connection(kind="imap", config=IMAP_CONFIG)
        result = await plane.patch_permissions(
            row.id, {"host": "imap.new", "password": "sneaky", "label": "x"}
        )
        assert result["applied_keys"] == ["host"]
        assert sorted(result["ignored_keys"]) == ["label", "password"]
        public = await plane.get_connection(row.id)
        assert public["config"]["host"] == "imap.new"
        assert "password" not in public["config"]


# -- OAuth flow -------------------------------------------------------------------


class TestOauth:
    async def test_start_oauth_persists_state_and_returns_url(self, env):
        plane, registry, *_ = env
        row = await plane.create_connection(
            kind="gmail", config={"client_id": "cid", "oauth_redirect_uri": "http://cb"}
        )
        result = await plane.start_oauth(row.id)
        assert result["provider"] == "google"
        assert result["state"]
        assert "client_id=cid" in result["authorization_url"]
        # The flow state persists in the raw config…
        raw = await registry.connections.get_connection(row.id)
        assert raw.config.get("oauth_state") == result["state"]
        # …but is stripped from the public projection (denied key).
        public = await plane.get_connection(row.id)
        assert "oauth_state" not in public["config"]

    async def test_start_oauth_wrong_kind(self, env):
        plane, *_ = env
        row = await plane.create_connection(kind="imap")
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await plane.start_oauth(row.id)
        assert exc.value.code == "oauth_incomplete"

    async def test_complete_oauth_stores_token_via_vault(self, env):
        plane, registry, vault, _ = env
        row = await plane.create_connection(kind="graph_mail")
        exchanger = FakeExchanger("ms-token")
        plane.set_token_exchanger(exchanger)
        flow = await plane.start_oauth(row.id)
        result = await plane.complete_oauth(row.id, code="c1", state=flow["state"])
        assert result["ok"] is True
        assert result["auth_status"] == "authenticated"
        assert exchanger.calls == [("microsoft", "c1", flow["state"])]
        stored = await registry.connections.get_secret(
            kind=ACCESS_TOKEN, owner_kind="connection", owner_id=row.id
        )
        assert stored == "ms-token"
        # The durable row carries the fernet ciphertext, not the raw token.
        async with registry.session_factory() as session:
            secret_row = (
                await session.execute(
                    select(SecretRow).where(
                        SecretRow.owner_id == row.id,
                        SecretRow.kind == ACCESS_TOKEN,
                    )
                )
            ).scalar_one()
        assert secret_row.payload["alg"] == "fernet"
        assert "ms-token" not in str(secret_row.payload)
        assert vault.decrypt_secret(secret_row.payload) == "ms-token"

    async def test_complete_oauth_grant_stores_both_durable_kinds(self, env):
        """A grant-mapping exchanger persists both the durable access token
        and the durable refresh token; a legacy string exchanger persists
        only the access token (refresh token is absent)."""
        plane, registry, vault, _ = env
        row = await plane.create_connection(kind="gmail")
        exchanger = FakeExchanger(
            {"access_token": "g-at-1", "refresh_token": "g-rt-1"}
        )
        plane.set_token_exchanger(exchanger)
        flow = await plane.start_oauth(row.id)
        result = await plane.complete_oauth(row.id, code="c1", state=flow["state"])
        assert result["ok"] is True
        assert result["auth_status"] == "authenticated"
        # Both durable kinds are persisted.
        access = await registry.connections.get_secret(
            kind=ACCESS_TOKEN, owner_kind="connection", owner_id=row.id
        )
        assert access == "g-at-1"
        refresh = await registry.connections.get_secret(
            kind=REFRESH_TOKEN, owner_kind="connection", owner_id=row.id
        )
        assert refresh == "g-rt-1"
        # The durable rows carry the fernet ciphertext, not the raw tokens.
        async with registry.session_factory() as session:
            secret_rows = (
                await session.execute(
                    select(SecretRow).where(
                        SecretRow.owner_id == row.id,
                        SecretRow.kind.in_([ACCESS_TOKEN, REFRESH_TOKEN]),
                    )
                )
            ).scalars().all()
            assert len(secret_rows) == 2
            for sr in secret_rows:
                assert sr.payload["alg"] == "fernet"
                assert "g-at-1" not in str(sr.payload)
                assert "g-rt-1" not in str(sr.payload)

    async def test_complete_oauth_legacy_string_exchanger_stores_access_only(
        self, env
    ):
        """A legacy string exchanger persists only the access token; no
        refresh token is stored (access-only onboarding)."""
        plane, registry, vault, _ = env
        row = await plane.create_connection(kind="graph_mail")
        exchanger = FakeExchanger("ms-legacy-token")
        plane.set_token_exchanger(exchanger)
        flow = await plane.start_oauth(row.id)
        result = await plane.complete_oauth(row.id, code="c1", state=flow["state"])
        assert result["ok"] is True
        access = await registry.connections.get_secret(
            kind=ACCESS_TOKEN, owner_kind="connection", owner_id=row.id
        )
        assert access == "ms-legacy-token"
        refresh = await registry.connections.get_secret(
            kind=REFRESH_TOKEN, owner_kind="connection", owner_id=row.id
        )
        assert refresh is None

    async def test_complete_oauth_without_exchanger(self, env):
        plane, *_ = env
        row = await plane.create_connection(kind="gmail")
        flow = await plane.start_oauth(row.id)
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await plane.complete_oauth(row.id, code="c", state=flow["state"])
        assert exc.value.code == "oauth_incomplete"

    async def test_complete_oauth_empty_token(self, env):
        plane, *_ = env
        row = await plane.create_connection(kind="gmail")
        plane.set_token_exchanger(FakeExchanger(""))
        flow = await plane.start_oauth(row.id)
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await plane.complete_oauth(row.id, code="c", state=flow["state"])
        assert exc.value.code == "oauth_incomplete"

    # -- PKCE (P1-106) + callback-state verification (CSRF) --------------------

    async def test_start_oauth_emits_s256_challenge_and_verifier(self, env):
        """The authorize URL must carry a PKCE ``code_challenge`` (S256) that
        matches the returned ``code_verifier`` (RFC 7636 §4.2)."""
        plane, *_ = env
        row = await plane.create_connection(
            kind="gmail", config={"client_id": "cid", "oauth_redirect_uri": "http://cb"}
        )
        result = await plane.start_oauth(row.id)
        url_query = result["authorization_url"].split("?", 1)[1]
        challenge = _url_query(url_query)["code_challenge"]
        assert challenge == _s256(result["code_verifier"])
        assert _url_query(url_query)["code_challenge_method"] == "S256"

    async def test_complete_oauth_rejects_forged_state(self, env):
        """A callback ``state`` that does not match the persisted per-flow
        state must be rejected with the stable code — the CSRF guard. The
        token must NOT be exchanged or stored."""
        plane, registry, *_ = env
        row = await plane.create_connection(kind="gmail")
        exchanger = FakeExchanger("forged-token")
        plane.set_token_exchanger(exchanger)
        await plane.start_oauth(row.id)
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await plane.complete_oauth(row.id, code="c", state="attacker-state")
        assert exc.value.code == "oauth_incomplete"
        # No exchange happened, no secret stored, auth unchanged.
        assert exchanger.calls == []
        stored = await registry.connections.get_secret(
            kind=ACCESS_TOKEN, owner_kind="connection", owner_id=row.id
        )
        assert stored is None
        public = await plane.get_connection(row.id)
        assert public["auth_status"] == "unconfigured"

    async def test_complete_oauth_rejects_stale_state_after_replay(self, env):
        """Once a flow's state has been consumed, replaying the same state
        (a second callback for the same code) is rejected — one flow per
        state, no token reuse."""
        plane, registry, *_ = env
        row = await plane.create_connection(kind="graph_mail")
        plane.set_token_exchanger(FakeExchanger("tok"))
        flow = await plane.start_oauth(row.id)
        first = await plane.complete_oauth(row.id, code="c1", state=flow["state"])
        assert first["ok"] is True
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await plane.complete_oauth(row.id, code="c1", state=flow["state"])
        assert exc.value.code == "oauth_incomplete"
        # Still exactly one stored access token from the first (valid) flow.
        stored = await registry.connections.get_secret(
            kind=ACCESS_TOKEN, owner_kind="connection", owner_id=row.id
        )
        assert stored == "tok"


# -- diagnostics ------------------------------------------------------------------


class TestDiagnostics:
    async def test_diagnostics_map_error_code_to_action(self, env):
        plane, *_ = env
        row = await plane.create_connection(kind="imap", config=IMAP_CONFIG)
        plane.register_probe("imap", FakeImapProbe(success=False, error_code="imap_auth_rejected"))
        await plane.verify(row.id)
        diag = await plane.diagnostics(row.id)
        assert diag["diagnosis"]["error_code"] == "imap_auth_rejected"
        assert "credential" in diag["diagnosis"]["fix"].lower()
        assert diag["diagnosis"]["import_job"] is None

    async def test_diagnostics_without_error(self, env):
        plane, *_ = env
        row = await plane.create_connection(kind="imap")
        diag = await plane.diagnostics(row.id)
        assert diag["diagnosis"]["error_code"] is None
        assert "No errors recorded" in diag["diagnosis"]["fix"]


# -- relay plane --------------------------------------------------------------------


async def _pair_and_register(
    plane: ConnectionControlPlane,
    *,
    device_name: str | None = None,
    wrong_token: bool = False,
) -> tuple[dict[str, Any], Any]:
    pair = await plane.relay_pair(device_name=device_name)
    token = "wrong-token" if wrong_token else pair["token"]
    if wrong_token:
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await plane.relay_register(device_id=pair["device_id"], token=token)
        return pair, exc.value
    register = await plane.relay_register(device_id=pair["device_id"], token=token)
    return pair, register


class TestRelay:
    async def test_pair_mints_device_and_returns_token_once(self, env):
        plane, registry, vault, _ = env
        pair = await plane.relay_pair(device_name="MacBook")
        assert pair["device_id"].startswith("relay-")
        assert pair["token"]
        assert pair["connection_id"]
        public = await plane.get_connection(pair["connection_id"])
        assert public["kind"] == "relay_device"
        assert public["auth_status"] == "pairing_pending"
        assert public["state"] == "auth_required"
        assert public["config"]["device_id"] == pair["device_id"]
        # The pairing token lives encrypted in the vault, never in config.
        assert "token" not in public["config"]
        stored = await registry.connections.get_secret(
            kind=RELAY_TOKEN, owner_kind="connection", owner_id=pair["connection_id"]
        )
        assert stored == pair["token"]
        # The durable row stores the ciphertext, not the token.
        async with registry.session_factory() as session:
            row = (
                await session.execute(
                    select(SecretRow).where(SecretRow.owner_id == pair["connection_id"])
                )
            ).scalar_one()
        assert pair["token"] not in str(row.payload)

    async def test_register_authenticates_device(self, env):
        plane, *_ = env
        pair, register = await _pair_and_register(plane)
        assert register["ok"] is True
        assert register["auth_status"] == "authenticated"
        # Frozen hard rule: registration is not a sync.
        assert register["operational_status"] == "never_synced"
        assert register["state"] == "connected"
        public = await plane.get_connection(pair["connection_id"])
        assert public["last_attempt_at"] is not None
        assert public["last_success_at"] is None

    async def test_register_unknown_device(self, env):
        plane, *_ = env
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await plane.relay_register(device_id="relay-nobody", token="x")
        assert exc.value.code == "relay_token_invalid"

    async def test_register_wrong_token(self, env):
        plane, *_ = env
        pair, error = await _pair_and_register(plane, wrong_token=True)
        # Constant-time mismatch → stable code; the device stays pairing.
        assert error.code == "relay_token_invalid"
        public = await plane.get_connection(pair["connection_id"])
        assert public["auth_status"] == "pairing_pending"

    async def test_ingest_requires_registered_device(self, env):
        plane, *_ = env
        pair, _ = await _pair_and_register(plane, wrong_token=True)
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await plane.relay_ingest_batch(
                device_id=pair["device_id"],
                messages=[{"message_id": "m1", "body": "hi"}],
            )
        assert exc.value.code == "relay_token_invalid"

    async def test_ingest_unknown_device(self, env):
        plane, *_ = env
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await plane.relay_ingest_batch(
                device_id="relay-nobody", messages=[{"message_id": "m1"}]
            )
        assert exc.value.code == "relay_token_invalid"

    async def test_ingest_batch_dedupes_by_message_id(self, env):
        plane, registry, *_ = env
        pair, _ = await _pair_and_register(plane)
        device_id = pair["device_id"]
        messages = [
            {"message_id": "m1", "channel": "imessage", "peer_identifier": "+15550001111", "body": "hello"},
            {"message_id": "m2", "channel": "imessage", "peer_identifier": "+15550001111", "body": "world"},
            {"message_id": "m1", "channel": "imessage", "peer_identifier": "+15550001111", "body": "hello"},
        ]
        first = await plane.relay_ingest_batch(device_id=device_id, messages=messages)
        assert first == {"ok": True, "stored": 2, "skipped": 1}
        # Replay: everything is already seen → all skipped, nothing new.
        second = await plane.relay_ingest_batch(device_id=device_id, messages=messages)
        assert second == {"ok": True, "stored": 0, "skipped": 3}

    async def test_ingest_marks_connection_healthy(self, env):
        plane, registry, *_ = env
        pair, _ = await _pair_and_register(plane)
        device_id = pair["device_id"]
        await plane.relay_ingest_batch(
            device_id=device_id,
            messages=[{"message_id": "m1", "peer_identifier": "+15550001111", "body": "x"}],
        )
        public = await plane.get_connection(pair["connection_id"])
        assert public["operational_status"] == "healthy"
        assert public["state"] == "connected"
        assert public["last_success_at"] is not None

    async def test_ingest_persists_durable_communications(self, env):
        plane, registry, *_ = env
        pair, _ = await _pair_and_register(plane)
        device_id = pair["device_id"]
        await plane.relay_ingest_batch(
            device_id=device_id,
            messages=[
                {
                    "message_id": "m1",
                    "channel": "imessage",
                    "peer_identifier": "+15550001111",
                    "peer_name": "Alice",
                    "body": "hello durable",
                }
            ],
        )
        comms = await registry.communications.list_communications(limit=200)
        assert len(comms) == 1
        comm = comms[0]
        assert comm.channel == "imessage"
        assert comm.direction == "inbound"
        assert comm.content_ref  # stable, non-empty
        assert comm.content_ref.startswith("relay://")
        assert "body" not in comm.__dict__  # bodies never land in durable rows

    async def test_ingest_same_peer_resolves_to_one_person(self, env):
        plane, registry, *_ = env
        pair, _ = await _pair_and_register(plane)
        device_id = pair["device_id"]
        await plane.relay_ingest_batch(
            device_id=device_id,
            messages=[
                {"message_id": "a", "peer_identifier": "+15550001111", "body": "1"},
                {"message_id": "b", "peer_identifier": "+15550001111", "body": "2"},
            ],
        )
        comms = await registry.communications.list_communications(limit=200)
        assert {c.person_id for c in comms} == {comms[0].person_id}

    async def test_health_reports_backlog_and_seen(self, env):
        plane, *_ = env
        pair, _ = await _pair_and_register(plane)
        device_id = pair["device_id"]
        await plane.relay_ingest_batch(
            device_id=device_id,
            messages=[
                {"message_id": "m1", "peer_identifier": "+15550001111", "body": "x"},
                {"message_id": "m2", "peer_identifier": "+15550001111", "body": "y"},
            ],
        )
        health = await plane.relay_health()
        assert len(health) == 1
        entry = health[0]
        assert entry["device_id"] == device_id
        assert entry["auth_status"] == "authenticated"
        assert entry["operational_status"] == "healthy"
        assert entry["state"] == "connected"
        assert entry["pending_backlog"] == 2
        assert entry["last_message_at"] is not None
        assert entry["last_seen_at"] is not None

    async def test_import_request_requires_consent(self, env):
        plane, *_ = env
        pair, _ = await _pair_and_register(plane)
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await plane.relay_import_request(device_id=pair["device_id"], consent=False)
        assert exc.value.code == "consent_required"
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await plane.relay_import_request(device_id=pair["device_id"], consent=True)
            await plane.relay_import_request(device_id="relay-nobody", consent=True)
        assert exc.value.code == "connection_not_found"

    async def test_import_request_with_consent_creates_job(self, env):
        plane, registry, *_ = env
        pair, _ = await _pair_and_register(plane)
        result = await plane.relay_import_request(
            device_id=pair["device_id"], consent=True
        )
        assert result["ok"] is True
        assert result["consent"] is True
        assert result["state"] == "pending"
        job = await registry.imports.get_import_job(result["import_job_id"])
        assert job is not None
        assert job.kind == "history"
        assert job.state == "pending"
        assert job.progress == {"consent": True}
        # The job id is remembered on the connection for diagnostics.
        public = await plane.get_connection(pair["connection_id"])
        assert public["import_progress"]["last_job_id"] == result["import_job_id"]

    async def test_import_request_unknown_device(self, env):
        plane, *_ = env
        with pytest.raises(ConnectionControlPlaneError) as exc:
            await plane.relay_import_request(device_id="relay-nobody", consent=True)
        assert exc.value.code == "connection_not_found"


# -- relay seen-set bound ------------------------------------------------------------


class TestRelaySeenBound:
    async def test_seen_set_is_bounded(self, env):
        from backend.connectors.registry import RELAY_SEEN_LIMIT

        plane, registry, *_ = env
        pair, _ = await _pair_and_register(plane)
        device_id = pair["device_id"]
        # Feed more than the bound in one batch.
        batch = [
            {"message_id": f"m{i}", "peer_identifier": f"+1555{i:07d}", "body": f"b{i}"}
            for i in range(RELAY_SEEN_LIMIT + 50)
        ]
        await plane.relay_ingest_batch(device_id=device_id, messages=batch)
        cursor = await registry.imports.get_cursor(pair["connection_id"], CURSOR_RELAY)
        seen = cursor.cursor_value["seen"]
        assert len(seen) == RELAY_SEEN_LIMIT
        # The *oldest* entries were dropped (m0..m49), the newest retained
        # (m50..m549 — exactly RELAY_SEEN_LIMIT entries).
        assert seen[-1] == f"{device_id}:m{RELAY_SEEN_LIMIT + 49}"
        assert seen[0] == f"{device_id}:m50"
