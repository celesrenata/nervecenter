"""Static attachment inspection and scanner service interface (Task 2.4).

Two independent, explicit-call building blocks. **Nothing here is wired into
the worker, policy or app startup** (see the Task 2.4 report for the later
integration); existing assessments are unchanged.

1. :func:`inspect_attachment` — a pure *static* inspection. It consumes the
   Task 1.3 parser's :class:`AttachmentDescriptor` and, only where the caller
   supplies a bounded payload (the parser does not retain bytes), reads magic
   bytes, the ZIP/OOXML *central directory* (entry names and declared sizes
   only) and a bounded PDF prefix. It never executes, extracts, decompresses
   or recurses into anything (T-07, T-08) and performs no I/O.

2. :class:`AttachmentScanner` + :func:`run_scan` — a reviewed *interface* for an
   optional, isolated, unprivileged malware engine (R-005.4). No engine is
   shipped or assumed (decisions.md open question 10). Only
   :class:`NullAttachmentScanner` (returns ``NOT_RUN``) exists here; a real
   clamd / YARA-service client is a later deployment decision that needs
   egress/isolation review.

**Privacy contract (R-017.3, R-022).** Scanner endpoints are local-only by
contract: private attachment bytes may never go to a third-party scanning
service. The Protocol accepts bytes and a deadline only — never a URL or
address. Attachment bytes and filenames are attacker-controlled: findings carry
only stable rule ids, safe upper-snake codes and opaque evidence refs; no
filename text, bytes, or exception text is ever stored, logged or raised.

**Incomplete is never clean (I-02, I-08).** Only a scanner that actually
returned a clean verdict with an explicit engine name *and* version yields
``clean_by_engine``; ERROR, TIMEOUT, UNAVAILABLE, TOO_LARGE and NOT_RUN all
yield an ``ATTACHMENT_SCAN_INCOMPLETE`` finding. Even an engine "clean" is
evidence, not safety: it is an INFO finding, never a SAFE marker.
"""

from __future__ import annotations

import asyncio
import re
import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from backend.mail_security.models import (
    Code,
    FindingCategory,
    FindingSeverity,
    FindingStatus,
    SecurityFinding,
)
from backend.mail_security.parser import AttachmentDescriptor

ATTACHMENTS_VERSION = "attachment-static@1"

# -- rule ids (stable) ---------------------------------------------------------
RULE_DOUBLE_EXTENSION = "ATTACHMENT_DOUBLE_EXTENSION"
RULE_TYPE_MISMATCH = "ATTACHMENT_TYPE_MISMATCH"
RULE_EXECUTABLE_DISGUISED = "ATTACHMENT_EXECUTABLE_DISGUISED"
RULE_DANGEROUS_TYPE = "ATTACHMENT_DANGEROUS_TYPE"
RULE_FILENAME_SPOOF = "ATTACHMENT_FILENAME_SPOOF"
RULE_MACRO_CAPABLE = "ATTACHMENT_OFFICE_MACRO_CAPABLE"
RULE_ARCHIVE_BOMB = "ATTACHMENT_ARCHIVE_BOMB_INDICATOR"
RULE_ARCHIVE_TRAVERSAL = "ATTACHMENT_ARCHIVE_PATH_TRAVERSAL"
RULE_ARCHIVE_EXECUTABLE = "ATTACHMENT_ARCHIVE_EXECUTABLE_MEMBER"
RULE_ARCHIVE_NESTED = "ATTACHMENT_ARCHIVE_NESTED"
RULE_ARCHIVE_NOT_INSPECTED = "ATTACHMENT_ARCHIVE_NOT_INSPECTED"
RULE_ENCRYPTED = "ATTACHMENT_ENCRYPTED"
RULE_PDF_ACTIVE = "ATTACHMENT_PDF_ACTIVE_CONTENT"
RULE_MALFORMED = "ATTACHMENT_MALFORMED_CONTAINER"
RULE_STATIC_INCOMPLETE = "ATTACHMENT_STATIC_INCOMPLETE"
RULE_SCAN_INCOMPLETE = "ATTACHMENT_SCAN_INCOMPLETE"
RULE_SCANNER_DETECTION = "ATTACHMENT_SCANNER_DETECTION"
RULE_ENGINE_CLEAN = "ATTACHMENT_ENGINE_CLEAN_VERDICT"


