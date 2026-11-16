"""Vault-backed persistence for the OmniRoute keys (NS-ADR-117).

Values are encrypted by the app's Fernet vault through ``ConnectionsRepository``
(table ``secrets``). Writes are refused when no encrypting vault is loaded, so a
key is never stored in the clear. Every change is appended to
``omniroute_key_audit`` (who, when, which key, outcome; never the value).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select

from backend.inference import key_resolver as kr
from backend.persistence.models.omniroute_keys import OmniRouteKeyAudit
from backend.persistence.models.personal import SecretRow

log = structlog.get_logger(__name__)


class VaultUnavailableError(Exception):
    """No encrypting vault is loaded; keys cannot be stored."""


class OmniRouteKeyStore:
    def __init__(self, registry: Any, vault: Any) -> None:
        self._registry = registry
        self._vault = vault

    @property
    def writable(self) -> bool:
        return self._vault is not None

    def _address(self, kind: str) -> dict[str, str]:
        return {
            "kind": kr.SECRET_KIND[kind],
            "owner_kind": kr.SECRET_OWNER_KIND,
            "owner_id": kr.SECRET_OWNER_ID,
        }

    async def set(self, kind: str, value: str) -> None:
        if self._vault is None:
            raise VaultUnavailableError
        await self._registry.connections.upsert_secret(plaintext=value, **self._address(kind))

    async def clear(self, kind: str) -> bool:
        return await self._registry.connections.delete_secret(**self._address(kind))

    async def refresh(self) -> None:
        """Reload the vault values into the resolver; failures keep the old copy."""
        if self._vault is None:
            return
        values: dict[str, str] = {}
        changed: dict[str, datetime | None] = {}
        try:
            async with self._registry.session_factory() as session:
                for kind in kr.KINDS:
                    addr = self._address(kind)
                    row = (
                        await session.execute(
                            select(SecretRow).where(
                                SecretRow.kind == addr["kind"],
                                SecretRow.owner_kind == addr["owner_kind"],
                                SecretRow.owner_id == addr["owner_id"],
                            )
                        )
                    ).scalar_one_or_none()
                    if row is None:
                        continue
                    try:
                        values[kind] = self._vault.decrypt_secret(row.payload)
                    except Exception as exc:  # noqa: BLE001 - code only, never the payload
                        log.warning(
                            "omniroute_keys.decrypt_failed", key=kind, code=type(exc).__name__
                        )
                        continue
                    changed[kind] = row.updated_at
        except Exception as exc:  # noqa: BLE001 - keep serving the cached values
            log.warning("omniroute_keys.refresh_failed", error=type(exc).__name__)
            return
        kr.apply_vault_values(values, changed)

    async def audit(self, *, actor: str, key: str, action: str, outcome: str) -> None:
        """Append one audit row and log it; never carries the value."""
        log.info("omniroute_keys.audit", actor=actor, key=key, action=action, outcome=outcome)
        try:
            async with self._registry.session_factory() as session:
                session.add(
                    OmniRouteKeyAudit(
                        at=datetime.now(UTC),
                        actor=actor[:200],
                        key_kind=key,
                        action=action,
                        outcome=outcome[:64],
                    )
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - the log line above is the fallback
            log.warning("omniroute_keys.audit_write_failed", error=type(exc).__name__)

    async def recent_audit(self, limit: int = 20) -> list[dict[str, Any]]:
        try:
            async with self._registry.session_factory() as session:
                rows = (
                    (
                        await session.execute(
                            select(OmniRouteKeyAudit)
                            .order_by(OmniRouteKeyAudit.at.desc())
                            .limit(limit)
                        )
                    )
                    .scalars()
                    .all()
                )
        except Exception:  # noqa: BLE001 - table may not exist yet
            return []
        return [
            {
                "at": r.at.isoformat() if r.at else None,
                "actor": r.actor,
                "key": r.key_kind,
                "action": r.action,
                "outcome": r.outcome,
            }
            for r in rows
        ]
