"""Attachment inspection + optional local scanner for the pipeline (Item 3).

Proposed NS-ADR-014 ("scan-not-run surfacing"; the owner may reverse it):

(a) Static attachment inspection (:func:`inspect_attachment`) ALWAYS runs on the
    parsed attachment descriptors; its findings flow to the policy like any
    other finding.
(b) No scanner configured: a message with attachments records the explicit
    ``ATTACHMENT_SCAN_NOT_RUN`` check in ``checks_not_run``. That alone does
    NOT make the assessment INCOMPLETE (review load stays sane; SAFE_TO_PROCESS
    is unreachable anyway, NS-ADR-013).
(c) Scanner configured but it errors / times out / is unavailable / too large /
    has stale signatures: ``ATTACHMENT_SCAN_INCOMPLETE``; the assessment is
    INCOMPLETE (or REVIEW with threat findings), never clean (I-02).
(d) A scanner "clean" is evidence only: engine + signature version recorded on
    the finding, never a safety claim.
(e) Scanner configuration is environment-only: a ClamAV unix socket path OR a
    loopback host (+port), validated by :class:`ClamdConfig` (non-loopback is
    rejected at construction). Absent config => no scanner. Invalid config =>
    a safe log code and no scanner; never a crash and never a remote fallback.

Bytes: the parser retains an attachment's decoded bytes (transiently, already
bounded by its per-part cap) ONLY when a scanner is configured. They are cleared
here as soon as inspection/scan finishes; nothing raw is persisted or logged.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

import structlog

from backend.mail_security.attachments import (
    DEFAULT_LIMITS,
    RULE_SCAN_INCOMPLETE,
    AttachmentLimits,
    AttachmentScanner,
    inspect_attachment,
    run_scan,
)
from backend.mail_security.clamav import ClamdConfig, ClamdConfigError, ClamdScanner
from backend.mail_security.models import Code, SecurityFinding
from backend.mail_security.parser import ParsedEvidence
from backend.mail_security.static_inspection import (
    CODE_DEEP_INCOMPLETE,
    CODE_DEEP_NOT_RUN,
    StaticInspector,
    deep_inspect,
    wants_deep_inspection,
)

log = structlog.get_logger(__name__)

CODE_SCAN_NOT_RUN: Code = "ATTACHMENT_SCAN_NOT_RUN"
CODE_SCAN_INCOMPLETE: Code = "ATTACHMENT_SCAN_INCOMPLETE"
CODE_STATIC_INCOMPLETE: Code = "ATTACHMENT_STATIC_INCOMPLETE"

ENV_CLAMD_SOCKET = "NERVE_CLAMD_SOCKET"
ENV_CLAMD_HOST = "NERVE_CLAMD_HOST"
ENV_CLAMD_PORT = "NERVE_CLAMD_PORT"

#: Whole-scan wall-clock budget per attachment (seconds).
DEFAULT_SCAN_TIMEOUT_SECONDS = 30.0

_CODE_CONFIG_INVALID = "ATTACHMENT_SCANNER_CONFIG_INVALID"
_CODE_INSPECTION_ERROR = "ATTACHMENT_STATIC_INSPECTION_ERROR"


def scanner_from_env(
    env: Mapping[str, str] | None = None,
    *,
    clock: object | None = None,
) -> AttachmentScanner | None:
    """Build the optional local clamd scanner from the environment.

    Returns ``None`` when nothing is configured (the default; no socket is ever
    opened) or when the configuration is invalid (logged as a safe code only,
    never the rejected value). Construction itself opens no connection.
    """
    source = os.environ if env is None else env
    socket_path = (source.get(ENV_CLAMD_SOCKET) or "").strip()
    host = (source.get(ENV_CLAMD_HOST) or "").strip()
    port_raw = (source.get(ENV_CLAMD_PORT) or "").strip()
    if not socket_path and not host:
        return None
    try:
        if socket_path and host:
            raise ClamdConfigError("ENDPOINT_AMBIGUOUS")
        if socket_path:
            config = ClamdConfig(socket_path=socket_path)
        else:
            try:
                port = int(port_raw) if port_raw else 3310
            except ValueError:
                raise ClamdConfigError("PORT_INVALID") from None
            config = ClamdConfig(host=host, port=port)
        if clock is not None:
            return ClamdScanner(config, clock=clock)  # type: ignore[arg-type]
        return ClamdScanner(config)
    except ClamdConfigError as exc:
        log.warning(
            "mail_security.attachment_scanner_config_invalid",
            code=_CODE_CONFIG_INVALID,
            reason=exc.code,
        )
        return None
    except Exception as exc:  # noqa: BLE001 - never crash startup
        log.warning(
            "mail_security.attachment_scanner_config_invalid",
            code=_CODE_CONFIG_INVALID,
            reason=type(exc).__name__,
        )
        return None


@dataclass(frozen=True)
class AttachmentAssessment:
    """What the attachment stage contributes to an assessment.

    ``checks_not_run`` and ``incomplete_codes`` hold safe upper-snake codes.
    Only ``incomplete_codes`` make the policy treat inspection as incomplete.
    """

    findings: list[SecurityFinding] = field(default_factory=list)
    checks_not_run: list[Code] = field(default_factory=list)
    incomplete_codes: list[Code] = field(default_factory=list)


async def collect_attachment_results(
    evidence: ParsedEvidence,
    *,
    scanner: AttachmentScanner | None,
    limits: AttachmentLimits = DEFAULT_LIMITS,
    timeout_seconds: float = DEFAULT_SCAN_TIMEOUT_SECONDS,
    deep_inspector: StaticInspector | None = None,
) -> AttachmentAssessment:
    """Static inspection (always), the optional scan, and the optional deep
    structural inspection (NS-ADR-086, PDF/OOXML/ZIP only) per attachment.

    Never raises. Retained payloads are cleared before returning.
    """
    attachments = evidence.attachments
    if not attachments:
        return AttachmentAssessment()
    findings: dict[str, SecurityFinding] = {}
    incomplete: list[Code] = []
    deep_wanted = False
    try:
        for descriptor in attachments:
            payload = descriptor.payload
            try:
                result = inspect_attachment(descriptor, payload, limits=limits)
            except Exception as exc:  # noqa: BLE001 - static failure is incomplete
                log.warning(
                    "mail_security.attachment_inspection_failed",
                    code=_CODE_INSPECTION_ERROR,
                    error_type=type(exc).__name__,
                )
                incomplete.append(CODE_STATIC_INCOMPLETE)
                continue
            for finding in result.findings:
                findings.setdefault(finding.finding_key, finding)
            if result.incomplete:
                incomplete.append(CODE_STATIC_INCOMPLETE)
            if wants_deep_inspection(payload):
                deep_wanted = True
                if deep_inspector is not None:
                    deep = await deep_inspect(deep_inspector, descriptor.evidence_ref, payload)
                    for finding in deep.findings:
                        findings.setdefault(finding.finding_key, finding)
                    if deep.incomplete:
                        incomplete.append(CODE_DEEP_INCOMPLETE)
            if scanner is None or payload == b"":
                continue  # nothing configured / nothing to scan
            record = await run_scan(
                scanner,
                payload,
                evidence_ref=descriptor.evidence_ref,
                max_bytes=limits.max_bytes,
                timeout_seconds=timeout_seconds,
            )
            findings.setdefault(record.finding.finding_key, record.finding)
            if record.incomplete:
                incomplete.append(CODE_SCAN_INCOMPLETE)
    finally:
        for descriptor in attachments:
            descriptor.payload = None
    ordered = sorted(findings.values(), key=lambda f: f.finding_key)
    return AttachmentAssessment(
        findings=ordered,
        checks_not_run=[
            *([CODE_SCAN_NOT_RUN] if scanner is None else []),
            *([CODE_DEEP_NOT_RUN] if deep_wanted and deep_inspector is None else []),
        ],
        incomplete_codes=list(dict.fromkeys(incomplete)),
    )


__all__ = [
    "CODE_SCAN_INCOMPLETE",
    "CODE_SCAN_NOT_RUN",
    "CODE_STATIC_INCOMPLETE",
    "ENV_CLAMD_HOST",
    "ENV_CLAMD_PORT",
    "ENV_CLAMD_SOCKET",
    "RULE_SCAN_INCOMPLETE",
    "AttachmentAssessment",
    "collect_attachment_results",
    "scanner_from_env",
]