@dataclass(frozen=True)
class AttachmentLimits:
    """Hard caps for static inspection (all bounded, T-07)."""

    max_bytes: int = 16 * 1024 * 1024
    max_zip_entries: int = 1000
    max_zip_tail_bytes: int = 65_535 + 22 + 20  # EOCD + max comment + zip64 locator
    max_declared_uncompressed: int = 1024 * 1024 * 1024
    bomb_ratio: int = 100
    bomb_min_entry_bytes: int = 1024 * 1024
    pdf_prefix_bytes: int = 256 * 1024
    ole_scan_bytes: int = 512 * 1024
    max_filename_chars: int = 64


DEFAULT_LIMITS = AttachmentLimits()

# -- type tables ---------------------------------------------------------------
_DANGEROUS_EXTS = frozenset(
    {
        "exe", "scr", "com", "bat", "cmd", "js", "jse", "vbs", "vbe", "wsf",
        "wsh", "hta", "jar", "ps1", "msi", "lnk", "pif", "cpl", "dll", "reg",
        "sh", "app", "iso", "img", "apk", "msc", "gadget", "inf", "scf",
    }
)  # fmt: skip
_DOC_EXTS = frozenset(
    {
        "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "rtf",
        "jpg", "jpeg", "png", "gif", "csv", "zip", "odt", "ods", "html", "htm",
    }
)  # fmt: skip
_MACRO_EXTS = frozenset({"docm", "xlsm", "pptm", "dotm", "xltm", "xlam", "ppam", "potm", "sldm"})
_ZIP_EXTS = frozenset(
    {"zip", "docx", "xlsx", "pptx", "odt", "ods", "odp", "dotx", "xltx"} | _MACRO_EXTS
)
_OLE_EXTS = frozenset({"doc", "xls", "ppt", "msg", "dot", "xlt"})
_EXT_FAMILY: dict[str, str] = {
    **dict.fromkeys(_ZIP_EXTS, "zip"),
    **dict.fromkeys(_OLE_EXTS, "ole"),
    "pdf": "pdf",
    "png": "png",
    "jpg": "jpeg",
    "jpeg": "jpeg",
    "gif": "gif",
    "rtf": "rtf",
    "gz": "gzip",
    "rar": "rar",
    "7z": "7z",
}
_EXECUTABLE_FAMILIES = frozenset({"exe", "elf", "macho", "script"})
_MIME_FAMILY = {
    "application/x-dosexec": "exe",
    "application/x-elf": "elf",
    "application/zip": "zip",
    "application/pdf": "pdf",
    "application/gzip": "gzip",
    "application/x-rar-compressed": "rar",
    "application/x-7z-compressed": "7z",
    "image/jpeg": "jpeg",
    "image/png": "png",
    "image/gif": "gif",
}
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"MZ", "exe"),
    (b"\x7fELF", "elf"),
    (b"\xcf\xfa\xed\xfe", "macho"),
    (b"\xce\xfa\xed\xfe", "macho"),
    (b"\xfe\xed\xfa\xce", "macho"),
    (b"\xfe\xed\xfa\xcf", "macho"),
    (b"#!", "script"),
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole"),
    (b"%PDF-", "pdf"),
    (b"\x1f\x8b", "gzip"),
    (b"Rar!", "rar"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"{\\rtf", "rtf"),
)
_MACRO_MEMBER_RE = re.compile(r"(^|/)vbaproject\.bin$|(^|/)macrosheets/")
_PDF_ACTIVE_RE = re.compile(
    rb"/(JavaScript|JS|OpenAction|Launch|EmbeddedFile|RichMedia|XFA|AA)(?![A-Za-z0-9])"
)
_PDF_ENCRYPT_RE = re.compile(rb"/Encrypt(?![A-Za-z0-9])")
_BIDI_CHARS = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f")
_ZERO_WIDTH = frozenset("\u200b\u200c\u200d\u2060\ufeff\u00ad")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def safe_filename_form(raw: str, *, max_chars: int = 64) -> str:
    """Bounded, sanitized rendering of an attacker-controlled filename.

    For *transient display only* (never stored in findings): every character
    outside ``[A-Za-z0-9._-]`` (RTL overrides, control, whitespace, path
    separators, non-ASCII) becomes ``_`` and the result is capped.
    """
    return _SAFE_FILENAME_RE.sub("_", raw[: max(0, max_chars)])


