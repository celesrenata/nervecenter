"""Offline, opt-in Google Takeout MBOX / EML import (NerveShield task 5.1).

Library only: nothing in the pipeline, API or cluster calls this yet. It reads
local files the user explicitly selected and hands each message to an injected
sink. It makes no network calls and executes nothing (R-014, R-017).

Safeguards:

* **Explicit account selection.** Every source names its ``account_scope``.
  A source whose account is not in ``selected_accounts`` is never opened.
* **Archive policy.** Paths must resolve inside an allowed root with no
  symlink anywhere in the path; files are opened with ``O_NOFOLLOW``. Only
  ``.mbox`` and ``.eml`` are read. Compressed archives (zip, tar, gz, ...) are
  refused: Takeout must be extracted by the user first, so no extraction code
  runs here (no zip bombs, no path traversal).
* **Bounded streaming.** MBOX is read line by line with a line cap, a
  per-message byte cap, a message-count cap and a file-size cap. Oversized
  messages are quarantined, never buffered whole. Each message is parsed with
  the existing bounded MIME parser.
* **Manifests, no secrets.** The manifest holds counts and enum codes only:
  no paths, subjects, addresses, Message-IDs or labels. Records keep account
  provenance; identical bytes in two accounts stay two records.
* **Dry run by default.** ``dry_run=True`` performs the full read and parse
  but never calls the sink.

Not here yet (later tasks): checkpoints/resume and error quarantine storage
(5.2), Gmail label normalisation (5.3; raw ``X-Gmail-Labels`` is carried on
the record, bounded, and never logged), storage and queueing.
"""

from __future__ import annotations

import os
import re
import stat
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Protocol

from backend.mail_security.acquisition import fidelity_for_mode
from backend.mail_security.identifiers import logical_source_key
from backend.mail_security.models import AcquisitionMode, EvidenceFidelity
from backend.mail_security.parser import IncompleteReason, ParserLimits, parse_mime_evidence

IMPORT_PROVIDER = "takeout"
MAX_LABELS_CHARS = 4096
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")
_COMPRESSED_SUFFIXES = frozenset(
    {".zip", ".tgz", ".gz", ".tar", ".bz2", ".xz", ".7z", ".rar", ".zst"}
)
_MBOXRD_QUOTED = re.compile(rb"^>+From ")
_FATAL_PARSE = frozenset({IncompleteReason.MALFORMED_MIME, IncompleteReason.EMPTY_SOURCE})


class SourceKind(StrEnum):
    MBOX = "MBOX"
    EML = "EML"  # a single .eml file or a directory of .eml files


class SourceStatus(StrEnum):
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"  # account not selected; never opened
    REFUSED = "REFUSED"  # archive policy; never read
    LIMIT_REACHED = "LIMIT_REACHED"  # stopped at a configured cap
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"  # I/O error while reading


class IssueCode(StrEnum):
    """Log-safe reasons. Never carry paths or content."""

    ACCOUNT_NOT_SELECTED = "ACCOUNT_NOT_SELECTED"
    ACCOUNT_INVALID = "ACCOUNT_INVALID"
    PATH_OUTSIDE_ROOTS = "PATH_OUTSIDE_ROOTS"
    PATH_SYMLINK = "PATH_SYMLINK"
    PATH_MISSING = "PATH_MISSING"
    PATH_NOT_REGULAR = "PATH_NOT_REGULAR"
    COMPRESSED_ARCHIVE_UNSUPPORTED = "COMPRESSED_ARCHIVE_UNSUPPORTED"
    SUFFIX_NOT_ALLOWED = "SUFFIX_NOT_ALLOWED"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    MESSAGE_COUNT_EXCEEDED = "MESSAGE_COUNT_EXCEEDED"
    EML_FILE_COUNT_EXCEEDED = "EML_FILE_COUNT_EXCEEDED"
    READ_ERROR = "READ_ERROR"
    CHECKPOINT_MISMATCH = "CHECKPOINT_MISMATCH"
    # per-message quarantine
    MESSAGE_TOO_LARGE = "MESSAGE_TOO_LARGE"
    PARSE_FAILED = "PARSE_FAILED"
    LEADING_DATA_BEFORE_FIRST_SEPARATOR = "LEADING_DATA_BEFORE_FIRST_SEPARATOR"
    EML_ENTRY_SKIPPED = "EML_ENTRY_SKIPPED"


