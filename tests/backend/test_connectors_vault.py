"""Workstream B: the Fernet-backed secret vault (backend/connectors/vault.py).

Covers the frozen vault contract (spec ``personal-ops-recovery`` §3):

- opaque, JSON-serialisable payload ``{"alg": "fernet", "c": <token>}`` —
  the plaintext never appears in the payload or in the durable row;
- guaranteed round-trip; ciphertext is non-deterministic (timestamp + IV);
- every failure raises :class:`VaultError` with a stable ``code``
  (``invalid_key`` / ``bad_payload`` / ``decrypt_failed`` /
  ``secret_too_large`` / ``no_key``) — never a raw exception message;
- ``load_from_env`` fails loudly when ``NERVE_SECRET_KEY`` is unset or empty
  (no silent identity fallback);
- integration with the frozen durable boundary: the ``secrets`` table stores
  only the codec ciphertext, verified by reading the raw row back from SQLite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

import backend.api.app  # noqa: F401  (circular-import-safe entry, like the rest of the suite)

from backend.connectors.vault import (
    ALG_FERNET,
    ENV_SECRET_KEY,
    MAX_SECRET_BYTES,
    FernetVault,
    VaultError,
    generate_key,
    load_from_env,
)
from backend.persistence.models.personal import (
    CommunicationRow,
    ConnectionRow,
    PersonRow,
    SecretRow,
)
from backend.persistence.repositories import RepositoryRegistry

PERSONAL_TABLES = [
    PersonRow.__table__,
    ConnectionRow.__table__,
    CommunicationRow.__table__,
    SecretRow.__table__,
]


def _make_vault() -> FernetVault:
    return FernetVault(generate_key())


class TestRoundtrip:
    def test_encrypt_decrypt_roundtrip(self):
        vault = _make_vault()
        payload = vault.encrypt_secret("app-password-123")
        assert vault.decrypt_secret(payload) == "app-password-123"

    def test_payload_shape_is_opaque_json(self):
        vault = _make_vault()
        payload = vault.encrypt_secret("s3cret")
        assert set(payload) == {"alg", "c"}
        assert payload["alg"] == ALG_FERNET
        assert isinstance(payload["c"], str)
        assert payload["c"]
        # The payload must round-trip through JSON (it lands in a JSONB column).
        assert json.loads(json.dumps(payload)) == payload

    def test_plaintext_never_in_payload(self):
        vault = _make_vault()
        plaintext = "hunter2-super-secret-app-password"
        payload = vault.encrypt_secret(plaintext)
        assert plaintext not in json.dumps(payload, sort_keys=True)

    def test_ciphertext_is_nondeterministic(self):
        vault = _make_vault()
        first = vault.encrypt_secret("same")
        second = vault.encrypt_secret("same")
        assert first["c"] != second["c"]

    def test_unicode_roundtrip(self):
        vault = _make_vault()
        text = "pässwörd-密码-🔑"
        assert vault.decrypt_secret(vault.encrypt_secret(text)) == text

    def test_empty_plaintext_roundtrip(self):
        vault = _make_vault()
        assert vault.decrypt_secret(vault.encrypt_secret("")) == ""


class TestStableFailures:
    def test_wrong_key_is_decrypt_failed(self):
        other = _make_vault().encrypt_secret("token")
        vault = _make_vault()
        with pytest.raises(VaultError) as exc:
            vault.decrypt_secret(other)
        assert exc.value.code == "decrypt_failed"

    def test_unknown_algorithm_is_bad_payload(self):
        vault = _make_vault()
        with pytest.raises(VaultError) as exc:
            vault.decrypt_secret({"alg": "rot13", "c": "x"})
        assert exc.value.code == "bad_payload"

    def test_missing_ciphertext_is_bad_payload(self):
        vault = _make_vault()
        with pytest.raises(VaultError) as exc:
            vault.decrypt_secret({"alg": ALG_FERNET})
        assert exc.value.code == "bad_payload"

    def test_garbage_ciphertext_is_decrypt_failed(self):
        vault = _make_vault()
        with pytest.raises(VaultError) as exc:
            vault.decrypt_secret({"alg": ALG_FERNET, "c": "not-a-fernet-token"})
        assert exc.value.code == "decrypt_failed"

    def test_invalid_key_is_invalid_key(self):
        with pytest.raises(VaultError) as exc:
            FernetVault("definitely-not-a-fernet-key")
        assert exc.value.code == "invalid_key"

    def test_oversized_secret_is_rejected(self):
        vault = _make_vault()
        with pytest.raises(VaultError) as exc:
            vault.encrypt_secret("a" * (MAX_SECRET_BYTES + 1))
        assert exc.value.code == "secret_too_large"

    def test_error_carries_stable_code_and_message(self):
        with pytest.raises(VaultError) as exc:
            _make_vault().decrypt_secret({"alg": "nope", "c": "x"})
        assert exc.value.code == "bad_payload"
        assert exc.value.message
        # The raw message must not echo key material or the payload.
        assert "nope" not in exc.value.message


class TestKeyLoading:
    def test_load_from_env_unset_fails_loudly(self):
        with pytest.raises(VaultError) as exc:
            load_from_env({})
        assert exc.value.code == "no_key"

    def test_load_from_env_blank_fails_loudly(self):
        with pytest.raises(VaultError) as exc:
            load_from_env({ENV_SECRET_KEY: "   "})
        assert exc.value.code == "no_key"

    def test_load_from_env_explicit_mapping(self):
        key = generate_key()
        vault = load_from_env({ENV_SECRET_KEY: key})
        assert vault.decrypt_secret(vault.encrypt_secret("x")) == "x"

    def test_generated_key_is_usable(self):
        key = generate_key()
        assert len(key) == 44  # 32-byte urlsafe base64
        vault = FernetVault(key)
        assert vault.decrypt_secret(vault.encrypt_secret("k")) == "k"

    def test_implements_frozen_secret_codec_protocol(self):
        from backend.persistence.repositories.connections_repo import SecretCodec

        assert isinstance(_make_vault(), SecretCodec)


class TestCiphertextAtRest:
    """The durable ``secrets`` table must hold only the opaque payload."""

    async def test_secrets_row_carries_no_plaintext(self, tmp_path: Path):
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'vault.db'}",
            poolclass=StaticPool,
        )
        try:
            async with engine.begin() as conn:
                await conn.run_sync(
                    PersonRow.__table__.metadata.create_all, tables=PERSONAL_TABLES
                )
            from backend.persistence.connection import make_session_factory

            vault = _make_vault()
            registry = RepositoryRegistry(
                make_session_factory(engine), secret_codec=vault
            )
            row = await registry.connections.create_connection(
                kind="imap", label="mail", config={"host": "imap.example"}
            )
            plaintext = "app-password-at-rest"
            await registry.connections.upsert_secret(
                kind="app_password",
                owner_kind="connection",
                owner_id=row.id,
                plaintext=plaintext,
            )

            # Read the raw row straight from the DB (no codec in the loop).
            # A session (not a bare connection) is required for ORM entity
            # hydration — scalar_one() on a plain connection yields the first
            # column (the id), not the row.
            async with registry.session_factory() as session:
                stored = (
                    await session.execute(
                        select(SecretRow).where(
                            SecretRow.owner_id == row.id,
                            SecretRow.kind == "app_password",
                        )
                    )
                ).scalar_one()

            assert stored.payload["alg"] == ALG_FERNET
            assert plaintext not in json.dumps(stored.payload, sort_keys=True)
            # And the codec round-trips the stored payload end to end.
            assert vault.decrypt_secret(stored.payload) == plaintext
            assert (
                await registry.connections.get_secret(
                    kind="app_password",
                    owner_kind="connection",
                    owner_id=row.id,
                )
                == plaintext
            )
        finally:
            await engine.dispose()

    async def test_key_rotation_makes_secrets_unreadable(self, tmp_path: Path):
        """A different key cannot decrypt a stored payload (rotation, by design)."""
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'rotate.db'}",
            poolclass=StaticPool,
        )
        try:
            async with engine.begin() as conn:
                await conn.run_sync(
                    PersonRow.__table__.metadata.create_all, tables=PERSONAL_TABLES
                )
            from backend.persistence.connection import make_session_factory

            registry = RepositoryRegistry(
                make_session_factory(engine), secret_codec=_make_vault()
            )
            row = await registry.connections.create_connection(kind="imap")
            await registry.connections.upsert_secret(
                kind="relay_token",
                owner_kind="connection",
                owner_id=row.id,
                plaintext="pairing-token",
            )
            # Simulate a key rotation: a fresh vault for the same table.
            rotated = RepositoryRegistry(
                make_session_factory(engine), secret_codec=_make_vault()
            )
            with pytest.raises(VaultError) as exc:
                await rotated.connections.get_secret(
                    kind="relay_token",
                    owner_kind="connection",
                    owner_id=row.id,
                )
            assert exc.value.code == "decrypt_failed"
        finally:
            await engine.dispose()