# =============================================================================
# static inspection
# =============================================================================


@dataclass(frozen=True)
class AttachmentInspection:
    """Result of :func:`inspect_attachment`. Carries no filename or bytes."""

    evidence_ref: str
    findings: list[SecurityFinding] = field(default_factory=list)
    payload_inspected: bool = False
    incomplete: bool = False


def _mk(
    ref: str,
    rule_id: str,
    category: FindingCategory,
    severity: FindingSeverity,
    status: FindingStatus,
    code: Code,
    *,
    detector_version: str = ATTACHMENTS_VERSION,
    extra_refs: Sequence[str] = (),
) -> SecurityFinding:
    refs = [ref, *extra_refs]
    return SecurityFinding(
        finding_key=f"{ref}:{rule_id}",
        rule_id=rule_id,
        category=category,
        severity=severity,
        status=status,
        detector_version=detector_version,
        evidence_refs=list(dict.fromkeys(refs))[:24],
        explanation_code=code,
    )


def _ext_chain(raw: str) -> list[str]:
    base = raw.strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    base = "".join(c for c in base if c not in _BIDI_CHARS and c not in _ZERO_WIDTH)
    parts = [p.strip().lower() for p in base.split(".")]
    return parts[1:] if len(parts) > 1 else []


def _sniff_family(data: bytes) -> str | None:
    head = data[:16]
    for prefix, fam in _MAGIC:
        if head.startswith(prefix):
            return fam
    return None


