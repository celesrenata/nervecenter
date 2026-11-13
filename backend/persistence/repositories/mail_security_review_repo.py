"""Read-only loader for the NerveShield review queue (Task 7.3).

Builds :class:`~backend.mail_security.review_queue.QueueCandidate` inputs for
one account scope from the durable plane. Reads ids, codes, flags and
timestamps only: never subject, body, sender address or commitment text.

Joins (all scope-bound through the assessment's own ``account_scope``):

* flagged assessments (REVIEW / RESTRICTED / INCOMPLETE) in the scope;
* their findings (category, severity, status) and policy reason codes;
* whether any feedback exists for them (reviewed -> excluded from queue);
* job ``source_key`` -> job ``meta.content_ref`` -> ``communications`` row
  whose ref must sit under this scope's own connection prefix;
* the communication's person ``vip`` flag (important human, R-013.5);
* open commitments (``source_communication_id``), open follow-ups and active
  attention items whose refs name the communication: preserved as review
  candidates by id only.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select

from backend.mail_security.backfill import parse_content_ref
from backend.mail_security.campaigns import FLAGGED_DISPOSITIONS, siblings_of_flagged
from backend.mail_security.expectedness import unexpected_high_impact
from backend.mail_security.review_feedback import verification_advice
from backend.mail_security.review_queue import (
    QUEUED_DISPOSITIONS,
    THREAD_WEIGHTS,
    CommitmentRef,
    QueueCandidate,
)
from backend.persistence.models.mail_security import (
    MailCampaignMemberRow,
    MailSecurityAssessmentRow,
    MailSecurityFeedbackRow,
    MailSecurityFindingRow,
    MailSecurityHeldPromotionRow,
    MailSecurityJobRow,
    MailSemanticObservationRow,
    MailThreadLedgerRow,
)
from backend.persistence.models.personal import (
    AttentionItemRow,
    CommitmentRow,
    CommunicationRow,
    FollowupRow,
    PersonRow,
)

#: Upper bound on flagged assessments considered per scope per request.
MAX_CANDIDATES = 2000
_OPEN = ("open",)
_ACTIVE_ATTENTION = ("active", "snoozed")


def _names_communication(ref: Any, comm_id: str) -> bool:
    """Does a stored ref (string or mapping) point at ``comm_id``?"""
    if isinstance(ref, str):
        return ref == comm_id or ref.endswith(f":{comm_id}")
    if isinstance(ref, Mapping):
        return any(v == comm_id for v in ref.values() if isinstance(v, str))
    return False


def _match(rows: Iterable[tuple[str, Any]], comm_ids: set[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for row_id, refs in rows:
        for ref in refs or []:
            for cid in comm_ids:
                if _names_communication(ref, cid):
                    out[cid].append(row_id)
    return out


async def _thread_flags(s: Any, account_scope: str) -> dict[str, str]:
    """``source_key -> SUSPECT/BROKEN`` from the SHADOW ledger (NS-ADR-067),
    joined through the job's body ``content_ref`` within this scope."""
    flagged = {
        str(ref): str(st)
        for ref, st in (
            await s.execute(
                select(MailThreadLedgerRow.content_ref, MailThreadLedgerRow.status).where(
                    MailThreadLedgerRow.account_scope == account_scope,
                    MailThreadLedgerRow.status.in_(sorted(THREAD_WEIGHTS)),
                    MailThreadLedgerRow.content_ref.is_not(None),
                )
            )
        ).all()
    }
    if not flagged:
        return {}
    out: dict[str, str] = {}
    for source_key, meta in (
        await s.execute(
            select(MailSecurityJobRow.source_key, MailSecurityJobRow.meta).where(
                MailSecurityJobRow.account_scope == account_scope
            )
        )
    ).all():
        ref = (meta or {}).get("content_ref") if isinstance(meta, Mapping) else None
        if isinstance(ref, str) and ref in flagged:
            out[source_key] = flagged[ref]
    return out


