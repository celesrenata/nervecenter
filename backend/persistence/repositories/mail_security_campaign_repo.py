"""Durable per-account content campaigns (NS-ADR-069).

Every read and write names the account scope; signatures and band keys are
keyed per account, so campaigns never span accounts. Expired rows are pruned
on write and ignored on read.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from backend.mail_security.campaigns import MAX_CANDIDATES, THRESHOLD, TTL_DAYS, similarity
from backend.persistence.models.mail_security import MailCampaignBandRow, MailCampaignMemberRow


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class MailCampaignRepository:
    def __init__(self, session_factory: Any) -> None:
        self._sessions = session_factory

    async def record(
        self,
        account_scope: str,
        *,
        source_key: str,
        content_ref: str | None,
        sig: tuple[int, ...],
        bands: list[str],
        now: datetime,
    ) -> tuple[str, int] | None:
        """Join the best-matching live campaign or start one; idempotent.

        Returns ``(campaign_id, matched_member_count)`` for a new member, or
        ``None`` for an empty signature, a replay or a lost insert race.
        """
        if not sig or not bands:
            return None
        now = _aware(now)
        async with self._sessions() as s:
            await s.execute(
                delete(MailCampaignBandRow).where(MailCampaignBandRow.expires_at < now)
            )
            await s.execute(
                delete(MailCampaignMemberRow).where(MailCampaignMemberRow.expires_at < now)
            )
            exists = (
                await s.execute(
                    select(MailCampaignMemberRow.id).where(
                        MailCampaignMemberRow.account_scope == account_scope,
                        MailCampaignMemberRow.source_key == source_key,
                    )
                )
            ).first()
            if exists is not None:
                await s.commit()
                return None
            member_ids = list(
                dict.fromkeys(
                    r[0]
                    for r in (
                        await s.execute(
                            select(MailCampaignBandRow.member_id)
                            .where(
                                MailCampaignBandRow.account_scope == account_scope,
                                MailCampaignBandRow.band_key.in_(bands),
                            )
                            .limit(MAX_CANDIDATES * len(bands))
                        )
                    ).all()
                )
            )[:MAX_CANDIDATES]
            campaign_id: str | None = None
            matched = 0
            if member_ids:
                candidates = (
                    await s.execute(
                        select(MailCampaignMemberRow)
                        .where(
                            MailCampaignMemberRow.account_scope == account_scope,
                            MailCampaignMemberRow.id.in_(member_ids),
                        )
                        .order_by(MailCampaignMemberRow.created_at)
                    )
                ).scalars()
                for c in candidates:
                    if similarity(sig, c.signature or ()) >= THRESHOLD:
                        matched += 1
                        campaign_id = campaign_id or c.campaign_id
            campaign_id = campaign_id or uuid.uuid4().hex
            expires = now + timedelta(days=TTL_DAYS)
            member_id = str(uuid.uuid4())
            s.add(
                MailCampaignMemberRow(
                    id=member_id,
                    expires_at=expires,
                    account_scope=account_scope,
                    source_key=source_key,
                    content_ref=content_ref,
                    campaign_id=campaign_id,
                    signature=list(sig),
                )
            )
            for band in dict.fromkeys(bands):
                s.add(
                    MailCampaignBandRow(
                        id=str(uuid.uuid4()),
                        account_scope=account_scope,
                        band_key=band,
                        member_id=member_id,
                        expires_at=expires,
                    )
                )
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                return None
            return campaign_id, matched

    async def live_members(self, account_scope: str, now: datetime) -> list[tuple[str, str]]:
        """``(source_key, campaign_id)`` of unexpired members in campaigns of 2+."""
        now = _aware(now)
        async with self._sessions() as s:
            rows = (
                await s.execute(
                    select(MailCampaignMemberRow.source_key, MailCampaignMemberRow.campaign_id)
                    .where(
                        MailCampaignMemberRow.account_scope == account_scope,
                        MailCampaignMemberRow.expires_at >= now,
                    )
                )
            ).all()
        sizes: dict[str, int] = {}
        for _src, cid in rows:
            sizes[cid] = sizes.get(cid, 0) + 1
        return [(str(src), str(cid)) for src, cid in rows if sizes[cid] > 1]


__all__ = ["MailCampaignRepository"]