def inspect_attachment(
    descriptor: AttachmentDescriptor,
    payload: bytes | None = None,
    *,
    limits: AttachmentLimits = DEFAULT_LIMITS,
) -> AttachmentInspection:
    """Statically inspect one attachment. Pure; never executes or extracts.

    ``payload`` is optional bounded bytes (whole attachment, or a prefix when
    ``len(payload) < descriptor.size_bytes``). Without it only descriptor
    metadata is judged.
    """
    ref = descriptor.evidence_ref
    out: list[SecurityFinding] = []
    raw_name = descriptor.filename_raw or ""
    chain = _ext_chain(raw_name)
    ext = chain[-1] if chain else ""

    # -- filename tricks -------------------------------------------------------
    has_bidi = any(c in _BIDI_CHARS for c in raw_name)
    has_ctrl = any((ord(c) < 32 or 0x7F <= ord(c) < 0xA0 or c in _ZERO_WIDTH) for c in raw_name)
    if has_bidi or has_ctrl:
        out.append(
            _mk(
                ref, RULE_FILENAME_SPOOF, FindingCategory.ATTACHMENT,
                FindingSeverity.HIGH if has_bidi else FindingSeverity.MEDIUM,
                FindingStatus.OBSERVED,
                "FILENAME_BIDI_OVERRIDE" if has_bidi else "FILENAME_CONTROL_CHARS",
            )
        )  # fmt: skip

    # -- double extension ------------------------------------------------------
    double = descriptor.double_extension or (
        len(chain) >= 2 and chain[-1] in _DANGEROUS_EXTS and chain[-2] in _DOC_EXTS
    )
    if double:
        out.append(
            _mk(
                ref, RULE_DOUBLE_EXTENSION, FindingCategory.ATTACHMENT,
                FindingSeverity.HIGH, FindingStatus.OBSERVED, "DOUBLE_EXTENSION",
            )
        )  # fmt: skip

    # -- size cap --------------------------------------------------------------
    too_large = descriptor.size_bytes > limits.max_bytes or (
        payload is not None and len(payload) > limits.max_bytes
    )
    data = None if too_large else payload
    if too_large:
        out.append(
            _mk(
                ref, RULE_SCAN_INCOMPLETE, FindingCategory.SCANNER_FAILURE,
                FindingSeverity.UNKNOWN, FindingStatus.INCOMPLETE,
                "ATTACHMENT_TOO_LARGE",
            )
        )  # fmt: skip

    # -- sniffed family (payload first, else parser metadata) -------------------
    family = _sniff_family(data) if data else None
    if family is None and descriptor.sniffed_type:
        family = _MIME_FAMILY.get(descriptor.sniffed_type)

    if ext in _DANGEROUS_EXTS and not double:
        out.append(
            _mk(
                ref, RULE_DANGEROUS_TYPE, FindingCategory.ATTACHMENT,
                FindingSeverity.HIGH, FindingStatus.OBSERVED, "DANGEROUS_EXTENSION",
            )
        )  # fmt: skip
    if family in _EXECUTABLE_FAMILIES and ext not in _DANGEROUS_EXTS:
        out.append(
            _mk(
                ref, RULE_EXECUTABLE_DISGUISED, FindingCategory.MALWARE,
                FindingSeverity.HIGH, FindingStatus.OBSERVED,
                "EXECUTABLE_CONTENT_DISGUISED",
            )
        )  # fmt: skip
    else:
        expected = _EXT_FAMILY.get(ext)
        ext_mismatch = (
            expected is not None
            and family is not None
            and family != expected
            and family not in _EXECUTABLE_FAMILIES
        )
        if ext_mismatch or descriptor.magic_mismatch:
            out.append(
                _mk(
                    ref, RULE_TYPE_MISMATCH, FindingCategory.ATTACHMENT,
                    FindingSeverity.MEDIUM, FindingStatus.OBSERVED,
                    "CONTENT_TYPE_MISMATCH",
                )
            )  # fmt: skip

    # -- payload-dependent checks ----------------------------------------------
    inspected = False
    incomplete = too_large
    if data:
        inspected = True
        complete = len(data) >= descriptor.size_bytes
        if family == "zip":
            incomplete |= _inspect_zip(ref, data, ext, complete, limits, out)
        elif family == "pdf":
            _inspect_pdf(ref, data, limits, out)
        elif family == "ole":
            if b"V\x00B\x00A\x00" in data[: limits.ole_scan_bytes]:
                out.append(
                    _mk(
                        ref, RULE_MACRO_CAPABLE, FindingCategory.ATTACHMENT,
                        FindingSeverity.MEDIUM, FindingStatus.SUSPECTED,
                        "OLE_VBA_INDICATOR",
                    )
                )  # fmt: skip
        elif family in {"rar", "7z", "gzip"}:
            out.append(
                _mk(
                    ref, RULE_ARCHIVE_NOT_INSPECTED, FindingCategory.ATTACHMENT,
                    FindingSeverity.INFO, FindingStatus.NOT_EVALUATED,
                    "ARCHIVE_FORMAT_NOT_INSPECTED",
                )
            )  # fmt: skip
    elif descriptor.is_archive and not too_large:
        out.append(
            _mk(
                ref, RULE_ARCHIVE_NOT_INSPECTED, FindingCategory.ATTACHMENT,
                FindingSeverity.INFO, FindingStatus.NOT_EVALUATED,
                "ARCHIVE_NOT_INSPECTED",
            )
        )  # fmt: skip

    return AttachmentInspection(
        evidence_ref=ref,
        findings=out,
        payload_inspected=inspected,
        incomplete=incomplete,
    )


def _inspect_pdf(
    ref: str, data: bytes, limits: AttachmentLimits, out: list[SecurityFinding]
) -> None:
    prefix = data[: limits.pdf_prefix_bytes]
    hits = {m.group(1) for m in _PDF_ACTIVE_RE.finditer(prefix)}
    if hits:
        sev = (
            FindingSeverity.HIGH
            if hits & {b"Launch", b"JavaScript", b"JS"}
            else FindingSeverity.MEDIUM
        )
        out.append(
            _mk(
                ref, RULE_PDF_ACTIVE, FindingCategory.ATTACHMENT, sev,
                FindingStatus.SUSPECTED, "PDF_ACTIVE_CONTENT_KEYWORD",
            )
        )  # fmt: skip
    if _PDF_ENCRYPT_RE.search(prefix):
        out.append(
            _mk(
                ref, RULE_ENCRYPTED, FindingCategory.ATTACHMENT,
                FindingSeverity.MEDIUM, FindingStatus.SUSPECTED, "PDF_ENCRYPTED",
            )
        )  # fmt: skip