async def _campaign_flags(s: Any, account_scope: str) -> dict[str, int]:
    """``source_key -> OTHER flagged members`` of its live content campaign
    (NS-ADR-069), within this scope only."""
    now = datetime.now(UTC)
    rows = (
        await s.execute(
            select(MailCampaignMemberRow.source_key, MailCampaignMemberRow.campaign_id).where(
                MailCampaignMemberRow.account_scope == account_scope,
                MailCampaignMemberRow.expires_at >= now,
            )
        )
    ).all()
    if not rows:
        return {}
    sources = sorted({str(r[0]) for r in rows})
    flagged = {
        str(r[0])
        for r in (
            await s.execute(
                select(MailSecurityAssessmentRow.source_id).where(
                    MailSecurityAssessmentRow.account_scope == account_scope,
                    MailSecurityAssessmentRow.source_id.in_(sources),
                    MailSecurityAssessmentRow.disposition.in_(sorted(FLAGGED_DISPOSITIONS)),
                )
            )
        ).all()
    }
    return siblings_of_flagged(((str(a), str(b)) for a, b in rows), flagged)


async def _unexpected_asks(s: Any, account_scope: str) -> set[str]:
    """Sources whose SHADOW observation holds an UNEXPECTED high-impact ask
    (NS-ADR-075), newest observations first, bounded."""
    rows = (
        await s.execute(
            select(MailSemanticObservationRow.source_key, MailSemanticObservationRow.observation)
            .where(
                MailSemanticObservationRow.account_scope == account_scope,
                MailSemanticObservationRow.status == "COMPLETED",
            )
            .order_by(MailSemanticObservationRow.created_at.desc())
            .limit(MAX_CANDIDATES)
        )
    ).all()
    return {
        str(src)
        for src, obs in rows
        if isinstance(obs, Mapping) and unexpected_high_impact(obs.get("expectedness") or [])
    }


