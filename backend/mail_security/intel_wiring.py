"""Threat-intel lookup for the assessment pipeline (Phase 2 wiring, Item 2).

Runs AFTER the offline inspections. It canonicalizes the domains / URLs / file
hashes the parser already extracted (reusing :mod:`._domains` and the intel
module's canonicalization), looks them up in the live intel store and converts
each live match with :func:`~backend.mail_security.intel.match_to_finding`.

Invariants:

- Matches are EVIDENCE for the policy, never an action (R-006.2, I-09). This
  module returns findings only; it has no way to act.
- Intel unavailable, erroring or empty means NO findings and nothing else
  changes (UNKNOWN, never safe: R-004.5). An error is logged as a safe code
  only (never the indicator).
- Only LIVE matches (enabled, not rolled back/superseded, before expiry on the
  worker's injected clock) are returned by the store.
- The indicator value never leaves process memory: findings carry the
  deterministic ``finding_key`` digest and safe ``intel:<field>:<token>`` refs.
- The store is global threat data; nothing derived from mail is written to it
  (this module is read-only).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

import structlog

from backend.mail_security._domains import domain_of
from backend.mail_security.auth_evidence import SenderAddresses
from backend.mail_security.inspections import _url_host
from backend.mail_security.intel import (
    IndicatorType,
    IntelError,
    canonicalize_indicator,
    match_to_finding,
)
from backend.mail_security.models import SecurityFinding
from backend.mail_security.parser import ParsedEvidence

log = structlog.get_logger(__name__)

#: Upper bound on distinct indicators looked up per message (bounded work).
MAX_INDICATORS_PER_MESSAGE = 256

_CODE_INTEL_UNAVAILABLE = "INTEL_UNAVAILABLE"

_Indicator = tuple[IndicatorType, str, str | None]


def extract_indicators(
    evidence: ParsedEvidence, addresses: SenderAddresses | None = None
) -> list[_Indicator]:
    """Canonical, de-duplicated, bounded ``(type, canonical, evidence_ref)``.

    Deterministic order: sender domains, link hosts/URLs (link order), then
    attachment hashes. Values that do not canonicalize are skipped.
    """
    out: list[_Indicator] = []
    seen: set[tuple[IndicatorType, str]] = set()

    def add(kind: IndicatorType, raw: str | None, ref: str | None) -> None:
        if not raw or len(out) >= MAX_INDICATORS_PER_MESSAGE:
            return
        try:
            canonical = canonicalize_indicator(kind, raw)
        except IntelError:
            return
        key = (kind, canonical)
        if key in seen:
            return
        seen.add(key)
        out.append((kind, canonical, ref))

    if addresses is not None:
        for header in (
            addresses.from_address,
            addresses.reply_to,
            addresses.return_path,
        ):
            add(IndicatorType.DOMAIN, domain_of(header), None)
    for link in evidence.links:
        add(IndicatorType.DOMAIN, _url_host(link.target), link.evidence_ref)
        add(IndicatorType.URL_SHA256, link.target, link.evidence_ref)
    for attachment in evidence.attachments:
        add(IndicatorType.FILE_SHA256, attachment.sha256, attachment.evidence_ref)
    return out


async def collect_intel_findings(
    evidence: ParsedEvidence,
    addresses: SenderAddresses | None,
    *,
    store: Any | None,
    now: datetime,
) -> list[SecurityFinding]:
    """Live intel matches for the message as ``THREAT_INTEL_MATCH`` findings.

    ``store`` is anything with ``async lookup(indicator_type, value, *, now)``
    (the Task 2.2 repository). ``None`` or any store error yields ``[]``.
    """
    if store is None:
        return []
    findings: dict[str, SecurityFinding] = {}
    try:
        for kind, canonical, ref in extract_indicators(evidence, addresses):
            for match in await store.lookup(kind, canonical, now=now):
                finding = match_to_finding(match, evidence_ref=ref)
                findings.setdefault(finding.finding_key, finding)
    except Exception as exc:  # noqa: BLE001 - intel outage must never alter a verdict
        log.warning(
            "mail_security.intel_unavailable",
            code=_CODE_INTEL_UNAVAILABLE,
            error_type=type(exc).__name__,
        )
        return []
    return _ordered(findings.values())


def _ordered(findings: Iterable[SecurityFinding]) -> list[SecurityFinding]:
    return sorted(findings, key=lambda f: f.finding_key)


__all__ = ["MAX_INDICATORS_PER_MESSAGE", "collect_intel_findings", "extract_indicators"]