def _inspect_zip(
    ref: str,
    data: bytes,
    ext: str,
    complete: bool,
    limits: AttachmentLimits,
    out: list[SecurityFinding],
) -> bool:
    """List the central directory only. Returns True when coverage is incomplete."""
    incomplete_finding = _mk(
        ref, RULE_STATIC_INCOMPLETE, FindingCategory.ATTACHMENT,
        FindingSeverity.UNKNOWN, FindingStatus.INCOMPLETE, "ZIP_DIRECTORY_UNREADABLE",
    )  # fmt: skip
    if not complete:
        out.append(incomplete_finding)
        return True
    tail_start = max(0, len(data) - limits.max_zip_tail_bytes)
    eocd = data.rfind(b"PK\x05\x06", tail_start)
    if eocd < 0 or eocd + 22 > len(data):
        out.append(incomplete_finding)
        return True
    total_entries = struct.unpack_from("<H", data, eocd + 10)[0]
    cd_size, cd_offset = struct.unpack_from("<II", data, eocd + 12)
    if total_entries == 0xFFFF or cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
        out.append(incomplete_finding)  # zip64 not parsed (documented limit)
        return True
    if cd_offset + cd_size > len(data):
        out.append(
            _mk(
                ref, RULE_MALFORMED, FindingCategory.ATTACHMENT,
                FindingSeverity.LOW, FindingStatus.SUSPECTED, "ZIP_DIRECTORY_BOUNDS",
            )
        )  # fmt: skip
        return True

    pos = cd_offset
    end = cd_offset + cd_size
    seen = 0
    encrypted = macro = traversal = nested = exe_member = bomb = False
    total_uncompressed = 0
    truncated = False
    while pos + 46 <= end and data[pos : pos + 4] == b"PK\x01\x02":
        if seen >= limits.max_zip_entries:
            truncated = True
            break
        flags = struct.unpack_from("<H", data, pos + 8)[0]
        comp = struct.unpack_from("<I", data, pos + 20)[0]
        uncomp = struct.unpack_from("<I", data, pos + 24)[0]
        nlen, xlen, clen = struct.unpack_from("<HHH", data, pos + 28)
        name_end = pos + 46 + nlen
        if name_end > end:
            break
        raw_name = data[pos + 46 : name_end]
        name = raw_name.decode("utf-8" if flags & 0x800 else "cp437", "replace")
        low = name.replace("\\", "/").lower()
        seen += 1
        encrypted |= bool(flags & 0x1)
        macro |= bool(_MACRO_MEMBER_RE.search(low))
        traversal |= (
            ".." in low.split("/") or low.startswith("/") or bool(re.match(r"^[a-z]:", low))
        )
        mext = low.rsplit(".", 1)[-1] if "." in low.rsplit("/", 1)[-1] else ""
        exe_member |= mext in _DANGEROUS_EXTS
        nested |= mext in {"zip", "rar", "7z", "gz", "jar", "iso"}
        total_uncompressed += uncomp
        if uncomp >= limits.bomb_min_entry_bytes and uncomp > limits.bomb_ratio * max(comp, 1):
            bomb = True
        pos = name_end + xlen + clen

    if total_uncompressed > limits.max_declared_uncompressed:
        bomb = True
    if seen != total_entries and not truncated:
        out.append(
            _mk(
                ref, RULE_MALFORMED, FindingCategory.ATTACHMENT,
                FindingSeverity.LOW, FindingStatus.SUSPECTED, "ZIP_ENTRY_COUNT_MISMATCH",
            )
        )  # fmt: skip
    if truncated or total_entries > limits.max_zip_entries:
        out.append(
            _mk(
                ref, RULE_STATIC_INCOMPLETE, FindingCategory.ATTACHMENT,
                FindingSeverity.UNKNOWN, FindingStatus.INCOMPLETE,
                "ZIP_ENTRY_CAP_EXCEEDED",
            )
        )  # fmt: skip
        truncated = True

    def add(rule: str, sev: FindingSeverity, st: FindingStatus, code: Code) -> None:
        out.append(_mk(ref, rule, FindingCategory.ATTACHMENT, sev, st, code))

    if macro:
        macro_ext = ext in _MACRO_EXTS
        add(
            RULE_MACRO_CAPABLE,
            FindingSeverity.MEDIUM if macro_ext else FindingSeverity.HIGH,
            FindingStatus.OBSERVED,
            "OOXML_MACRO_MEMBER" if macro_ext else "OOXML_MACRO_MEMBER_EXT_MISMATCH",
        )
    if encrypted:
        add(RULE_ENCRYPTED, FindingSeverity.MEDIUM, FindingStatus.OBSERVED, "ZIP_ENCRYPTED")
    if bomb:
        add(RULE_ARCHIVE_BOMB, FindingSeverity.HIGH, FindingStatus.SUSPECTED, "ZIP_BOMB_INDICATOR")
    if traversal:
        add(
            RULE_ARCHIVE_TRAVERSAL,
            FindingSeverity.HIGH,
            FindingStatus.OBSERVED,
            "ZIP_PATH_TRAVERSAL",
        )
    if exe_member:
        add(
            RULE_ARCHIVE_EXECUTABLE,
            FindingSeverity.HIGH,
            FindingStatus.OBSERVED,
            "ZIP_EXECUTABLE_MEMBER",
        )
    if nested:
        add(
            RULE_ARCHIVE_NESTED,
            FindingSeverity.MEDIUM,
            FindingStatus.OBSERVED,
            "ZIP_NESTED_ARCHIVE_NOT_OPENED",
        )
    return truncated


