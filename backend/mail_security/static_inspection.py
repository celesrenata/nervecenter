"""Client for the static deep-inspection sidecar (NS-ADR-086).

The sidecar (``inspector/nerve_inspector``, its own image) parses PDF, OOXML
and ZIP structure without executing anything and answers over a UNIX socket
in the pod. This module:

* talks to it over that socket ONLY (a path; no host, port or URL is
  accepted). It is the second module exempt from the network-free rule, next
  to ``clamav.py``, and for the same reason: local IPC;
* validates the report strictly as untrusted input (closed codes, reasons and
  kinds; bounded lists; host regex). An invalid report is ``INCOMPLETE``,
  never clean;
* maps codes to ordinary :class:`SecurityFinding` objects (category
  ``ATTACHMENT``, so structure alone never reaches RESTRICTED) and runs the
  existing domain-deception checks on link hosts found inside documents.

A report can only ADD findings or mark the attachment incomplete.
"""
from __future__ import annotations

import asyncio
import os
import re
import struct
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from backend.mail_security.models import (
    Code,
    FindingCategory,
    FindingSeverity,
    FindingStatus,
    SecurityFinding,
)

log = structlog.get_logger(__name__)

DETECTOR_VERSION = "attachment-deep@1"
ENV_STATIC_INSPECTION_SOCKET = "NERVE_MAIL_SECURITY_STATIC_INSPECTION_SOCKET"
CODE_DEEP_NOT_RUN: Code = "ATTACHMENT_DEEP_INSPECTION_NOT_RUN"
CODE_DEEP_INCOMPLETE: Code = "ATTACHMENT_DEEP_INSPECTION_INCOMPLETE"
RULE_DEEP_INCOMPLETE = "ATTACHMENT_DEEP_INSPECTION_INCOMPLETE"
MAGIC = b"NSI1"
MAX_SAMPLE_BYTES = 16 * 1024 * 1024
MAX_REPLY_BYTES = 256 * 1024
DEFAULT_TIMEOUT_SECONDS = 20.0

