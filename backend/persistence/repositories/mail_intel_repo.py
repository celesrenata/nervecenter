"""NerveShield threat-intel repository (Phase 2, Task 2.2).

Async, session-scoped storage for the global indicator table and the
account-scoped override table (see :mod:`backend.mail_security.intel` for the
pure rules). Contracts:

- **Expiry (R-006.3, T-19):** :meth:`lookup` returns only *live* indicators —
  enabled, not rolled back, not superseded and strictly before ``expires_at``
  by the injected clock. Non-live rows stay in storage for audit
  (:meth:`audit_history`); nothing is ever deleted.
- **Versioning (R-006.5):** every explicit :meth:`promote` that changes
  anything takes the next monotonic version. :meth:`rollback` retires rows
  promoted after the target version and restores rows they superseded.
  Both support ``dry_run`` (computes the plan, writes nothing). Neither runs
  automatically. Operator-triggered, single-writer: concurrent promotions are
  not serialized here (documented risk).
- **Overrides (T-30, R-023):** every override read/write is filtered by
  ``account_scope`` *and* sender *and* indicator; suppressed matches are
  returned as evidence, not erased. An override never touches the indicator
  table.
- **Privacy:** only canonical indicators / hashes and opaque refs are stored.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from backend.mail_security.intel import (
    MAX_BATCH,
    ConfidenceType,
    IndicatorType,
    IntelChange,
    IntelError,
    IntelErrorCode,
    IntelEvaluation,
    IntelMatch,
    IntelRecord,
    PromotionPlan,
    SuppressedMatch,
    aware,
    check_ttl,
    is_live,
    lookup_candidates,
    validate_override_scope,
)
from backend.persistence.models.mail_security import (
    MailIntelIndicatorRow,
    MailIntelOverrideRow,
)

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _row_live(row: MailIntelIndicatorRow, now: datetime) -> bool:
    return is_live(
        enabled=row.enabled,
        rolled_back=row.rolled_back,
        superseded=row.superseded_in_version is not None,
        expires_at=row.expires_at,
        now=now,
    )


class MailIntelRepository:
    """Threat-intel store access with an injected clock."""

    def __init__(self, session_factory: Any, *, clock: Clock | None = None) -> None:
        self._sessions = session_factory
        self._clock: Clock = clock if clock is not None else _utc_now

    # -- versions -----------------------------------------------------------

    @staticmethod
    async def _versions(session: Any) -> tuple[int, int]:
        """Return ``(current_version, max_version_ever)``."""
        ever = (
            await session.execute(select(func.max(MailIntelIndicatorRow.promotion_version)))
        ).scalar_one_or_none()
        cur = (
            await session.execute(
                select(func.max(MailIntelIndicatorRow.promotion_version)).where(
                    MailIntelIndicatorRow.rolled_back.is_(False)
                )
            )
        ).scalar_one_or_none()
        return int(cur or 0), int(ever or 0)

    async def current_version(self) -> int:
        async with self._sessions() as session:
            return (await self._versions(session))[0]

    # -- promotion ----------------------------------------------------------

    async def promote(
        self, records: Sequence[IntelRecord], *, dry_run: bool = False
    ) -> PromotionPlan:
        """Explicitly promote validated records as the next store version.

        ``dry_run=True`` replays the decision and returns the plan without any
        write. Re-promoting identical ``(key, feed_version)`` is a no-op and
        consumes no version.
        """
        now = self._clock()
        if len(records) > MAX_BATCH:
            raise IntelError(IntelErrorCode.BATCH_TOO_LARGE)
        keys = [r.key for r in records]
        if len(set(keys)) != len(keys):
            raise IntelError(IntelErrorCode.DUPLICATE_IN_BATCH)
        for record in records:
            check_ttl(record.expires_at, now)  # may have expired since validation
        async with self._sessions() as session:
            current, ever = await self._versions(session)
            next_version = ever + 1
            changes: list[IntelChange] = []
            to_write: list[MailIntelIndicatorRow] = []
            to_supersede: list[MailIntelIndicatorRow] = []
            for rec in records:
                existing = (
                    (
                        await session.execute(
                            select(MailIntelIndicatorRow).where(
                                MailIntelIndicatorRow.indicator_type == rec.indicator_type.value,
                                MailIntelIndicatorRow.canonical_indicator
                                == rec.canonical_indicator,
                                MailIntelIndicatorRow.source == rec.source,
                                MailIntelIndicatorRow.rolled_back.is_(False),
                                MailIntelIndicatorRow.superseded_in_version.is_(None),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if any(e.feed_version == rec.feed_version for e in existing):
                    action = "UNCHANGED"
                else:
                    action = "REPLACED" if existing else "ADDED"
                    to_supersede.extend(existing)
                    to_write.append(
                        MailIntelIndicatorRow(
                            indicator_type=rec.indicator_type.value,
                            canonical_indicator=rec.canonical_indicator,
                            source=rec.source,
                            confidence_type=rec.confidence_type.value,
                            license=rec.license,
                            provenance=rec.provenance,
                            scope=rec.scope.value,
                            feed_version=rec.feed_version,
                            first_seen=rec.first_seen,
                            last_seen=rec.last_seen,
                            expires_at=rec.expires_at,
                            enabled=rec.enabled,
                            promotion_version=next_version,
                            superseded_in_version=None,
                            rolled_back=False,
                        )
                    )
                changes.append(
                    IntelChange(
                        rec.indicator_type.value,
                        rec.canonical_indicator,
                        rec.source,
                        action,
                    )
                )
            if not to_write:
                return PromotionPlan(current, current, dry_run, tuple(changes))
            plan = PromotionPlan(current, next_version, dry_run, tuple(changes))
            if dry_run:
                return plan
            for old in to_supersede:
                old.superseded_in_version = next_version
            session.add_all(to_write)
            await session.commit()
            return plan

    async def rollback(self, to_version: int, *, dry_run: bool = False) -> PromotionPlan:
        """Explicitly roll back to ``to_version`` (R-006.5). Rows are retained."""
        async with self._sessions() as session:
            current, _ = await self._versions(session)
            if not isinstance(to_version, int) or to_version < 0 or to_version >= current:
                raise IntelError(IntelErrorCode.INVALID_ROLLBACK_TARGET)
            retire = (
                (
                    await session.execute(
                        select(MailIntelIndicatorRow).where(
                            MailIntelIndicatorRow.promotion_version > to_version,
                            MailIntelIndicatorRow.rolled_back.is_(False),
                        )
                    )
                )
                .scalars()
                .all()
            )
            restore = (
                (
                    await session.execute(
                        select(MailIntelIndicatorRow).where(
                            MailIntelIndicatorRow.superseded_in_version > to_version,
                            MailIntelIndicatorRow.promotion_version <= to_version,
                            MailIntelIndicatorRow.rolled_back.is_(False),
                        )
                    )
                )
                .scalars()
                .all()
            )
            changes = tuple(
                IntelChange(r.indicator_type, r.canonical_indicator, r.source, "ROLLED_BACK")
                for r in retire
            ) + tuple(
                IntelChange(r.indicator_type, r.canonical_indicator, r.source, "RESTORED")
                for r in restore
            )
            plan = PromotionPlan(current, to_version, dry_run, changes)
            if dry_run:
                return plan
            for row in retire:
                row.rolled_back = True
            for row in restore:
                row.superseded_in_version = None
            await session.commit()
            return plan

    async def set_enabled(
        self,
        indicator_type: IndicatorType,
        canonical_indicator: str,
        source: str,
        *,
        enabled: bool,
    ) -> int:
        """Enable/disable matching rows (kept in storage). Returns rows changed."""
        async with self._sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(MailIntelIndicatorRow).where(
                            MailIntelIndicatorRow.indicator_type == indicator_type.value,
                            MailIntelIndicatorRow.canonical_indicator == canonical_indicator,
                            MailIntelIndicatorRow.source == source,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                row.enabled = enabled
            await session.commit()
            return len(rows)

    # -- lookup -------------------------------------------------------------

    async def lookup(self, indicator_type: IndicatorType, value: str) -> list[IntelMatch]:
        """Return only *live* matches. No data or invalid input → ``[]`` (UNKNOWN)."""
        candidates = lookup_candidates(indicator_type, value)
        if not candidates:
            return []
        now = self._clock()
        methods = dict(candidates)
        async with self._sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(MailIntelIndicatorRow).where(
                            MailIntelIndicatorRow.indicator_type == indicator_type.value,
                            MailIntelIndicatorRow.canonical_indicator.in_(list(methods)),
                        )
                    )
                )
                .scalars()
                .all()
            )
        out = [
            IntelMatch(
                indicator_id=r.id,
                indicator_type=IndicatorType(r.indicator_type),
                canonical_indicator=r.canonical_indicator,
                source=r.source,
                confidence_type=ConfidenceType(r.confidence_type),
                feed_version=r.feed_version,
                first_seen=aware(r.first_seen),
                last_seen=aware(r.last_seen),
                expires_at=aware(r.expires_at),
                promotion_version=r.promotion_version,
                match_method=methods[r.canonical_indicator],
            )
            for r in rows
            if _row_live(r, now)
        ]
        out.sort(key=lambda m: (m.canonical_indicator, m.source, m.feed_version))
        return out

    async def audit_history(
        self, indicator_type: IndicatorType, canonical_indicator: str
    ) -> list[MailIntelIndicatorRow]:
        """All rows (live or not) for audit; expired intel is never erased."""
        async with self._sessions() as session:
            return list(
                (
                    await session.execute(
                        select(MailIntelIndicatorRow)
                        .where(
                            MailIntelIndicatorRow.indicator_type == indicator_type.value,
                            MailIntelIndicatorRow.canonical_indicator == canonical_indicator,
                        )
                        .order_by(MailIntelIndicatorRow.promotion_version)
                    )
                )
                .scalars()
                .all()
            )

    # -- overrides ----------------------------------------------------------

    async def add_override(
        self,
        *,
        account_scope: str,
        sender: str,
        indicator_type: IndicatorType,
        indicator_value: str,
        approval_ref: str | None,
        expires_at: datetime,
    ) -> MailIntelOverrideRow:
        """Create an override scoped to account + sender + indicator + expiry."""
        canonical = validate_override_scope(
            account_scope=account_scope,
            sender=sender,
            indicator_type=indicator_type,
            indicator_value=indicator_value,
            approval_ref=approval_ref,
            expires_at=expires_at,
            now=self._clock(),
        )
        async with self._sessions() as session:
            row = MailIntelOverrideRow(
                account_scope=account_scope,
                sender_key=sender,
                indicator_type=indicator_type.value,
                canonical_indicator=canonical,
                approval_ref=approval_ref,
                expires_at=expires_at,
            )
            session.add(row)
            await session.commit()
            return row

    async def revoke_override(self, account_scope: str, override_id: str) -> bool:
        """Revoke an override owned by ``account_scope`` (other accounts: no-op)."""
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(MailIntelOverrideRow).where(
                        MailIntelOverrideRow.account_scope == account_scope,
                        MailIntelOverrideRow.id == override_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            row.revoked_at = self._clock()
            await session.commit()
            return True

    async def evaluate(
        self,
        *,
        account_scope: str,
        sender: str,
        indicator_type: IndicatorType,
        value: str,
    ) -> IntelEvaluation:
        """Live matches for this account+sender, split into active and suppressed.

        Only an unexpired, unrevoked override whose account, sender, indicator
        type and canonical indicator all equal the match's suppresses it.
        """
        matches = await self.lookup(indicator_type, value)
        if not matches:
            return IntelEvaluation((), ())
        now = self._clock()
        async with self._sessions() as session:
            overrides = (
                (
                    await session.execute(
                        select(MailIntelOverrideRow).where(
                            MailIntelOverrideRow.account_scope == account_scope,
                            MailIntelOverrideRow.sender_key == sender,
                            MailIntelOverrideRow.indicator_type == indicator_type.value,
                            MailIntelOverrideRow.canonical_indicator.in_(
                                [m.canonical_indicator for m in matches]
                            ),
                            MailIntelOverrideRow.revoked_at.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
        active = {
            o.canonical_indicator: o for o in overrides if aware(o.expires_at) > aware(now)
        }
        kept: list[IntelMatch] = []
        suppressed: list[SuppressedMatch] = []
        for m in matches:
            ov = active.get(m.canonical_indicator)
            if ov is None:
                kept.append(m)
            else:
                suppressed.append(
                    SuppressedMatch(m, ov.approval_ref, aware(ov.expires_at))
                )
        return IntelEvaluation(tuple(kept), tuple(suppressed))


__all__ = ["MailIntelRepository"]
