"""Propose bill candidates from incoming mail (owner: "yes", NS-ADR-095).

Called by the mail-security worker AFTER its verdict commits, so the
security disposition is known. A candidate is only a proposal: it becomes a
bill (and shows on Today) solely when the owner accepts it.

Order of checks, each fail-closed:

1. **Opt-in** per account: ``NERVE_BILL_CANDIDATE_SCOPES`` (``*`` or a
   ``conn:<uuid>`` list; unset = off).
2. **Security**: only ``LOW_PRIORITY`` mail. REVIEW, RESTRICTED, INCOMPLETE or
   anything unknown never produces a candidate: a phishing "invoice" must not
   look like a bill.
3. **Bill signal** (:mod:`backend.bills.signal`): an owed-money phrase
   anchored to an amount, no paid/receipt or promotional wording. On real mail
   this cut 127 mostly-marketing candidates to 9 from real billers.
4. **Idempotent** per source (``content_ref``); duplicates of an existing
   proposal (same payee, amount, due date window) are linked, not repeated.

The anchored amount replaces the extractor's "first price in the email".
"""
from __future__ import annotations

import email.utils
from datetime import UTC, datetime
from typing import Any

import structlog

from backend.bills.extractor import BillSourceData, extract_bill_candidate
from backend.bills.signal import bill_signal

log = structlog.get_logger(__name__)

ENV_BILL_CANDIDATE_SCOPES = "NERVE_BILL_CANDIDATE_SCOPES"
#: The only security disposition allowed to produce a candidate.
ALLOWED_DISPOSITIONS = frozenset({"LOW_PRIORITY"})
SOURCE_KIND = "email"


def _header(evidence: Any, name: str) -> str | None:
    for n, v in getattr(evidence, "headers", []) or []:
        if n.strip().lower() == name:
            return v
    return None


class MailBillProposer:
    """Turns one assessed incoming email into at most one bill candidate."""

    def __init__(self, repository: Any, *, clock: Any = None) -> None:
        self._repo = repository
        self._clock = clock or (lambda: datetime.now(UTC))

    async def propose(
        self,
        account_scope: str,
        *,
        content_ref: str | None,
        evidence: Any,
        disposition: str,
    ) -> str:
        """Returns a status code (never raises for expected cases)."""
        from backend.mail_security.campaigns import text_of
        from backend.mail_security.observer_wiring import scope_listed

        if not scope_listed(account_scope, ENV_BILL_CANDIDATE_SCOPES):
            return "NOT_OPTED_IN"
        if str(disposition) not in ALLOWED_DISPOSITIONS:
            return "SECURITY_NOT_LOW_PRIORITY"
        if not content_ref:
            return "NO_SOURCE_REF"
        subject = _header(evidence, "subject")
        text = text_of(evidence)
        signal = bill_signal(subject, text)
        if signal is None:
            return "NOT_A_BILL"
        if await self._repo.candidate_exists_for_source(SOURCE_KIND, content_ref):
            return "ALREADY_PROPOSED"
        name = email.utils.parseaddr(_header(evidence, "from") or "")[0] or None
        candidate = extract_bill_candidate(
            BillSourceData(source_id=content_ref, source_kind=SOURCE_KIND, text=text,
                           subject=subject, sender_display_name=name),
            now=self._clock(),
        )
        if candidate is None:
            return "NOT_EXTRACTABLE"
        fields = dict(candidate.extracted_fields)
        fields["amount_cents"] = {"value": signal.amount_cents, "confidence": 0.97,
                                  "anchor": signal.phrase}
        evidence_trail = [*candidate.evidence,
                          {"field": "amount_cents", "source_kind": SOURCE_KIND,
                           "source_id": content_ref, "snippet": signal.phrase,
                           "confidence": 0.97}]
        # Look for an earlier proposal BEFORE inserting, so the lookup can never
        # return the new row itself.
        duplicate_of = await self._find_duplicate(fields)
        row = await self._repo.create_candidate(
            source_kind=SOURCE_KIND,
            source_id=content_ref,
            extracted_fields=fields,
            evidence=evidence_trail,
            needs_review=list(candidate.needs_review),
        )
        if duplicate_of is not None:
            await self._repo.set_candidate_duplicate_of(row.id, duplicate_of)
        log.info("bills.mail_candidate_proposed", needs_review=len(candidate.needs_review))
        return "PROPOSED"

    async def _find_duplicate(self, fields: dict[str, Any]) -> str | None:
        payee = (fields.get("payee") or {}).get("value")
        due = (fields.get("due_date") or {}).get("value")
        if not payee or not due:
            return None
        try:
            dup = await self._repo.find_duplicate_candidate(
                payee=str(payee), amount_cents=int(fields["amount_cents"]["value"]),
                due_at=datetime.fromisoformat(str(due)),
            )
        except Exception as exc:  # noqa: BLE001 - linking is best-effort
            log.warning("bills.mail_candidate_dedup_failed", error_type=type(exc).__name__)
            return None
        return dup.id if dup is not None else None

_proposer: MailBillProposer | None = None


def set_bill_proposer(proposer: MailBillProposer | None) -> None:
    global _proposer
    _proposer = proposer


def get_bill_proposer() -> MailBillProposer | None:
    return _proposer


__all__ = [
    "ALLOWED_DISPOSITIONS",
    "ENV_BILL_CANDIDATE_SCOPES",
    "MailBillProposer",
    "get_bill_proposer",
    "set_bill_proposer",
]
