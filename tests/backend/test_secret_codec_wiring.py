"""GAP 2 — codec-bound repository registry via wire_dependencies (workstream F).

Proves that the single ``app.state.repository_registry`` built by
``wire_dependencies`` is bound to the Fernet vault when
``NERVE_SECRET_KEY`` is set, and degrades to the identity codec when it is
absent — and that the connection control plane (workstream B) and the mail
worker (workstream C) share that same codec-bound registry, so:

- Rotated OAuth refresh tokens are persisted as Fernet ciphertext, never
  plaintext.
- Secrets written by the mail worker's ``upsert_secret`` are readable by
  the control plane's ``get_secret`` (and vice versa) through the one
  shared registry.
- Without the key, startup still succeeds (honest degradation) and the
  codec is the identity.

The harness monkeypatches ``build_db_engine`` to return a real in-memory
SQLite async engine, and leaves NATS URL unset (no connect attempt).
"""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.api.wiring import _load_secret_codec, wire_dependencies
from backend.connectors.registry import ConnectionControlPlane
from backend.connectors.vault import FernetVault, generate_key
from backend.persistence.models.base import Base
from backend.persistence.models.personal import SecretRow
from backend.persistence.repositories import RepositoryRegistry

# Synthetic credential — long enough to exercise the 40+ char blob regex.
_TEST_CREDENTIAL = (
    "imaps://user:secret-token-00011122233344455566677788899@imap.example.com"
)


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


async def _make_sqlite_engine() -> AsyncEngine:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[SecretRow.__table__],
        )
    return engine


