"""Connection-plane repository: connections and secrets.

``secrets`` stores only the *encrypted* payload. The actual encryption hook
(Fernet) lands in workstream B's vault; this module defines the
:class:`SecretCodec` protocol and an :class:`IdentitySecretCodec` so tests can
exercise the durable boundary without a real key. Repositories accept the
codec so the real implementation can be swapped in later without touching the
repository surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import select

from ..models.personal import ConnectionRow, SecretRow

#: Owner kinds a secret may be attached to (``connections`` today; the
#: connector control plane may extend this).
OWNER_KINDS = ("connection", "person")


@runtime_checkable
class SecretCodec(Protocol):
    """Encrypt/decrypt hook for secret payloads.

    The real implementation (workstream B's Fernet vault) implements this
    protocol; the durable layer only cares that ``decrypt_secret(
    encrypt_secret(x)) == x``.
    """

    def encrypt_secret(self, plaintext: str) -> dict[str, Any]:
        """Return an opaque, JSON-serialisable ciphertext structure."""
        ...

    def decrypt_secret(self, payload: dict[str, Any]) -> str:
        """Recover the original plaintext from a stored payload."""
        ...


@dataclass(frozen=True, slots=True)
class IdentitySecretCodec:
    """No-op codec used in tests — the *durable* boundary under test, not a
    production codec. Plaintext round-trips verbatim, so the repository
    logic (upsert / get / delete) is exercised end-to-end without a key."""

    def encrypt_secret(self, plaintext: str) -> dict[str, Any]:
        return {"c": plaintext}

    def decrypt_secret(self, payload: dict[str, Any]) -> str:
        return payload.get("c", "")


def _to_dict(row: Any) -> dict[str, Any]:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


class ConnectionsRepository:
    """Durable access to :class:`ConnectionRow` and :class:`SecretRow`."""

    def __init__(
        self,
        session_factory: Any,
        *,
        secret_codec: SecretCodec | None = None,
    ) -> None:
        self._sessions = session_factory
        self._codec: SecretCodec = secret_codec or IdentitySecretCodec()

    # -- connections --------------------------------------------------------

    async def create_connection(
        self,
        *,
        kind: str,
        label: str | None = None,
        config: dict[str, Any] | None = None,
        auth_status: str = "unconfigured",
        operational_status: str = "never_synced",
        enabled: bool = True,
    ) -> ConnectionRow:
        """Insert a new connection row and return it."""

        async with self._sessions() as session:
            row = ConnectionRow(
                kind=kind,
                label=label,
                config=config or {},
                auth_status=auth_status,
                operational_status=operational_status,
                enabled=enabled,
            )
            session.add(row)
            await session.commit()
            return row

    async def get_connection(self, connection_id: str) -> ConnectionRow | None:
        async with self._sessions() as session:
            return await session.get(ConnectionRow, connection_id)

    async def list_connections(self, *, enabled_only: bool = False) -> list[ConnectionRow]:
        async with self._sessions() as session:
            stmt = select(ConnectionRow)
            if enabled_only:
                stmt = stmt.where(ConnectionRow.enabled.is_(True))
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def update_connection(
        self,
        connection_id: str,
        **fields: Any,
    ) -> ConnectionRow | None:
        """Update the named fields on a connection and return the row."""

        async with self._sessions() as session:
            row = await session.get(ConnectionRow, connection_id)
            if row is None:
                return None
            for name, value in fields.items():
                setattr(row, name, value)
            await session.commit()
            return row

    # -- secrets (encrypted) -----------------------------------------------

    async def upsert_secret(
        self,
        *,
        kind: str,
        owner_kind: str,
        owner_id: str,
        plaintext: str,
    ) -> SecretRow:
        """Encrypt ``plaintext`` via the codec and upsert the secret row.

        A secret is addressed by ``(kind, owner_kind, owner_id)``; an existing
        row for that key is updated in place, otherwise a new row is created.
        The plaintext is never stored — only the codec's ciphertext.
        """

        payload = self._codec.encrypt_secret(plaintext)
        async with self._sessions() as session:
            stmt = select(SecretRow).where(
                SecretRow.kind == kind,
                SecretRow.owner_kind == owner_kind,
                SecretRow.owner_id == owner_id,
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                row = SecretRow(kind=kind, owner_kind=owner_kind, owner_id=owner_id)
                session.add(row)
            row.payload = payload
            await session.commit()
            return row

    async def get_secret(
        self,
        *,
        kind: str,
        owner_kind: str,
        owner_id: str,
    ) -> str | None:
        """Decrypt and return the secret's plaintext, or ``None`` if absent."""

        async with self._sessions() as session:
            stmt = select(SecretRow).where(
                SecretRow.kind == kind,
                SecretRow.owner_kind == owner_kind,
                SecretRow.owner_id == owner_id,
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return self._codec.decrypt_secret(row.payload)

    async def delete_secret(
        self, *, kind: str, owner_kind: str, owner_id: str
    ) -> bool:
        """Delete the secret for the key; return whether a row was removed."""

        async with self._sessions() as session:
            stmt = select(SecretRow).where(
                SecretRow.kind == kind,
                SecretRow.owner_kind == owner_kind,
                SecretRow.owner_id == owner_id,
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    # -- convenience --------------------------------------------------------

    @staticmethod
    def to_dict(row: ConnectionRow | SecretRow) -> dict[str, Any]:
        """Plain-dict view of a row (handy for workstreams shaping payloads)."""
        return _to_dict(row)


__all__ = [
    "ConnectionsRepository",
    "SecretCodec",
    "IdentitySecretCodec",
    "OWNER_KINDS",
]