_SEVERITY: dict[str, FindingSeverity] = {
    **dict.fromkeys(
        (
            "PDF_JAVASCRIPT", "PDF_LAUNCH_ACTION", "OOXML_EXTERNAL_TEMPLATE",
            "OOXML_EXTERNAL_OBJECT", "OOXML_DDE_FIELD", "ZIP_EXECUTABLE_ENTRY",
            "ZIP_DOUBLE_EXTENSION", "ZIP_PATH_TRAVERSAL",
        ),
        FindingSeverity.HIGH,
    ),
    **dict.fromkeys(
        (
            "PDF_EMBEDDED_FILE", "PDF_OPENACTION_URI", "PDF_REMOTE_ACTION", "PDF_RICH_MEDIA",
            "OOXML_MACRO_PROJECT", "OOXML_EMBEDDED_OLE", "OOXML_ACTIVEX",
            "ZIP_ENCRYPTED_ENTRY", "ZIP_HTML_ENTRY",
        ),
        FindingSeverity.MEDIUM,
    ),
    **dict.fromkeys(
        ("PDF_XFA_FORM", "PDF_LARGE_LINK_AREA", "ZIP_NESTED_ARCHIVE"), FindingSeverity.LOW
    ),
}  # fmt: skip
_REASONS = (
    "TOO_LARGE", "PARSE_FAILED", "PASSWORD_PROTECTED", "DEPTH_EXCEEDED", "BUDGET_EXCEEDED",
    "OBJECT_CAP_EXCEEDED", "ENTRY_CAP_EXCEEDED", "TIMEOUT", "RESOURCE_LIMIT", "INTERNAL_ERROR",
)  # fmt: skip
_DeepCode = Literal[tuple(_SEVERITY)]  # type: ignore[valid-type]
_Reason = Literal[_REASONS]  # type: ignore[valid-type]
_Host = Annotated[str, StringConstraints(max_length=253, pattern=r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")]
_CountKey = Annotated[str, StringConstraints(max_length=40, pattern=r"^[a-z_]{1,40}$")]
_SOCKET_RE = re.compile(r"^/[A-Za-z0-9._/-]{1,200}\.sock$")


class DeepReport(BaseModel):
    """The only shape accepted from the sidecar (untrusted)."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    v: Literal[1]
    kind: Literal["pdf", "ooxml", "zip", "not_applicable"]
    complete: bool
    incomplete_reasons: list[_Reason] = Field(max_length=16)
    codes: list[_DeepCode] = Field(max_length=32)
    counts: dict[_CountKey, Annotated[int, Field(ge=0, le=10_000_000)]] = Field(max_length=16)
    link_hosts: list[_Host] = Field(max_length=64)
    sandbox: dict[Literal["netns", "nofile"], bool] | None = None


class StaticInspectorUnavailable(Exception):
    """The sidecar could not be reached or answered badly (code only)."""


class StaticInspector:
    """Socket client. Construction opens nothing; one connection per sample."""

    def __init__(self, socket_path: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
        if not _SOCKET_RE.fullmatch(socket_path) or ".." in socket_path.split("/"):
            raise ValueError("SOCKET_PATH_INVALID")
        self.socket_path = socket_path
        self._timeout = timeout_seconds

    async def inspect(self, sample: bytes) -> DeepReport:
        if len(sample) > MAX_SAMPLE_BYTES:
            raise StaticInspectorUnavailable("SAMPLE_TOO_LARGE")
        try:
            async with asyncio.timeout(self._timeout):
                reader, writer = await asyncio.open_unix_connection(self.socket_path)
                try:
                    writer.write(MAGIC + struct.pack(">I", len(sample)) + sample)
                    await writer.drain()
                    (length,) = struct.unpack(">I", await reader.readexactly(4))
                    if length > MAX_REPLY_BYTES:
                        raise StaticInspectorUnavailable("REPLY_TOO_LARGE")
                    body = await reader.readexactly(length)
                finally:
                    writer.close()
        except StaticInspectorUnavailable:
            raise
        except (OSError, TimeoutError, asyncio.IncompleteReadError, struct.error):
            raise StaticInspectorUnavailable("UNREACHABLE") from None
        try:
            return DeepReport.model_validate_json(body)
        except (ValidationError, ValueError):
            raise StaticInspectorUnavailable("REPORT_INVALID") from None


def inspector_from_env(env: Mapping[str, str] | None = None) -> StaticInspector | None:
    """The kill switch: unset (default) means no deep inspection."""
    raw = ((env if env is not None else os.environ).get(ENV_STATIC_INSPECTION_SOCKET) or "")
    raw = raw.strip()
    if not raw:
        return None
    try:
        return StaticInspector(raw)
    except ValueError:
        log.warning("mail_security.static_inspector_config_invalid")
        return None


#: Families worth sending (magic-sniffed; the claimed type is not trusted).
def wants_deep_inspection(payload: bytes | None) -> bool:
    if not payload:
        return False
    head = payload[:1024]
    return head.startswith((b"%PDF-", b"PK\x03\x04", b"PK\x05\x06")) or b"%PDF-" in head


def _finding(ref: str, code: str, severity: FindingSeverity, status: FindingStatus,
             category: FindingCategory = FindingCategory.ATTACHMENT,
             rule: str | None = None) -> SecurityFinding:
    rule_id = rule or f"ATTACHMENT_DEEP_{code}"
    return SecurityFinding(
        finding_key=f"{ref}:{rule_id}",
        rule_id=rule_id,
        category=category,
        severity=severity,
        status=status,
        detector_version=DETECTOR_VERSION,
        evidence_refs=[ref],
        explanation_code=code,
    )


@dataclass(frozen=True)
class DeepResult:
    findings: list[SecurityFinding] = field(default_factory=list)
    incomplete: bool = False


def findings_from_report(ref: str, report: DeepReport) -> DeepResult:
    """Map a validated report to findings. Only adds; never clears."""
    from backend.mail_security.inspections import _domain_deception_findings

    out = [
        _finding(ref, code, _SEVERITY[code],
                 FindingStatus.OBSERVED if code.startswith("ZIP_") else FindingStatus.SUSPECTED)
        for code in sorted(set(report.codes))
    ]
    for i, host in enumerate(report.link_hosts):
        out.extend(
            _domain_deception_findings(
                host, finding_key_prefix=f"{ref}:doclink:{i}", evidence_ref=f"{ref}:doclink:{i}"
            )
        )
    incomplete = not report.complete
    for reason in sorted(set(report.incomplete_reasons)) or (["INTERNAL_ERROR"] if incomplete
                                                              else []):
        out.append(_finding(ref, f"DEEP_INSPECTION_{reason}", FindingSeverity.UNKNOWN,
                            FindingStatus.INCOMPLETE, FindingCategory.SCANNER_FAILURE,
                            rule=RULE_DEEP_INCOMPLETE))
    return DeepResult(findings=out, incomplete=incomplete)


async def deep_inspect(inspector: StaticInspector, ref: str, payload: bytes) -> DeepResult:
    """Inspect one attachment; an unreachable or invalid sidecar is incomplete."""
    try:
        report = await inspector.inspect(payload)
    except StaticInspectorUnavailable as exc:
        log.warning("mail_security.static_inspection_unavailable", code=str(exc))
        return DeepResult(
            findings=[_finding(ref, "DEEP_INSPECTION_UNAVAILABLE", FindingSeverity.UNKNOWN,
                               FindingStatus.INCOMPLETE, FindingCategory.SCANNER_FAILURE,
                               rule=RULE_DEEP_INCOMPLETE)],
            incomplete=True,
        )
    return findings_from_report(ref, report)


__all__ = [
    "CODE_DEEP_INCOMPLETE",
    "CODE_DEEP_NOT_RUN",
    "DETECTOR_VERSION",
    "ENV_STATIC_INSPECTION_SOCKET",
    "DeepReport",
    "DeepResult",
    "StaticInspector",
    "StaticInspectorUnavailable",
    "deep_inspect",
    "findings_from_report",
    "inspector_from_env",
    "wants_deep_inspection",
]
