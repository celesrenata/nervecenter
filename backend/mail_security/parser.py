"""Offline, resource-bounded MIME evidence parser for NerveShield (Task 1.3).

This module turns raw RFC 5322 / MIME source bytes into *structured evidence*
without ever touching the network, executing code, following links, loading
remote images, or opening/extracting archives. It wraps Python's standard
:mod:`email` library (``BytesParser`` with the lenient ``default`` policy) in a
hardened, bounded driver so that malformed and adversarial input yields typed
errors plus safely bounded partial evidence rather than crashing the worker
(R-003.2, T-06).

Design anchors (`.kiro/specs/nerve-shield/design.md` §4.3 "Parser", §5.2
"Offline inspections", §9 "Data storage"):

- **Standard parser + hardened wrapper.** We use ``email.parser.BytesParser``
  with ``email.policy.default`` (a reviewed, in-repo-standard parser; no new
  third-party dependency) and never call any method that would resolve or fetch
  a URL, image or external resource.
- **Explicit, configurable limits** (:class:`ParserLimits`) for source bytes,
  MIME nesting depth, part count, per-header size, header count, total decoded
  bytes and filename length, plus a dangerous-encoding guard and a refusal to
  decompress archives. Every breach stops at the limit and records a typed
  ``INSPECTION_INCOMPLETE`` reason code — truncated/failed input is *never*
  treated as clean (I-02, R-003.3).
- **Extraction without side effects** (R-003.1/.4/.5, §5.2): plain-text and
  HTML parts; links from both HTML attributes (``href``/``src``/``action``/…)
  and visible text, retaining *both* the display text and the actual target;
  inline / content-ID images and external-resource references flagged as
  evidence (never fetched — T-05); attachment *descriptors* only (filename,
  claimed content-type, size, sha256, double-extension / magic-sniff metadata)
  with no extraction or execution (T-08 boundary); and parse-failure metadata.
- **Raw→normalized provenance (I-05, R-003.1).** Human-facing strings are
  Unicode-normalized (NFC) for display/comparison, but the raw form is retained
  alongside the normalized form so a lossy normalization is never mistaken for
  the original. Opaque evidence refs tie each extracted item back to its MIME
  location.

Output shape (design §9 isolation boundary): the rich :class:`ParsedEvidence`
object is **transient / in-memory only** — it is documented as such and is not a
persisted contract model. It may carry short raw fragments (display text, link
targets) needed by later deterministic inspections, but those live only in
process memory. The parser additionally emits :class:`MessageEvidenceManifest`
and typed :class:`SecurityFinding` objects from
:mod:`backend.mail_security.models` (via :meth:`ParsedEvidence.to_findings`)
and a :func:`evidence_fidelity_for` classifier that feeds a
:class:`~backend.mail_security.models.MessageEvidenceManifest`, so the persisted
surface stays metadata + opaque refs only, exactly like Tasks 1.1/1.2.

No code in this module performs any I/O other than reading the in-memory bytes
it is handed; there is no import of ``socket``, ``urllib``, ``requests``,
``http`` or any archive-extraction call, and the parser is a pure function of
its inputs.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.parser import BytesParser
from enum import StrEnum
from html.parser import HTMLParser

from backend.mail_security.models import (
    Code,
    EvidenceFidelity,
    FindingCategory,
    FindingSeverity,
    FindingStatus,
    SecurityFinding,
)

# -- detector identity -------------------------------------------------------

#: Stable version of this parser's behavior, recorded on every emitted finding
#: so deterministic evidence is reproducible/auditable (R-002, R-005.5).
PARSER_VERSION = "mime-evidence-parser@1"

#: ``rule_id`` recorded on the typed INSPECTION_INCOMPLETE finding (design §5.2
#: findings carry a stable rule id). Distinct from the per-reason explanation
#: codes so dashboards can group all bound breaches under one rule.
_INSPECTION_INCOMPLETE_RULE_ID = "mime.inspection.incomplete"


# -- typed limit / failure reason codes --------------------------------------


class IncompleteReason(StrEnum):
    """Why inspection stopped short of fully parsing the message.

    Each value is a safe upper-snake :data:`backend.mail_security.models.Code`
    so it can be used directly as a finding ``explanation_code`` and can never
    carry raw email text (I-05). These are the ``INSPECTION_INCOMPLETE``
    sub-reasons required by R-003.3 / T-06 / T-07.
    """

    SOURCE_BYTES_EXCEEDED = "SOURCE_BYTES_EXCEEDED"
    MIME_DEPTH_EXCEEDED = "MIME_DEPTH_EXCEEDED"
    PART_COUNT_EXCEEDED = "PART_COUNT_EXCEEDED"
    HEADER_COUNT_EXCEEDED = "HEADER_COUNT_EXCEEDED"
    HEADER_SIZE_EXCEEDED = "HEADER_SIZE_EXCEEDED"
    DECODED_BYTES_EXCEEDED = "DECODED_BYTES_EXCEEDED"
    FILENAME_LENGTH_EXCEEDED = "FILENAME_LENGTH_EXCEEDED"
    DANGEROUS_ENCODING = "DANGEROUS_ENCODING"
    ARCHIVE_NOT_INSPECTED = "ARCHIVE_NOT_INSPECTED"
    MALFORMED_MIME = "MALFORMED_MIME"
    DECODE_ERROR = "DECODE_ERROR"
    EMPTY_SOURCE = "EMPTY_SOURCE"


# Reasons that mean "we stopped at a configured safety limit" (as opposed to a
# recoverable malformed-structure note). All of them keep the evidence partial
# but valid; none may ever be treated as a clean result (I-02).
_LIMIT_REASONS: frozenset[IncompleteReason] = frozenset(
    {
        IncompleteReason.SOURCE_BYTES_EXCEEDED,
        IncompleteReason.MIME_DEPTH_EXCEEDED,
        IncompleteReason.PART_COUNT_EXCEEDED,
        IncompleteReason.HEADER_COUNT_EXCEEDED,
        IncompleteReason.HEADER_SIZE_EXCEEDED,
        IncompleteReason.DECODED_BYTES_EXCEEDED,
        IncompleteReason.FILENAME_LENGTH_EXCEEDED,
        IncompleteReason.DANGEROUS_ENCODING,
        IncompleteReason.ARCHIVE_NOT_INSPECTED,
    }
)


# -- configurable bounds ------------------------------------------------------


@dataclass(frozen=True)
class ParserLimits:
    """Explicit, configurable resource caps for the bounded parser (R-003.3).

    Defaults are deliberately conservative but large enough for ordinary mail;
    tests override them to tiny values to prove each cap triggers a typed
    ``INSPECTION_INCOMPLETE`` without crashing. ``frozen=True`` mirrors the
    immutable-config convention used elsewhere in the package.
    """

    #: Hard cap on raw source bytes handed to the parser (T-06 MIME bomb).
    max_source_bytes: int = 25 * 1024 * 1024
    #: Maximum MIME nesting depth walked before stopping (T-06 deep nesting).
    max_depth: int = 20
    #: Maximum number of MIME parts visited (T-06 excessive part count).
    max_parts: int = 1000
    #: Maximum number of headers parsed on the top-level message.
    max_header_count: int = 256
    #: Maximum size in bytes of a single header (name + value).
    max_header_bytes: int = 16 * 1024
    #: Cap on cumulative decoded body/text bytes extracted across all parts.
    max_total_decoded_bytes: int = 10 * 1024 * 1024
    #: Cap on cumulative decoded bytes of a single part (defense in depth).
    max_part_decoded_bytes: int = 5 * 1024 * 1024
    #: Maximum retained filename length before it is truncated + flagged.
    max_filename_length: int = 255
    #: Maximum number of links/images/attachments retained per message.
    max_components: int = 2000
    #: Maximum characters of display/target text retained per link.
    max_link_text: int = 2048
    #: Content-Transfer-Encoding tokens treated as dangerous/unsupported and
    #: refused rather than decoded (keeps the decoder on known-safe paths).
    dangerous_encodings: frozenset[str] = frozenset({"uuencode", "x-uuencode"})

    def __post_init__(self) -> None:
        for name in (
            "max_source_bytes",
            "max_depth",
            "max_parts",
            "max_header_count",
            "max_header_bytes",
            "max_total_decoded_bytes",
            "max_part_decoded_bytes",
            "max_filename_length",
            "max_components",
            "max_link_text",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive int")


#: Reusable default limits so callers that do not care get a safe config.
DEFAULT_LIMITS = ParserLimits()


# -- archive / dangerous content heuristics ----------------------------------

#: Filename extensions that denote an archive. The parser never opens or
#: decompresses these (T-07 zip bomb / path traversal); it records a descriptor
#: and an ARCHIVE_NOT_INSPECTED reason instead. Archive *support* is explicitly
#: out of scope for Task 1.3 — see the module/report notes.
_ARCHIVE_EXTENSIONS: frozenset[str] = frozenset(
    {"zip", "gz", "tgz", "bz2", "7z", "rar", "xz", "tar", "cab", "jar"}
)

#: Content-types that denote an archive container (parallel to the extension
#: heuristic; either match flags the attachment as un-inspected).
_ARCHIVE_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "application/zip",
        "application/x-zip-compressed",
        "application/gzip",
        "application/x-gzip",
        "application/x-7z-compressed",
        "application/x-rar-compressed",
        "application/vnd.rar",
        "application/x-tar",
        "application/x-bzip2",
        "application/java-archive",
    }
)

#: Minimal magic-number table for lightweight content sniffing (no execution).
#: Each entry maps a sniffed type label to a byte prefix. Used only to compare
#: against the *claimed* content-type (double-extension / spoof evidence).
_MAGIC_PREFIXES: tuple[tuple[bytes, str], ...] = (
    (b"MZ", "application/x-dosexec"),
    (b"\x7fELF", "application/x-elf"),
    (b"PK\x03\x04", "application/zip"),
    (b"PK\x05\x06", "application/zip"),
    (b"%PDF-", "application/pdf"),
    (b"\x1f\x8b", "application/gzip"),
    (b"Rar!", "application/x-rar-compressed"),
    (b"7z\xbc\xaf\x27\x1c", "application/x-7z-compressed"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

_URL_IN_TEXT_RE = re.compile(
    r"""(?xi)
    \b(
        (?:https?|ftp|mailto)://[^\s<>"'\]\)]+   # scheme URLs
        |
        www\.[^\s<>"'\]\)]+                      # bare www. hosts
    )
    """
)

# HTML attributes that can reference a resource or action target (design §4.3).
_URL_ATTRS: frozenset[str] = frozenset(
    {"href", "src", "action", "background", "poster", "formaction", "data"}
)

# Tags whose src/background typically loads an external image/resource.
_RESOURCE_TAGS: frozenset[str] = frozenset(
    {"img", "image", "input", "body", "video", "audio", "source", "track"}
)


# -- transient, in-memory evidence records (NOT persisted) --------------------


@dataclass
class LinkEvidence:
    """A link extracted from HTML or text. Transient / in-memory only.

    Retains both ``display_text`` (what a human would see) and ``target`` (the
    actual href/src) so deceptive links where the two differ are preserved as
    evidence (R-003.5). ``target_raw`` keeps the un-normalized target for
    provenance (I-05). Nothing here is ever fetched.
    """

    evidence_ref: str
    target: str
    target_raw: str
    display_text: str
    source: str  # "html_attr" | "text"
    attribute: str | None = None


@dataclass
class ImageEvidence:
    """An inline or external image reference. Transient / in-memory only.

    ``is_remote`` flags an external/remote resource (candidate tracking pixel)
    that is recorded but *never* fetched (T-05). ``content_id`` is set for
    inline ``cid:`` images.
    """

    evidence_ref: str
    target: str
    target_raw: str
    is_remote: bool
    is_inline: bool
    content_id: str | None = None
    alt_text: str = ""


@dataclass
class AttachmentDescriptor:
    """Static descriptor for an attachment. Transient / in-memory only.

    Describes the attachment without extracting or executing it (T-08). Holds a
    sha256 of the decoded bytes (for later intel matching) plus spoof metadata:
    ``double_extension`` and ``magic_mismatch`` compare the claimed type/name to
    a lightweight magic sniff. ``is_archive`` marks containers the parser
    refused to open (T-07).
    """

    evidence_ref: str
    filename: str
    filename_raw: str
    filename_truncated: bool
    claimed_content_type: str
    size_bytes: int
    sha256: str | None
    sniffed_type: str | None
    double_extension: bool
    magic_mismatch: bool
    is_archive: bool
    #: Transient decoded bytes, set ONLY when the caller asked the parser to
    #: retain them (an attachment scanner is configured) and the decode was not
    #: truncated. Never persisted, logged or repr'd; the worker clears it right
    #: after scanning. Default ``None``: the parser retains nothing.
    payload: bytes | None = field(default=None, repr=False, compare=False)


@dataclass
class TextPart:
    """A decoded text/plain or text/html part. Transient / in-memory only.

    ``normalized`` is NFC Unicode-normalized for display/comparison; ``raw`` is
    the decoded-but-un-normalized form kept for provenance (I-05, R-003.1).
    ``truncated`` marks that a decode cap stopped the part early.
    """

    evidence_ref: str
    subtype: str  # "plain" | "html"
    raw: str
    normalized: str
    truncated: bool


@dataclass
class IncompleteNote:
    """A typed note recording why inspection was partial. In-memory only.

    ``reason`` is a :class:`IncompleteReason`; ``evidence_ref`` points at the
    offending MIME location when known. These map 1:1 onto emitted
    ``INSPECTION_INCOMPLETE`` findings (R-003.3, I-02).
    """

    reason: IncompleteReason
    evidence_ref: str | None = None


@dataclass
class ParsedEvidence:
    """Rich, transient in-memory result of bounded MIME parsing.

    **Not a persisted contract model.** It may hold short raw fragments needed
    by downstream deterministic inspections (Task 1.5); those stay in process
    memory only. Use :meth:`to_findings` / :meth:`completeness_codes` to emit
    the persisted, metadata-only surface (design §9).

    ``inspection_complete`` is ``True`` only when *no* limit/failure note was
    recorded. A truncated or failed parse is never complete and therefore never
    clean (I-02).
    """

    source_sha256: str
    source_bytes: int
    headers: list[tuple[str, str]] = field(default_factory=list)
    text_parts: list[TextPart] = field(default_factory=list)
    links: list[LinkEvidence] = field(default_factory=list)
    images: list[ImageEvidence] = field(default_factory=list)
    attachments: list[AttachmentDescriptor] = field(default_factory=list)
    incomplete_notes: list[IncompleteNote] = field(default_factory=list)
    part_count: int = 0
    max_depth_seen: int = 0

    @property
    def inspection_complete(self) -> bool:
        """``True`` iff parsing finished within all limits with no failures."""
        return not self.incomplete_notes

    @property
    def has_remote_resources(self) -> bool:
        """``True`` if any external image/resource reference was recorded.

        Remote resources are evidence of possible tracking (T-05); they are
        never fetched. Callers use this to raise a remote-resource finding.
        """
        return any(image.is_remote for image in self.images)

    def completeness_codes(self) -> list[Code]:
        """Return the unique set of INSPECTION_INCOMPLETE reason codes.

        Suitable for :class:`backend.mail_security.models.Completeness`
        ``missing``/``checks_not_run`` lists. Order-stable and de-duplicated.
        """
        seen: dict[str, None] = {}
        for note in self.incomplete_notes:
            seen.setdefault(note.reason.value, None)
        return list(seen.keys())

    def to_findings(self) -> list[SecurityFinding]:
        """Emit one typed ``INSPECTION_INCOMPLETE`` finding per distinct reason.

        The persisted surface is metadata + opaque refs only (design §9): each
        finding carries a safe ``explanation_code`` (the reason), a stable
        ``rule_id`` and ``detector_version``, and no raw body text. A finding is
        ``INCOMPLETE`` status with ``UNKNOWN`` severity because partial
        inspection can never assert safety (I-02).
        """
        findings: list[SecurityFinding] = []
        for index, reason in enumerate(self.completeness_codes()):
            findings.append(
                SecurityFinding(
                    finding_key=f"mime-incomplete-{index}-{reason}".replace("_", "-"),
                    rule_id=_INSPECTION_INCOMPLETE_RULE_ID,
                    category=FindingCategory.SCANNER_FAILURE,
                    severity=FindingSeverity.UNKNOWN,
                    status=FindingStatus.INCOMPLETE,
                    detector_version=PARSER_VERSION,
                    evidence_refs=[],
                    explanation_code=reason,
                )
            )
        return findings


# =============================================================================
# internal bounded-walk state
# =============================================================================


class _ParseState:
    """Mutable accumulator for a single bounded parse. Internal only."""

    def __init__(self, limits: ParserLimits) -> None:
        self.limits = limits
        self.retain_attachment_payloads = False
        self.total_decoded = 0
        self.part_count = 0
        self.component_count = 0
        self._seen_reasons: set[tuple[IncompleteReason, str | None]] = set()
        self.evidence: ParsedEvidence | None = None

    def note(self, reason: IncompleteReason, evidence_ref: str | None = None) -> None:
        """Record a de-duplicated typed incomplete note."""
        key = (reason, evidence_ref)
        if key in self._seen_reasons:
            return
        self._seen_reasons.add(key)
        assert self.evidence is not None
        self.evidence.incomplete_notes.append(
            IncompleteNote(reason=reason, evidence_ref=evidence_ref)
        )

    def can_add_component(self) -> bool:
        if self.component_count >= self.limits.max_components:
            return False
        self.component_count += 1
        return True


# =============================================================================
# public entry point
# =============================================================================


def parse_mime_evidence(
    source: bytes,
    *,
    limits: ParserLimits = DEFAULT_LIMITS,
    retain_attachment_payloads: bool = False,
) -> ParsedEvidence:
    """Parse ``source`` MIME bytes into bounded, structured evidence.

    ``retain_attachment_payloads`` (default ``False``) keeps each untruncated
    decoded attachment (already bounded by ``max_part_decoded_bytes``) on its
    descriptor, transiently, so an explicitly configured scanner can scan it.

    This is a pure function of ``(source, limits)``: it performs no network,
    file-system or subprocess I/O and never executes attachment or script
    content. Malformed/adversarial input yields partial evidence plus typed
    :class:`IncompleteNote` entries instead of raising (R-003.2).

    Returns a transient :class:`ParsedEvidence`; call its ``to_findings`` /
    ``completeness_codes`` for the persisted metadata surface.
    """
    if not isinstance(source, (bytes, bytearray)):
        raise TypeError("source must be bytes")
    source = bytes(source)

    sha = hashlib.sha256(source).hexdigest()
    state = _ParseState(limits)
    state.retain_attachment_payloads = retain_attachment_payloads
    evidence = ParsedEvidence(source_sha256=sha, source_bytes=len(source))
    state.evidence = evidence

    if len(source) == 0:
        state.note(IncompleteReason.EMPTY_SOURCE)
        return evidence

    # Bounded source-byte cap: parse only the capped prefix and flag truncation
    # (T-06). We still parse the prefix so partial evidence is available.
    working = source
    if len(source) > limits.max_source_bytes:
        working = source[: limits.max_source_bytes]
        state.note(IncompleteReason.SOURCE_BYTES_EXCEEDED)

    try:
        message = _safe_parse(working)
    except Exception:  # noqa: BLE001 - never crash the worker (R-003.2)
        state.note(IncompleteReason.MALFORMED_MIME)
        return evidence

    _extract_headers(message, state)
    _walk(message, state, depth=0)

    evidence.part_count = state.part_count
    return evidence


def _safe_parse(working: bytes) -> EmailMessage:
    """Parse bytes with the standard lenient policy; never fetch anything.

    ``BytesParser`` with ``policy.default`` tolerates many malformed structures
    and surfaces problems via ``defects`` rather than exceptions. We fall back
    to ``message_from_bytes`` if the parser object rejects the policy for some
    edge input.
    """
    try:
        parsed = BytesParser(policy=policy.default).parsebytes(working)
    except Exception:  # noqa: BLE001 - defensive: try the module-level helper
        parsed = message_from_bytes(working, policy=policy.default)
    # policy.default yields EmailMessage instances.
    return parsed  # type: ignore[return-value]


# =============================================================================
# header extraction (bounded)
# =============================================================================


def _extract_headers(message: EmailMessage, state: _ParseState) -> None:
    """Capture bounded, normalized top-level headers (R-002/§4.3).

    Caps header count and per-header size; oversized headers are truncated and
    flagged rather than retained in full. Header *values* are kept normalized
    for display but never interpreted as instructions (steering: content is
    data).
    """
    assert state.evidence is not None
    limits = state.limits
    count = 0
    for name, raw_value in message.items():
        if count >= limits.max_header_count:
            state.note(IncompleteReason.HEADER_COUNT_EXCEEDED)
            break
        count += 1
        value = str(raw_value)
        encoded_len = len((f"{name}: {value}").encode("utf-8", "replace"))
        if encoded_len > limits.max_header_bytes:
            state.note(IncompleteReason.HEADER_SIZE_EXCEEDED)
            value = value.encode("utf-8", "replace")[: limits.max_header_bytes].decode(
                "utf-8", "replace"
            )
        state.evidence.headers.append((str(name), _normalize(value)))


# =============================================================================
# recursive bounded MIME walk
# =============================================================================


def _walk(part: EmailMessage, state: _ParseState, *, depth: int) -> None:
    """Recursively walk MIME parts within depth/part/decode caps.

    Enforces :attr:`ParserLimits.max_depth` and :attr:`max_parts`; on breach it
    records a typed note and stops descending that branch rather than
    recursing without bound (T-06 deep nesting / part explosion).
    """
    assert state.evidence is not None
    limits = state.limits

    if depth > state.evidence.max_depth_seen:
        state.evidence.max_depth_seen = depth

    if depth > limits.max_depth:
        state.note(IncompleteReason.MIME_DEPTH_EXCEEDED)
        return

    state.part_count += 1
    if state.part_count > limits.max_parts:
        state.note(IncompleteReason.PART_COUNT_EXCEEDED)
        return

    if part.is_multipart():
        children = part.get_payload()
        if not isinstance(children, list):
            # Declared multipart but payload is not a list -> malformed.
            state.note(IncompleteReason.MALFORMED_MIME)
            return
        for child in children:
            if state.part_count >= limits.max_parts:
                state.note(IncompleteReason.PART_COUNT_EXCEEDED)
                break
            if isinstance(child, EmailMessage):
                _walk(child, state, depth=depth + 1)
            else:  # pragma: no cover - policy.default yields EmailMessage
                state.note(IncompleteReason.MALFORMED_MIME)
        return

    _handle_leaf(part, state)


def _handle_leaf(part: EmailMessage, state: _ParseState) -> None:
    """Dispatch a single non-multipart part to the right extractor."""
    content_type = (part.get_content_type() or "").lower()
    disposition = (part.get_content_disposition() or "").lower()
    filename = part.get_filename()

    is_attachment = disposition == "attachment" or (
        filename is not None and disposition != "inline"
    )

    if is_attachment:
        _handle_attachment(part, state, content_type=content_type)
        return

    if content_type == "text/plain":
        _handle_text(part, state, subtype="plain")
    elif content_type == "text/html":
        _handle_text(part, state, subtype="html")
    elif content_type.startswith("image/"):
        _handle_inline_image(part, state, content_type=content_type)
    else:
        # Unknown inline part: record a descriptor-style attachment so nothing
        # is silently dropped, but never decode/execute it.
        _handle_attachment(part, state, content_type=content_type)


# =============================================================================
# leaf handlers
# =============================================================================


def _decode_bounded(
    part: EmailMessage, state: _ParseState, evidence_ref: str
) -> bytes | None:
    """Decode a part's payload within per-part and total decode caps.

    Returns ``None`` (and records a typed note) when the Content-Transfer-
    Encoding is dangerous/unsupported, when decoding fails, or when a size cap
    is hit. Never raises (R-003.2).
    """
    cte = (part.get("Content-Transfer-Encoding") or "").strip().lower()
    if cte in state.limits.dangerous_encodings:
        state.note(IncompleteReason.DANGEROUS_ENCODING, evidence_ref)
        return None

    try:
        raw = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001 - malformed encoding, stay bounded
        state.note(IncompleteReason.DECODE_ERROR, evidence_ref)
        return None

    if raw is None:
        return None
    if not isinstance(raw, (bytes, bytearray)):  # pragma: no cover - defensive
        state.note(IncompleteReason.DECODE_ERROR, evidence_ref)
        return None
    raw = bytes(raw)

    if len(raw) > state.limits.max_part_decoded_bytes:
        raw = raw[: state.limits.max_part_decoded_bytes]
        state.note(IncompleteReason.DECODED_BYTES_EXCEEDED, evidence_ref)

    remaining = state.limits.max_total_decoded_bytes - state.total_decoded
    if remaining <= 0:
        state.note(IncompleteReason.DECODED_BYTES_EXCEEDED, evidence_ref)
        return None
    if len(raw) > remaining:
        raw = raw[:remaining]
        state.note(IncompleteReason.DECODED_BYTES_EXCEEDED, evidence_ref)
    state.total_decoded += len(raw)
    return raw


def _handle_text(part: EmailMessage, state: _ParseState, *, subtype: str) -> None:
    """Extract a decoded text/plain or text/html part and its links/images."""
    assert state.evidence is not None
    index = len(state.evidence.text_parts)
    evidence_ref = f"part:text:{subtype}:{index}"

    raw_bytes = _decode_bounded(part, state, evidence_ref)
    if raw_bytes is None:
        return

    charset = part.get_content_charset() or "utf-8"
    try:
        raw_text = raw_bytes.decode(charset, "replace")
    except (LookupError, ValueError):
        raw_text = raw_bytes.decode("utf-8", "replace")

    truncated = any(
        note.reason is IncompleteReason.DECODED_BYTES_EXCEEDED
        and note.evidence_ref == evidence_ref
        for note in state.evidence.incomplete_notes
    )

    state.evidence.text_parts.append(
        TextPart(
            evidence_ref=evidence_ref,
            subtype=subtype,
            raw=raw_text,
            normalized=_normalize(raw_text),
            truncated=truncated,
        )
    )

    if subtype == "html":
        _extract_from_html(raw_text, state, evidence_ref)
    else:
        _extract_urls_from_text(raw_text, state, evidence_ref)


def _handle_inline_image(
    part: EmailMessage, state: _ParseState, *, content_type: str
) -> None:
    """Record an inline image (usually ``cid:``) as evidence; never fetch it."""
    assert state.evidence is not None
    if not state.can_add_component():
        state.note(IncompleteReason.PART_COUNT_EXCEEDED)
        return
    index = len(state.evidence.images)
    evidence_ref = f"part:image:{index}"
    content_id = part.get("Content-ID")
    cid = content_id.strip("<>") if isinstance(content_id, str) else None
    state.evidence.images.append(
        ImageEvidence(
            evidence_ref=evidence_ref,
            target=f"cid:{cid}" if cid else evidence_ref,
            target_raw=content_id or "",
            is_remote=False,
            is_inline=True,
            content_id=cid,
        )
    )


def _handle_attachment(
    part: EmailMessage, state: _ParseState, *, content_type: str
) -> None:
    """Record a static attachment descriptor; never extract or execute it.

    Computes a sha256 of the decoded bytes (bounded), performs a lightweight
    magic sniff to compare against the claimed type, flags double extensions and
    archive containers (which are *not* opened — T-07), and caps filename
    length. Produces no side effects (T-08).
    """
    assert state.evidence is not None
    if not state.can_add_component():
        state.note(IncompleteReason.PART_COUNT_EXCEEDED)
        return

    index = len(state.evidence.attachments)
    evidence_ref = f"part:attachment:{index}"

    raw_filename = part.get_filename() or ""
    filename = _normalize(raw_filename)
    filename_truncated = False
    if len(filename) > state.limits.max_filename_length:
        filename = filename[: state.limits.max_filename_length]
        filename_truncated = True
        state.note(IncompleteReason.FILENAME_LENGTH_EXCEEDED, evidence_ref)

    ext = _lower_ext(raw_filename)
    is_archive = ext in _ARCHIVE_EXTENSIONS or content_type in _ARCHIVE_CONTENT_TYPES
    if is_archive:
        # We refuse to decompress archives (zip-bomb / path-traversal, T-07).
        state.note(IncompleteReason.ARCHIVE_NOT_INSPECTED, evidence_ref)

    decoded = _decode_bounded(part, state, evidence_ref)
    size_bytes = len(decoded) if decoded is not None else 0
    sha256 = hashlib.sha256(decoded).hexdigest() if decoded is not None else None
    sniffed = _sniff_magic(decoded) if decoded is not None else None

    magic_mismatch = bool(
        sniffed is not None
        and content_type
        and sniffed != content_type
        and not _types_compatible(sniffed, content_type)
    )

    truncated_decode = any(
        n.reason is IncompleteReason.DECODED_BYTES_EXCEEDED
        and n.evidence_ref == evidence_ref
        for n in state.evidence.incomplete_notes
    )
    retained = (
        decoded
        if state.retain_attachment_payloads and decoded is not None and not truncated_decode
        else None
    )

    state.evidence.attachments.append(
        AttachmentDescriptor(
            evidence_ref=evidence_ref,
            filename=filename,
            filename_raw=raw_filename,
            filename_truncated=filename_truncated,
            claimed_content_type=content_type,
            size_bytes=size_bytes,
            sha256=sha256,
            sniffed_type=sniffed,
            double_extension=_has_double_extension(raw_filename),
            magic_mismatch=magic_mismatch,
            is_archive=is_archive,
            payload=retained,
        )
    )


# =============================================================================
# link / image extraction helpers
# =============================================================================


class _LinkHTMLParser(HTMLParser):
    """Stdlib HTML parser that *only* records structure. Never renders/fetches.

    Collects resource/action URLs from attributes (href/src/action/…), tracks
    anchor display text so deceptive-link evidence (display != target) is
    retained (R-003.5), and captures ``alt`` text and hidden blocks as inert
    evidence (T-11). It executes nothing and follows nothing.
    """

    def __init__(self, state: _ParseState, evidence_ref: str) -> None:
        super().__init__(convert_charrefs=True)
        self._state = state
        self._evidence_ref = evidence_ref
        self._anchor_stack: list[dict[str, str]] = []
        self._link_count = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        attr_map = {name.lower(): (value or "") for name, value in attrs}

        if tag == "a":
            href = attr_map.get("href", "")
            self._anchor_stack.append({"href": href, "text": ""})

        for attr_name in _URL_ATTRS & attr_map.keys():
            target = attr_map[attr_name].strip()
            if not target or target.lower().startswith("cid:"):
                if target.lower().startswith("cid:"):
                    self._record_inline_cid(target)
                continue
            if tag in _RESOURCE_TAGS and attr_name in {"src", "background", "poster"}:
                self._record_image(target, attr_map.get("alt", ""))
            else:
                self._record_link(target, display_text="", attribute=attr_name)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._anchor_stack:
            self._anchor_stack[-1]["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._anchor_stack:
            anchor = self._anchor_stack.pop()
            href = anchor["href"].strip()
            if href and not href.lower().startswith("cid:"):
                self._record_link(
                    href, display_text=anchor["text"], attribute="href"
                )

    # -- recording (bounded) -------------------------------------------------

    def _record_link(
        self, target: str, *, display_text: str, attribute: str | None
    ) -> None:
        if not self._state.can_add_component():
            self._state.note(IncompleteReason.PART_COUNT_EXCEEDED, self._evidence_ref)
            return
        evidence = self._state.evidence
        assert evidence is not None
        cap = self._state.limits.max_link_text
        evidence.links.append(
            LinkEvidence(
                evidence_ref=f"{self._evidence_ref}:link:{len(evidence.links)}",
                target=_normalize(target)[:cap],
                target_raw=target[:cap],
                display_text=_normalize(display_text).strip()[:cap],
                source="html_attr",
                attribute=attribute,
            )
        )

    def _record_image(self, target: str, alt: str) -> None:
        if not self._state.can_add_component():
            self._state.note(IncompleteReason.PART_COUNT_EXCEEDED, self._evidence_ref)
            return
        evidence = self._state.evidence
        assert evidence is not None
        cap = self._state.limits.max_link_text
        is_remote = _is_remote_resource(target)
        evidence.images.append(
            ImageEvidence(
                evidence_ref=f"{self._evidence_ref}:img:{len(evidence.images)}",
                target=_normalize(target)[:cap],
                target_raw=target[:cap],
                is_remote=is_remote,
                is_inline=not is_remote,
                content_id=None,
                alt_text=_normalize(alt)[:cap],
            )
        )

    def _record_inline_cid(self, target: str) -> None:
        if not self._state.can_add_component():
            return
        evidence = self._state.evidence
        assert evidence is not None
        cid = target[4:].strip()
        evidence.images.append(
            ImageEvidence(
                evidence_ref=f"{self._evidence_ref}:cid:{len(evidence.images)}",
                target=f"cid:{cid}",
                target_raw=target,
                is_remote=False,
                is_inline=True,
                content_id=cid,
            )
        )


def _extract_from_html(html: str, state: _ParseState, evidence_ref: str) -> None:
    """Run the inert HTML structure parser; never render or fetch (T-05)."""
    parser = _LinkHTMLParser(state, evidence_ref)
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed HTML stays bounded
        state.note(IncompleteReason.MALFORMED_MIME, evidence_ref)
    # Also sweep visible text for bare URLs the markup did not wrap.
    _extract_urls_from_text(html, state, evidence_ref)


def _extract_urls_from_text(text: str, state: _ParseState, evidence_ref: str) -> None:
    """Capture bare URLs from plain text; display text == target here."""
    assert state.evidence is not None
    cap = state.limits.max_link_text
    for match in _URL_IN_TEXT_RE.finditer(text):
        if not state.can_add_component():
            state.note(IncompleteReason.PART_COUNT_EXCEEDED, evidence_ref)
            break
        target = match.group(1)
        state.evidence.links.append(
            LinkEvidence(
                evidence_ref=f"{evidence_ref}:texturl:{len(state.evidence.links)}",
                target=_normalize(target)[:cap],
                target_raw=target[:cap],
                display_text=_normalize(target)[:cap],
                source="text",
                attribute=None,
            )
        )


# =============================================================================
# small pure helpers
# =============================================================================


def _normalize(value: str) -> str:
    """NFC-normalize a string for display/comparison (R-003.1 provenance).

    The caller retains the raw form separately; this only produces the
    normalized view. Control characters are not stripped here so evidence stays
    faithful; downstream inspection decides on display-safety.
    """
    if not value:
        return ""
    return unicodedata.normalize("NFC", value)


def _is_remote_resource(target: str) -> bool:
    """Return ``True`` for an external (http/https/protocol-relative) target."""
    lowered = target.strip().lower()
    return (
        lowered.startswith("http://")
        or lowered.startswith("https://")
        or lowered.startswith("//")
        or lowered.startswith("ftp://")
    )


def _lower_ext(filename: str) -> str:
    """Return the lowercase final extension of a filename (no dot)."""
    base = filename.strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." not in base:
        return ""
    return base.rsplit(".", 1)[-1].lower()


def _has_double_extension(filename: str) -> bool:
    """Flag suspicious double extensions like ``invoice.pdf.exe`` (§5.2)."""
    base = filename.strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    parts = [segment for segment in base.split(".") if segment]
    if len(parts) < 3:
        return False
    executable = {
        "exe",
        "scr",
        "com",
        "bat",
        "cmd",
        "js",
        "vbs",
        "jar",
        "ps1",
        "msi",
        "lnk",
        "pif",
    }
    return parts[-1].lower() in executable


def _sniff_magic(data: bytes) -> str | None:
    """Return a sniffed content-type label from magic bytes, or ``None``.

    Reads only the first few bytes; performs no execution and loads nothing.
    """
    head = data[:16]
    for prefix, label in _MAGIC_PREFIXES:
        if head.startswith(prefix):
            return label
    return None


#: Sniffed types that denote an executable/code container. If the magic bytes
#: sniff as one of these but the claimed type is something else, that is always
#: a mismatch worth flagging, regardless of a shared top-level type (§5.2
#: double-extension / spoofed-document evidence).
_EXECUTABLE_SNIFFED_TYPES: frozenset[str] = frozenset(
    {"application/x-dosexec", "application/x-elf"}
)


def _types_compatible(sniffed: str, claimed: str) -> bool:
    """Loose compatibility check so benign type aliases are not flagged.

    Treats a shared top-level type (e.g. ``image/png`` vs ``image/jpeg`` both
    ``image/*``) as compatible to reduce false positives. The one exception:
    executable magic (``MZ``/``ELF``) is never compatible with a non-matching
    claimed type, so a document that is actually an executable (e.g. declared
    ``application/pdf`` but sniffed ``application/x-dosexec``) is flagged even
    though both share the ``application`` top-level type.
    """
    if sniffed in _EXECUTABLE_SNIFFED_TYPES and claimed != sniffed:
        return False
    sniffed_top = sniffed.split("/", 1)[0]
    claimed_top = claimed.split("/", 1)[0]
    return sniffed_top == claimed_top


# =============================================================================
# manifest helper (metadata-only persisted surface)
# =============================================================================


def evidence_fidelity_for(evidence: ParsedEvidence) -> EvidenceFidelity:
    """Classify how faithful the captured evidence is (design §4, I-05).

    We parsed real RFC822 bytes, so fidelity is ``ORIGINAL_RFC822`` when the
    parse was complete and nothing was truncated. If a source-bytes cap
    truncated the input, the capture is lossy and must not claim original
    fidelity, so we drop to ``SELECTED_HEADERS_AND_BODY`` (I-05: a lossy
    summary is never presented as original signed MIME).
    """
    source_truncated = any(
        note.reason is IncompleteReason.SOURCE_BYTES_EXCEEDED
        for note in evidence.incomplete_notes
    )
    if source_truncated:
        return EvidenceFidelity.SELECTED_HEADERS_AND_BODY
    return EvidenceFidelity.ORIGINAL_RFC822


__all__ = [
    "DEFAULT_LIMITS",
    "PARSER_VERSION",
    "AttachmentDescriptor",
    "ImageEvidence",
    "IncompleteNote",
    "IncompleteReason",
    "LinkEvidence",
    "ParsedEvidence",
    "ParserLimits",
    "TextPart",
    "evidence_fidelity_for",
    "parse_mime_evidence",
]