@dataclass(frozen=True)
class ImportPolicy:
    """Archive policy and resource caps. ``allowed_roots`` is required."""

    allowed_roots: tuple[Path, ...]
    max_file_bytes: int = 64 * 1024**3
    max_message_bytes: int = 25 * 1024 * 1024
    max_line_bytes: int = 1024 * 1024
    max_messages_per_source: int = 2_000_000
    max_eml_files: int = 200_000
    max_eml_depth: int = 8
    max_header_scan_bytes: int = 256 * 1024
    parser_limits: ParserLimits = field(default_factory=ParserLimits)

    def __post_init__(self) -> None:
        if not self.allowed_roots:
            raise ValueError("allowed_roots must name at least one directory")
        for name in (
            "max_file_bytes",
            "max_message_bytes",
            "max_line_bytes",
            "max_messages_per_source",
            "max_eml_files",
            "max_eml_depth",
            "max_header_scan_bytes",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True)
class ImportSource:
    account_scope: str
    path: Path | str
    kind: SourceKind


@dataclass(frozen=True)
class ImportRequest:
    selected_accounts: frozenset[str]
    sources: tuple[ImportSource, ...]
    dry_run: bool = True


@dataclass(frozen=True)
class ImportRecord:
    """One message handed to the sink. Holds raw bytes: keep it in process."""

    account_scope: str
    source_index: int
    logical_key: str
    sha256: str
    raw: bytes = field(repr=False)
    offset: int  # MBOX byte offset of the From_ line, or EML entry ordinal
    acquisition_mode: AcquisitionMode
    evidence_fidelity: EvidenceFidelity
    inspection_complete: bool
    gmail_labels_raw: str | None = field(default=None, repr=False)


class RecordSink(Protocol):
    def __call__(self, record: ImportRecord) -> None: ...


@dataclass
class SourceManifest:
    source_index: int
    account_scope: str
    kind: SourceKind
    status: SourceStatus = SourceStatus.COMPLETED
    status_code: IssueCode | None = None
    bytes_read: int = 0
    messages_seen: int = 0
    records: int = 0
    duplicates_in_account: int = 0
    inspection_incomplete: int = 0
    with_gmail_labels: int = 0
    without_message_id: int = 0
    quarantined: Counter[str] = field(default_factory=Counter)

    def to_dict(self) -> dict[str, object]:
        return {
            "source_index": self.source_index,
            "account_scope": self.account_scope,
            "kind": self.kind.value,
            "status": self.status.value,
            "status_code": self.status_code.value if self.status_code else None,
            "bytes_read": self.bytes_read,
            "messages_seen": self.messages_seen,
            "records": self.records,
            "duplicates_in_account": self.duplicates_in_account,
            "inspection_incomplete": self.inspection_incomplete,
            "with_gmail_labels": self.with_gmail_labels,
            "without_message_id": self.without_message_id,
            "quarantined": dict(sorted(self.quarantined.items())),
        }


@dataclass
class ImportManifest:
    dry_run: bool
    sources: list[SourceManifest] = field(default_factory=list)
    #: Per account: records whose bytes also appear in another selected account.
    cross_account_duplicates: Counter[str] = field(default_factory=Counter)

    def account_totals(self) -> dict[str, dict[str, int]]:
        totals: dict[str, Counter[str]] = {}
        for src in self.sources:
            t = totals.setdefault(src.account_scope, Counter())
            t["records"] += src.records
            t["duplicates_in_account"] += src.duplicates_in_account
            t["quarantined"] += sum(src.quarantined.values())
        for account, count in self.cross_account_duplicates.items():
            totals.setdefault(account, Counter())["cross_account_duplicates"] = count
        return {a: dict(sorted(c.items())) for a, c in sorted(totals.items())}

    def to_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "sources": [s.to_dict() for s in self.sources],
            "accounts": self.account_totals(),
        }


class _Refused(Exception):
    def __init__(self, code: IssueCode) -> None:
        super().__init__(code.value)
        self.code = code


class _Cancelled(Exception):
    pass


# -- archive policy ----------------------------------------------------------------------


def _check_path(raw_path: Path | str, policy: ImportPolicy) -> Path:
    """Resolve and validate a source path. Raises :class:`_Refused`.

    An allowed root may itself sit behind a symlink (macOS ``/var`` ->
    ``/private/var``, a linked home directory); it is resolved once. Below the
    root, no path component may be a symlink, so nothing can point outside it.
    Returns the real path.
    """
    path = Path(os.path.abspath(os.fspath(raw_path)))
    if not os.path.lexists(path):
        raise _Refused(IssueCode.PATH_MISSING)
    for root in policy.allowed_roots:
        root_abs = Path(os.path.abspath(os.fspath(root)))
        root_real = Path(os.path.realpath(root_abs))
        for base in (root_abs, root_real):
            if path != base and base not in path.parents:
                continue
            current = base
            for part in path.relative_to(base).parts:
                current = current / part
                if os.path.islink(current):
                    raise _Refused(IssueCode.PATH_SYMLINK)
            real = Path(os.path.realpath(path))
            if real != root_real and root_real not in real.parents:  # pragma: no cover
                raise _Refused(IssueCode.PATH_SYMLINK)
            return real
    raise _Refused(IssueCode.PATH_OUTSIDE_ROOTS)