# =============================================================================
# scanner service interface
# =============================================================================


class ScanStatus(StrEnum):
    """Outcome of one scanner invocation. Only CLEAN_ENGINE_VERDICT is clean."""

    CLEAN_ENGINE_VERDICT = "CLEAN_ENGINE_VERDICT"
    DETECTED = "DETECTED"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"
    TOO_LARGE = "TOO_LARGE"
    NOT_RUN = "NOT_RUN"


@dataclass(frozen=True)
class ScanOutcome:
    """What a scanner reports. ``engine`` / ``engine_version`` are explicit."""

    status: ScanStatus
    engine: str = ""
    engine_version: str = ""
    signature_ids: tuple[str, ...] = ()


@runtime_checkable
class AttachmentScanner(Protocol):
    """Local-only, isolated, unprivileged scanner contract (R-005.4, R-017.3).

    Implementations must run on a separated, unprivileged scanner host/process
    — never GPU/inference nodes — and must never forward bytes to a third-party
    service. The contract takes bytes and a deadline only: no URL/address.
    """

    async def scan(self, payload: bytes, *, deadline_seconds: float) -> ScanOutcome: ...


class NullAttachmentScanner:
    """Default: no scanner configured. Always ``NOT_RUN`` (never clean)."""

    async def scan(self, payload: bytes, *, deadline_seconds: float) -> ScanOutcome:
        return ScanOutcome(status=ScanStatus.NOT_RUN, engine="none")


_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9._-]")
_INCOMPLETE_CODES: dict[ScanStatus, Code] = {
    ScanStatus.ERROR: "SCANNER_ERROR",
    ScanStatus.TIMEOUT: "SCANNER_TIMEOUT",
    ScanStatus.UNAVAILABLE: "SCANNER_UNAVAILABLE",
    ScanStatus.TOO_LARGE: "ATTACHMENT_TOO_LARGE",
    ScanStatus.NOT_RUN: "SCAN_NOT_RUN",
}


def _token(value: object, limit: int) -> str:
    return _SAFE_TOKEN_RE.sub("_", str(value))[:limit] if isinstance(value, str) else ""


@dataclass(frozen=True)
class ScanRecord:
    """Recorded scan result. ``clean_by_engine`` is evidence, NOT safety."""

    evidence_ref: str
    status: ScanStatus
    engine: str
    engine_version: str
    signature_ids: tuple[str, ...]
    finding: SecurityFinding

    @property
    def clean_by_engine(self) -> bool:
        return self.status is ScanStatus.CLEAN_ENGINE_VERDICT

    @property
    def incomplete(self) -> bool:
        return self.status not in (ScanStatus.CLEAN_ENGINE_VERDICT, ScanStatus.DETECTED)


