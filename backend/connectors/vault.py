"""Fernet-backed secret vault for the connector control plane.

Implements the :class:`~backend.persistence.repositories.connections_repo.SecretCodec`
protocol from the frozen durable layer (workstream F). The durable
``secrets`` table stores only the opaque codec payload — this vault is the
single place where a key touches a credential.

Contract (spec ``personal-ops-recovery`` §3):

- Ciphertext payload is an opaque, JSON-serialisable dict —
  ``{"alg": "fernet", "c": <urlsafe token>}``.  Nothing about the plaintext
  (length, first/last chars, …) may leak through the payload.
- Round-trip is guaranteed: ``decrypt_secret(encrypt_secret(x)) == x``.
- The key comes from ``NERVE_SECRET_KEY`` (a 32-byte Fernet key, i.e. a
  44-char urlsafe-base64 string).  When the variable is unset,
  :func:`load_from_env` **fails loudly** — there is no silent identity
  fallback in production.  Tests inject their own key via
  :func:`load_from_env` with an explicit env mapping, or use the durable
  layer's ``IdentitySecretCodec`` at the repository level only.
- Every failure raises :class:`VaultError` (a single typed error) so the
  endpoint layer can map it to a stable error code instead of leaking
  internal messages.
"""

from __future__ import annotations

import os
from typing import Any, Mapping

from cryptography.fernet import Fernet, InvalidToken

#: Environment variable carrying the 32-byte urlsafe Fernet key.
ENV_SECRET_KEY = "NERVE_SECRET_KEY"

#: The algorithm tag recorded in every stored payload.
ALG_FERNET = "fernet"

#: Maximum bytes a single secret may hold (credentials, tokens, …).
MAX_SECRET_BYTES = 64 * 1024


class VaultError(Exception):
    """Single typed failure for every vault operation.

    Carries a *stable* ``code`` the endpoints map onto machine-readable
    error codes (never the raw exception text, which could echo key
    material or internal paths).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class FernetVault:
    """A :class:`SecretCodec` implementation backed by :class:`Fernet`.

    Construct directly with a key (tests) or via :func:`load_from_env`.
    ``decrypt_secret`` validates the ``alg`` tag so a payload encrypted by
    a different codec can never be silently misinterpreted.
    """

    def __init__(self, key: str | bytes) -> None:
        self._fernet = self._make_fernet(key)

    # -- SecretCodec protocol -------------------------------------------------

    def encrypt_secret(self, plaintext: str) -> dict[str, Any]:
        """Encrypt ``plaintext`` into an opaque JSON-serialisable payload.

        The plaintext is never stored or logged; only the Fernet token is.
        """

        data = plaintext.encode("utf-8")
        if len(data) > MAX_SECRET_BYTES:
            raise VaultError(
                "secret_too_large",
                f"secret exceeds {MAX_SECRET_BYTES} bytes",
            )
        token = self._fernet.encrypt(data)
        return {"alg": ALG_FERNET, "c": token.decode("ascii")}

    def decrypt_secret(self, payload: dict[str, Any]) -> str:
        """Recover the plaintext from a stored payload.

        Raises :class:`VaultError` (codes ``bad_payload`` /
        ``decrypt_failed``) for anything that is not a payload this vault
        produced — including payloads from a different key or codec.
        """

        if not isinstance(payload, dict):
            raise VaultError("bad_payload", "secret payload is not an object")
        if payload.get("alg") != ALG_FERNET:
            raise VaultError("bad_payload", "secret payload has an unknown algorithm")
        token = payload.get("c")
        if not isinstance(token, str) or not token:
            raise VaultError("bad_payload", "secret payload is missing its ciphertext")
        try:
            data = self._fernet.decrypt(token.encode("ascii"))
        except InvalidToken as exc:  # noqa: PERF203 - single narrow path
            raise VaultError(
                "decrypt_failed",
                "secret cannot be decrypted with the current key",
            ) from exc
        return data.decode("utf-8")

    # -- internals --------------------------------------------------------------

    @staticmethod
    def _make_fernet(key: str | bytes) -> Fernet:
        try:
            key_bytes = key if isinstance(key, bytes) else key.encode("ascii")
            return Fernet(key_bytes)
        except Exception as exc:
            raise VaultError(
                "invalid_key",
                "NERVE_SECRET_KEY must be a valid 32-byte urlsafe-base64 Fernet key",
            ) from exc


def load_from_env(env: Mapping[str, str] | None = None) -> FernetVault:
    """Build a vault from ``NERVE_SECRET_KEY``.

    Fails loudly with :class:`VaultError` (code ``no_key``) when the
    variable is unset or empty — production must never fall back to an
    identity codec.
    """

    source = os.environ if env is None else env
    key = source.get(ENV_SECRET_KEY, "").strip()
    if not key:
        raise VaultError(
            "no_key",
            f"{ENV_SECRET_KEY} is not set; generate one with "
            "FernetVault.generate_key()",
        )
    return FernetVault(key)


def generate_key() -> str:
    """Generate a fresh 32-byte urlsafe-base64 Fernet key for operators.

    Print the result into ``NERVE_SECRET_KEY``.  Regenerating a key makes
    every previously stored secret unreadable (by design — it is a key
    rotation, not an upgrade).
    """

    return Fernet.generate_key().decode("ascii")


__all__ = [
    "ALG_FERNET",
    "ENV_SECRET_KEY",
    "FernetVault",
    "MAX_SECRET_BYTES",
    "VaultError",
    "generate_key",
    "load_from_env",
]