def _check_suffix(path: Path, allowed: str) -> None:
    suffixes = {s.lower() for s in path.suffixes}
    if suffixes & _COMPRESSED_SUFFIXES:
        raise _Refused(IssueCode.COMPRESSED_ARCHIVE_UNSUPPORTED)
    if path.suffix.lower() != allowed:
        raise _Refused(IssueCode.SUFFIX_NOT_ALLOWED)


def _open_regular(path: Path, policy: ImportPolicy) -> BinaryIO:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _Refused(IssueCode.PATH_NOT_REGULAR)
        if info.st_size > policy.max_file_bytes:
            raise _Refused(IssueCode.FILE_TOO_LARGE)
        return os.fdopen(fd, "rb", buffering=1024 * 1024)
    except BaseException:
        os.close(fd)
        raise


# -- header scan -------------------------------------------------------------------------


def _scan_headers(raw: bytes, limit: int) -> tuple[str | None, str | None]:
    """Return (Message-ID, X-Gmail-Labels) from the bounded header block."""
    head = raw[:limit]
    for sep in (b"\r\n\r\n", b"\n\n"):
        cut = head.find(sep)
        if cut != -1:
            head = head[:cut]
            break
    unfolded: list[str] = []
    for line in head.decode("latin-1").splitlines():
        if line[:1] in (" ", "\t") and unfolded:
            unfolded[-1] += " " + line.strip()
        else:
            unfolded.append(line)
    message_id: str | None = None
    labels: str | None = None
    for line in unfolded:
        name, sep, value = line.partition(":")
        if not sep:
            continue
        key = name.strip().lower()
        if key == "message-id" and message_id is None:
            message_id = value.strip()[:998] or None
        elif key == "x-gmail-labels" and labels is None:
            labels = value.strip()[:MAX_LABELS_CHARS] or None
    return message_id, labels


# -- streaming readers -------------------------------------------------------------------


@dataclass
class _RawMessage:
    offset: int
    data: bytes | None  # None means over the per-message cap (quarantine)
    #: Where a resume must restart after this message: MBOX byte offset of the
    #: next ``From `` line (or EOF), or the EML ordinal of this entry.
    next_position: int = 0
    #: Quarantine code when ``data`` is None (default: over the size cap).
    issue: IssueCode = IssueCode.MESSAGE_TOO_LARGE


def _iter_mbox(
    handle: BinaryIO, policy: ImportPolicy, manifest: SourceManifest, start_offset: int = 0
) -> Iterator[_RawMessage]:
    """Stream messages from an mbox. mboxrd unquoting; one trailing blank line dropped.

    A ``From `` line separates messages only at file start or after a blank
    line, which keeps unquoted ``From `` body lines (mboxo) inside the message.
    """
    offset = 0
    if start_offset:
        handle.seek(start_offset)
        offset = start_offset
        peek = handle.read(5)
        handle.seek(start_offset)
        if peek and peek != b"From ":  # a resume must land on a separator (or EOF)
            raise _Refused(IssueCode.CHECKPOINT_MISMATCH)
    current: bytearray | None = None
    current_offset = 0
    over = False
    at_line_start = True
    prev_blank = True
    leading_reported = False
    while True:
        line = handle.readline(policy.max_line_bytes)
        if not line:
            break
        complete = line.endswith(b"\n")
        # The first separator needs no preceding blank line (it may follow junk).
        if at_line_start and line.startswith(b"From ") and (prev_blank or current is None):
            if current is not None:
                # Counted when handed over, so a checkpoint never includes a
                # message that was read but not yet processed.
                manifest.messages_seen += 1
                yield _finish(current, current_offset, over, offset)
            if manifest.messages_seen + 1 > policy.max_messages_per_source:
                raise _Refused(IssueCode.MESSAGE_COUNT_EXCEEDED)
            current, current_offset, over = bytearray(), offset, False
        elif current is None:
            if not leading_reported:
                manifest.quarantined[IssueCode.LEADING_DATA_BEFORE_FIRST_SEPARATOR] += 1
                leading_reported = True
        else:
            data = line[1:] if at_line_start and _MBOXRD_QUOTED.match(line) else line
            if not over:
                if len(current) + len(data) > policy.max_message_bytes:
                    over = True
                    current.clear()
                else:
                    current += data
        prev_blank = at_line_start and complete and line in (b"\n", b"\r\n")
        at_line_start = complete
        offset += len(line)
        manifest.bytes_read = offset
    if current is not None:
        manifest.messages_seen += 1
        yield _finish(current, current_offset, over, offset)