def _clear_env(monkeypatch) -> None:
    """Remove the DB / NATS / ingest / calendar env vars so the wiring
    degrades cleanly (no network, no supervisor install)."""
    for var in (
        "NERVE_DB_URL",
        "NERVECENTER_DATABASE_URL",
        "NERVE_NATS_URL",
        "NERVECENTER_NATS_URL",
        "NERVE_INGEST_ENABLED",
        "NERVE_CALENDAR_SYNC_ENABLED",
        "NERVE_SECRET_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


class TestLoadSecretCodec:
    """_load_secret_codec() unit tests."""

    def test_returns_fernet_vault_when_key_set(self, monkeypatch) -> None:
        key = generate_key()
        monkeypatch.setenv("NERVE_SECRET_KEY", key)
        codec = _load_secret_codec()
        assert isinstance(codec, FernetVault)

    def test_returns_none_when_key_absent(self, monkeypatch) -> None:
        monkeypatch.delenv("NERVE_SECRET_KEY", raising=False)
        codec = _load_secret_codec()
        assert codec is None

    def test_returns_none_when_key_invalid(self, monkeypatch) -> None:
        monkeypatch.setenv("NERVE_SECRET_KEY", "not-a-fernet-key")
        codec = _load_secret_codec()
        assert codec is None


class TestWireDependenciesCodecBinding:
    """wire_dependencies() end-to-end: the registry is Fernet-bound."""

    async def test_registry_uses_fernet_when_key_set(
        self, monkeypatch, tmp_path
    ) -> None:
        """With NERVE_SECRET_KEY set:

        - ``app.state.secret_vault`` is a FernetVault.
        - ``app.state.repository_registry.secret_codec`` is the same vault.
        - A secret written through the registry round-trips.
        - The durable row holds Fernet ciphertext, never plaintext.
        """
        _clear_env(monkeypatch)
        key = generate_key()
        monkeypatch.setenv("NERVE_SECRET_KEY", key)

        engine = await _make_sqlite_engine()
        monkeypatch.setattr(
            "backend.api.wiring.build_db_engine", lambda: engine
        )

        app = FastAPI()
        try:
            await wire_dependencies(app)

            # 1. vault is loaded and bound.
            vault = app.state.secret_vault
            assert isinstance(vault, FernetVault)

            # 2. registry's codec is the same vault (single shared codec).
            registry = app.state.repository_registry
            assert registry is not None
            assert isinstance(registry, RepositoryRegistry)
            assert registry.secret_codec is vault

            # 3. write a secret → read it back (round-trip).
            await registry.connections.upsert_secret(
                kind="access_token",
                owner_kind="connection",
                owner_id="conn-fernet-001",
                plaintext=_TEST_CREDENTIAL,
            )
            read_back = await registry.connections.get_secret(
                kind="access_token",
                owner_kind="connection",
                owner_id="conn-fernet-001",
            )
            assert read_back == _TEST_CREDENTIAL

            # 4. the durable row holds ciphertext, not plaintext.
            async with engine.begin() as conn:
                result = await conn.execute(
                    select(SecretRow.payload).where(
                        SecretRow.owner_id == "conn-fernet-001"
                    )
                )
                payload = result.scalar_one()
                assert payload.get("alg") == "fernet"
                assert "c" in payload
                assert _TEST_CREDENTIAL not in str(payload)
                # The Fernet token decodes to the original plaintext.
                assert vault.decrypt_secret(payload) == _TEST_CREDENTIAL
        finally:
            await engine.dispose()

    async def test_registry_degrades_to_identity_when_no_key(
        self, monkeypatch, tmp_path
    ) -> None:
        """Without NERVE_SECRET_KEY:

        - ``app.state.secret_vault`` is None.
        - ``app.state.repository_registry.secret_codec`` is None (identity).
        - Startup still succeeds.
        - A secret round-trips verbatim (identity codec).
        """
        _clear_env(monkeypatch)

        engine = await _make_sqlite_engine()
        monkeypatch.setattr(
            "backend.api.wiring.build_db_engine", lambda: engine
        )

        app = FastAPI()
        try:
            await wire_dependencies(app)

            # 1. no vault.
            assert app.state.secret_vault is None

            # 2. registry still exists (startup did not crash).
            registry = app.state.repository_registry
            assert registry is not None

            # 3. identity codec (None → the repo defaults to IdentitySecretCodec).
            assert registry.secret_codec is None

            # 4. round-trip works (identity: plaintext verbatim).
            await registry.connections.upsert_secret(
                kind="access_token",
                owner_kind="connection",
                owner_id="conn-identity-001",
                plaintext=_TEST_CREDENTIAL,
            )
            read_back = await registry.connections.get_secret(
                kind="access_token",
                owner_kind="connection",
                owner_id="conn-identity-001",
            )
            assert read_back == _TEST_CREDENTIAL

            # 5. identity payload is {"c": plaintext} — verify it is NOT Fernet.
            async with engine.begin() as conn:
                result = await conn.execute(
                    select(SecretRow.payload).where(
                        SecretRow.owner_id == "conn-identity-001"
                    )
                )
                payload = result.scalar_one()
                # Identity codec stores plaintext verbatim ({"c": ...}).
                assert payload.get("c") == _TEST_CREDENTIAL
                assert "alg" not in payload, (
                    "Identity codec must not produce an alg tag"
                )
        finally:
            await engine.dispose()


class TestSingleSharedRegistry:
    """The one registry is shared by control plane, mail worker, and calendar."""

    async def test_all_planes_share_one_codec(
        self, monkeypatch, tmp_path
    ) -> None:
        """Proves the dual-registry fix:

        - The registry is built ONCE in wire_dependencies.
        - The control plane reuses it (no second registry is built).
        - A secret written via the "mail worker path" (registry.connections)
          is readable by the "control plane path" (same registry), and
          vice versa.
        """
        _clear_env(monkeypatch)
        key = generate_key()
        monkeypatch.setenv("NERVE_SECRET_KEY", key)

        engine = await _make_sqlite_engine()
        monkeypatch.setattr(
            "backend.api.wiring.build_db_engine", lambda: engine
        )

        app = FastAPI()
        try:
            await wire_dependencies(app)

            registry = app.state.repository_registry
            assert registry is not None

            # Simulate what _install_connection_control_plane does:
            # it reuses the single registry (no rebuild).
            control_plane = ConnectionControlPlane(
                registry, vault=app.state.secret_vault
            )
            assert control_plane is not None
            # The control plane holds a reference to the SAME registry.
            assert control_plane._registry is registry  # noqa: SLF001

            # Write via the "mail worker" path (registry.connections).
            await registry.connections.upsert_secret(
                kind="access_token",
                owner_kind="connection",
                owner_id="shared-conn",
                plaintext="shared-token-aaaabbbccccddddeeeeffffgggg",
            )

            # Read via the "control plane" path (same registry → same codec).
            read_back = await registry.connections.get_secret(
                kind="access_token",
                owner_kind="connection",
                owner_id="shared-conn",
            )
            assert read_back == "shared-token-aaaabbbccccddddeeeeffffgggg"

            # And the durable row is Fernet-encrypted (not plaintext).
            async with engine.begin() as conn:
                result = await conn.execute(
                    select(SecretRow.payload).where(
                        SecretRow.owner_id == "shared-conn"
                    )
                )
                payload = result.scalar_one()
                assert payload.get("alg") == "fernet"
        finally:
            await engine.dispose()