async def load_queue_candidates(session_factory: Any, account_scope: str) -> list[QueueCandidate]:
    async with session_factory() as s:
        thread_flags = await _thread_flags(s, account_scope)
        campaign_flags = await _campaign_flags(s, account_scope)
        unexpected = await _unexpected_asks(s, account_scope)
        queued = MailSecurityAssessmentRow.disposition.in_(sorted(QUEUED_DISPOSITIONS))
        extra_sources = sorted(set(thread_flags) | set(campaign_flags) | unexpected)
        if extra_sources:
            queued = or_(queued, MailSecurityAssessmentRow.source_id.in_(extra_sources))
        assessments = (
            (
                await s.execute(
                    select(MailSecurityAssessmentRow)
                    .where(
                        MailSecurityAssessmentRow.account_scope == account_scope,
                        queued,
                    )
                    .order_by(MailSecurityAssessmentRow.created_at.desc())
                    .limit(MAX_CANDIDATES)
                )
            )
            .scalars()
            .all()
        )
        if not assessments:
            return []
        # Newest revision per source only.
        latest: dict[str, MailSecurityAssessmentRow] = {}
        for a in assessments:
            cur = latest.get(a.source_id)
            if cur is None or a.revision > cur.revision:
                latest[a.source_id] = a
        rows = list(latest.values())
        ids = [a.assessment_id for a in rows]

        findings: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        for aid, cat, sev, st in (
            await s.execute(
                select(
                    MailSecurityFindingRow.assessment_id,
                    MailSecurityFindingRow.category,
                    MailSecurityFindingRow.severity,
                    MailSecurityFindingRow.status,
                ).where(
                    MailSecurityFindingRow.account_scope == account_scope,
                    MailSecurityFindingRow.assessment_id.in_(ids),
                )
            )
        ).all():
            findings[aid].append((cat, sev, st))

        reviewed = {
            r[0]
            for r in (
                await s.execute(
                    select(MailSecurityFeedbackRow.assessment_id).where(
                        MailSecurityFeedbackRow.account_scope == account_scope,
                        MailSecurityFeedbackRow.assessment_id.in_(ids),
                    )
                )
            ).all()
        }

        ref_by_source: dict[str, str] = {}
        for source_key, meta in (
            await s.execute(
                select(MailSecurityJobRow.source_key, MailSecurityJobRow.meta).where(
                    MailSecurityJobRow.account_scope == account_scope,
                    MailSecurityJobRow.source_key.in_(list(latest)),
                )
            )
        ).all():
            ref = (meta or {}).get("content_ref") if isinstance(meta, Mapping) else None
            parsed = parse_content_ref(ref)
            # The ref must belong to this scope's own connection.
            if parsed is not None and f"conn:{parsed.connection_id}" == account_scope:
                ref_by_source.setdefault(source_key, str(ref))

        comm_by_ref: dict[str, tuple[str, str | None]] = {}
        if ref_by_source:
            for cid, ref, pid in (
                await s.execute(
                    select(
                        CommunicationRow.id,
                        CommunicationRow.content_ref,
                        CommunicationRow.person_id,
                    ).where(CommunicationRow.content_ref.in_(sorted(set(ref_by_source.values()))))
                )
            ).all():
                comm_by_ref[ref] = (cid, pid)

        person_ids = {pid for _cid, pid in comm_by_ref.values() if pid}
        vip: set[str] = set()
        if person_ids:
            vip = {
                r[0]
                for r in (
                    await s.execute(
                        select(PersonRow.id).where(
                            PersonRow.id.in_(sorted(person_ids)), PersonRow.vip.is_(True)
                        )
                    )
                ).all()
            }

        comm_ids = {cid for cid, _pid in comm_by_ref.values()}
        commitments: dict[str, list[CommitmentRef]] = defaultdict(list)
        if comm_ids:
            for row_id, cid in (
                await s.execute(
                    select(CommitmentRow.id, CommitmentRow.source_communication_id).where(
                        CommitmentRow.source_communication_id.in_(sorted(comm_ids)),
                        CommitmentRow.state.in_(_OPEN),
                    )
                )
            ).all():
                commitments[cid].append(CommitmentRef("commitment", str(row_id)))
            followups = (
                await s.execute(
                    select(FollowupRow.id, FollowupRow.linked_refs).where(
                        FollowupRow.state.in_(_OPEN)
                    )
                )
            ).all()
            for cid, fids in _match(followups, comm_ids).items():
                commitments[cid].extend(CommitmentRef("followup", str(f)) for f in fids)
            attention = (
                await s.execute(
                    select(AttentionItemRow.id, AttentionItemRow.source_refs).where(
                        AttentionItemRow.state.in_(_ACTIVE_ATTENTION)
                    )
                )
            ).all()
            for cid, aids in _match(attention, comm_ids).items():
                commitments[cid].extend(CommitmentRef("attention", str(a)) for a in aids)
            # Task 7.4: actionable writes the gate is holding for this message.
            cid_by_ref = {ref: cid for ref, (cid, _pid) in comm_by_ref.items()}
            for ref, kind, target_id in (
                await s.execute(
                    select(
                        MailSecurityHeldPromotionRow.content_ref,
                        MailSecurityHeldPromotionRow.target_kind,
                        MailSecurityHeldPromotionRow.target_id,
                    ).where(
                        MailSecurityHeldPromotionRow.account_scope == account_scope,
                        MailSecurityHeldPromotionRow.content_ref.in_(sorted(cid_by_ref)),
                        MailSecurityHeldPromotionRow.status.in_(("HELD", "REVIEW_CANDIDATE")),
                    )
                )
            ).all():
                commitments[cid_by_ref[ref]].append(CommitmentRef(f"held_{kind}", str(target_id)))

    out: list[QueueCandidate] = []
    for a in rows:
        policy = (a.assessment or {}).get("policy") or {}
        reasons = tuple(policy.get("reason_codes") or [])
        f = tuple(findings.get(a.assessment_id, []))
        ref = ref_by_source.get(a.source_id)
        comm = comm_by_ref.get(ref) if ref else None
        cid = comm[0] if comm else None
        created = a.created_at
        if created.tzinfo is None:  # SQLite returns naive UTC
            created = created.replace(tzinfo=UTC)
        out.append(
            QueueCandidate(
                account_scope=account_scope,
                assessment_id=a.assessment_id,
                disposition=a.disposition,
                created_at=created,
                communication_id=cid,
                reason_codes=reasons,
                findings=f,
                verification_advice=tuple(
                    verification_advice(((c, st) for c, _s, st in f), reasons)
                ),
                important_sender=bool(comm and comm[1] in vip),
                commitments=tuple(commitments.get(cid, [])) if cid else (),
                reviewed=a.assessment_id in reviewed,
                thread_continuity=thread_flags.get(a.source_id),
                campaign_flagged=campaign_flags.get(a.source_id, 0),
                unexpected_ask=a.source_id in unexpected,
            )
        )
    return out


__all__ = ["MAX_CANDIDATES", "load_queue_candidates"]