def _finish(buf: bytearray, offset: int, over: bool, next_position: int) -> _RawMessage:
    if over:
        return _RawMessage(offset, None, next_position)
    data = bytes(buf)
    for tail in (b"\r\n", b"\n"):  # the separator blank line belongs to mbox, not the message
        if data.endswith(tail * 2):
            data = data[: -len(tail)]
            break
    return _RawMessage(offset, data, next_position)


def _iter_eml(
    root: Path, policy: ImportPolicy, manifest: SourceManifest, skip: int = 0
) -> Iterator[_RawMessage]:
    """Yield .eml files: one file, or a directory walked without following links."""
    # Walk-time issues are held until the next entry, then counted only if that
    # entry lies after ``skip``; a resume never counts the same issue twice.
    pending: Counter[str] = Counter()
    if root.is_file():
        _check_suffix(root, ".eml")
        entries: Iterator[Path] = iter([root])
    else:
        entries = _walk_eml(root, policy, pending)
    ordinal = 0
    for entry in entries:
        ordinal += 1
        if ordinal > policy.max_eml_files:
            raise _Refused(IssueCode.EML_FILE_COUNT_EXCEEDED)
        if ordinal > skip:
            manifest.quarantined.update(pending)
        pending.clear()
        if ordinal <= skip:  # already processed before a checkpoint
            continue
        manifest.messages_seen += 1
        try:
            fd = os.open(entry, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            yield _RawMessage(ordinal, None, ordinal, IssueCode.EML_ENTRY_SKIPPED)
            continue
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                yield _RawMessage(ordinal, None, ordinal, IssueCode.EML_ENTRY_SKIPPED)
                continue
            if info.st_size > policy.max_message_bytes:
                yield _RawMessage(ordinal, None, ordinal)
                continue
            data = handle.read(policy.max_message_bytes + 1)
        manifest.bytes_read += len(data)
        fits = len(data) <= policy.max_message_bytes
        yield _RawMessage(ordinal, data if fits else None, ordinal)
    if ordinal > skip or skip == 0:
        manifest.quarantined.update(pending)


def _walk_eml(root: Path, policy: ImportPolicy, issues: Counter[str]) -> Iterator[Path]:
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        with os.scandir(directory) as it:
            items = sorted(it, key=lambda e: e.name)
        for item in items:
            if item.is_symlink():
                issues[IssueCode.EML_ENTRY_SKIPPED] += 1
            elif item.is_dir(follow_symlinks=False):
                if depth + 1 <= policy.max_eml_depth:
                    stack.append((Path(item.path), depth + 1))
                else:
                    issues[IssueCode.EML_ENTRY_SKIPPED] += 1
            elif item.is_file(follow_symlinks=False):
                if Path(item.name).suffix.lower() == ".eml":
                    yield Path(item.path)
                # other files (e.g. Takeout html/json) are ignored silently


# -- driver ------------------------------------------------------------------------------


def run_import(
    request: ImportRequest,
    policy: ImportPolicy,
    *,
    sink: RecordSink | None = None,
    cancel: Callable[[], bool] | None = None,
) -> ImportManifest:
    """Read every selected source and return a manifest. Never raises for data issues."""
    if not request.dry_run and sink is None:
        raise ValueError("a sink is required when dry_run is False")
    if not all(isinstance(a, str) and _ACCOUNT_RE.fullmatch(a) for a in request.selected_accounts):
        raise ValueError("selected_accounts contains an invalid account scope")
    manifest = ImportManifest(dry_run=request.dry_run)
    seen_by_account: dict[str, set[str]] = {}
    emitted: list[tuple[str, str]] = []  # (account, sha256) for cross-account counting
    for index, source in enumerate(request.sources):
        src = SourceManifest(index, str(source.account_scope), SourceKind(source.kind))
        manifest.sources.append(src)
        if not isinstance(source.account_scope, str) or not _ACCOUNT_RE.fullmatch(
            source.account_scope
        ):
            src.status, src.status_code = SourceStatus.REFUSED, IssueCode.ACCOUNT_INVALID
            continue
        if source.account_scope not in request.selected_accounts:
            src.status, src.status_code = SourceStatus.SKIPPED, IssueCode.ACCOUNT_NOT_SELECTED
            continue
        seen = seen_by_account.setdefault(source.account_scope, set())
        try:
            path = _check_path(source.path, policy)
            if src.kind is SourceKind.MBOX:
                _check_suffix(path, ".mbox")
                with _open_regular(path, policy) as handle:
                    messages = _iter_mbox(handle, policy, src)
                    _consume(
                        messages, source, index, src, policy, seen, emitted, request, sink, cancel
                    )
            else:
                if not path.is_file() and not path.is_dir():
                    raise _Refused(IssueCode.PATH_NOT_REGULAR)
                messages = _iter_eml(path, policy, src)
                _consume(messages, source, index, src, policy, seen, emitted, request, sink, cancel)
        except _Refused as exc:
            limit = exc.code in (
                IssueCode.MESSAGE_COUNT_EXCEEDED,
                IssueCode.EML_FILE_COUNT_EXCEEDED,
            )
            src.status = SourceStatus.LIMIT_REACHED if limit else SourceStatus.REFUSED
            src.status_code = exc.code
        except _Cancelled:
            src.status = SourceStatus.CANCELLED
            break
        except OSError:
            src.status, src.status_code = SourceStatus.FAILED, IssueCode.READ_ERROR
    accounts_by_sha: dict[str, set[str]] = {}
    for account, sha in emitted:
        accounts_by_sha.setdefault(sha, set()).add(account)
    for account, sha in emitted:
        if len(accounts_by_sha[sha]) > 1:
            manifest.cross_account_duplicates[account] += 1
    return manifest


def _accept(
    message: _RawMessage,
    account_scope: str,
    index: int,
    src: SourceManifest,
    policy: ImportPolicy,
    seen: set[str],
) -> ImportRecord | None:
    """Classify one message and update ``src``. Returns a record, or None if not kept.

    Shared by :func:`run_import` and the resumable job (``takeout_job``) so both
    apply identical quarantine, dedup and labelling rules.
    """
    if message.data is None:
        src.quarantined[message.issue] += 1
        return None
    raw = message.data
    if not raw.strip():  # separator with no message behind it
        src.quarantined[IssueCode.PARSE_FAILED] += 1
        return None
    evidence = parse_mime_evidence(raw, limits=policy.parser_limits)
    reasons = {note.reason for note in evidence.incomplete_notes}
    if reasons & _FATAL_PARSE:
        src.quarantined[IssueCode.PARSE_FAILED] += 1
        return None
    sha = evidence.source_sha256
    if sha in seen:
        src.duplicates_in_account += 1
        return None
    seen.add(sha)
    message_id, labels = _scan_headers(raw, policy.max_header_scan_bytes)
    if message_id is None:
        src.without_message_id += 1
    if labels is not None:
        src.with_gmail_labels += 1
    if not evidence.inspection_complete:
        src.inspection_incomplete += 1
    mode = AcquisitionMode.TAKEOUT_MBOX if src.kind is SourceKind.MBOX else AcquisitionMode.EML
    src.records += 1
    return ImportRecord(
        account_scope=account_scope,
        source_index=index,
        logical_key=logical_source_key(
            account_scope,
            provider=IMPORT_PROVIDER,
            provider_uid=None if message_id else f"sha256:{sha}",
            rfc_message_id=message_id,
        ),
        sha256=sha,
        raw=raw,
        offset=message.offset,
        acquisition_mode=mode,
        evidence_fidelity=fidelity_for_mode(mode),
        inspection_complete=evidence.inspection_complete,
        gmail_labels_raw=labels,
    )


def _consume(
    messages: Iterator[_RawMessage],
    source: ImportSource,
    index: int,
    src: SourceManifest,
    policy: ImportPolicy,
    seen: set[str],
    emitted: list[tuple[str, str]],
    request: ImportRequest,
    sink: RecordSink | None,
    cancel: Callable[[], bool] | None,
) -> None:
    if cancel is not None and cancel():
        raise _Cancelled
    for message in messages:
        record = _accept(message, source.account_scope, index, src, policy, seen)
        if record is not None:
            emitted.append((source.account_scope, record.sha256))
            if not request.dry_run:
                assert sink is not None  # noqa: S101 - checked in run_import
                sink(record)
        # Checked after a message is fully handled: cancel never splits one.
        if cancel is not None and cancel():
            raise _Cancelled


__all__ = [
    "IMPORT_PROVIDER",
    "ImportManifest",
    "ImportPolicy",
    "ImportRecord",
    "ImportRequest",
    "ImportSource",
    "IssueCode",
    "RecordSink",
    "SourceKind",
    "SourceManifest",
    "SourceStatus",
    "run_import",
]