def _record(
    ref: str,
    status: ScanStatus,
    engine: str,
    version: str,
    sigs: tuple[str, ...] = (),
    reason: Code | None = None,
) -> ScanRecord:
    detector = f"{engine}@{version}"[:100] if engine and version else ATTACHMENTS_VERSION
    if status is ScanStatus.CLEAN_ENGINE_VERDICT:
        finding = _mk(
            ref, RULE_ENGINE_CLEAN, FindingCategory.ATTACHMENT, FindingSeverity.INFO,
            FindingStatus.OBSERVED, "ENGINE_CLEAN_VERDICT_NOT_SAFETY",
            detector_version=detector,
        )  # fmt: skip
    elif status is ScanStatus.DETECTED:
        finding = _mk(
            ref, RULE_SCANNER_DETECTION, FindingCategory.MALWARE, FindingSeverity.HIGH,
            FindingStatus.OBSERVED, "SCANNER_DETECTION", detector_version=detector,
            extra_refs=[f"sig:{s.replace('.', '_')}" for s in sigs],
        )  # fmt: skip
    else:
        finding = _mk(
            ref, RULE_SCAN_INCOMPLETE, FindingCategory.SCANNER_FAILURE,
            FindingSeverity.UNKNOWN, FindingStatus.INCOMPLETE,
            reason or _INCOMPLETE_CODES[status], detector_version=detector,
        )  # fmt: skip
    return ScanRecord(ref, status, engine, version, sigs, finding)


async def run_scan(
    scanner: AttachmentScanner,
    payload: bytes | None,
    *,
    evidence_ref: str,
    max_bytes: int = 16 * 1024 * 1024,
    timeout_seconds: float = 10.0,
) -> ScanRecord:
    """Run ``scanner`` under a byte cap and timeout; never raises, never lies.

    Any failure mode (exception, timeout, oversize, missing payload, malformed
    outcome, clean without engine+version) yields an INCOMPLETE record.
    """
    if payload is None:
        return _record(evidence_ref, ScanStatus.NOT_RUN, "", "")
    if len(payload) > max_bytes:
        return _record(evidence_ref, ScanStatus.TOO_LARGE, "", "")
    try:
        outcome = await asyncio.wait_for(
            scanner.scan(payload, deadline_seconds=timeout_seconds),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        return _record(evidence_ref, ScanStatus.TIMEOUT, "", "")
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - contain; never surface exception text
        return _record(evidence_ref, ScanStatus.ERROR, "", "", reason="SCANNER_EXCEPTION")
    if not isinstance(outcome, ScanOutcome) or not isinstance(outcome.status, ScanStatus):
        return _record(evidence_ref, ScanStatus.ERROR, "", "", reason="SCANNER_BAD_OUTCOME")
    engine = _token(outcome.engine, 40)
    version = _token(outcome.engine_version, 60)
    sigs = tuple(t for t in (_token(s, 40) for s in list(outcome.signature_ids)[:8]) if t)
    if outcome.status is ScanStatus.CLEAN_ENGINE_VERDICT and not (engine and version):
        # I-02/R-005.4: a clean verdict needs an explicit engine + version.
        return _record(
            evidence_ref, ScanStatus.ERROR, engine, version, reason="ENGINE_VERSION_MISSING"
        )
    if outcome.status is ScanStatus.CLEAN_ENGINE_VERDICT and outcome.signature_ids:
        return _record(
            evidence_ref, ScanStatus.ERROR, engine, version, reason="SCANNER_BAD_OUTCOME"
        )
    return _record(evidence_ref, outcome.status, engine, version, sigs)


@dataclass(frozen=True)
class ScanSummary:
    """Aggregate over records. ``all_engine_clean`` is NOT a safety claim."""

    total: int
    detected: int
    incomplete: int
    engine_clean: int

    @property
    def complete(self) -> bool:
        return self.incomplete == 0


def summarize_scans(records: Sequence[ScanRecord]) -> ScanSummary:
    return ScanSummary(
        total=len(records),
        detected=sum(r.status is ScanStatus.DETECTED for r in records),
        incomplete=sum(r.incomplete for r in records),
        engine_clean=sum(r.clean_by_engine for r in records),
    )


__all__ = [
    "ATTACHMENTS_VERSION",
    "AttachmentInspection",
    "AttachmentLimits",
    "AttachmentScanner",
    "NullAttachmentScanner",
    "ScanOutcome",
    "ScanRecord",
    "ScanStatus",
    "ScanSummary",
    "inspect_attachment",
    "run_scan",
    "safe_filename_form",
    "summarize_scans",
]
