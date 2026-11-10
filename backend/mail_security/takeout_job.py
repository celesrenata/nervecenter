"""Resumable, idempotent Takeout import job (NerveShield task 5.2).

Library only: nothing in the pipeline, API or cluster calls this yet. It drives
the 5.1 readers (:mod:`takeout_import`) with checkpoints so an import can be
stopped, crash, and resume without losing or duplicating work (R-002, R-014,
R-018).

Guarantees:

* **Checkpoint/resume.** The whole job state (per-source position and
  counters, the per-account dedup set, quarantine list) is saved atomically
  every ``checkpoint_every`` messages, at the end of each source and on
  cancel. A resume restores exactly that state and replays at most the
  messages since the last checkpoint.
* **Idempotent writes.** Raw evidence is keyed by ``account/sha256`` and only
  written when absent; evidence entries carry a stable idempotency key. A
  crash between a write and a checkpoint replays the write, never duplicates.
  ``entry_sink`` MUST upsert by ``EvidenceEntry.idempotency_key``.
* **Fail closed on drift.** A resume is refused if the request, the file or
  directory listing, or the byte at the saved offset changed, or if the
  saved state is corrupt or from another version. Nothing is written then.
* **Honest fidelity.** Raw bytes are kept only when :class:`RawCapturePolicy`
  allows it AND a store is supplied (off by default: the cluster object store
  is plain HTTP and shared, an open owner decision). Otherwise the entry says
  ``METADATA_ONLY`` with ``RAW_NOT_RETAINED``; the source fidelity
  (``ORIGINAL_RFC822``) is recorded separately and never claimed for storage.
* **Quarantine without content.** Malformed, oversized and skipped entries are
  recorded as (source index, position, code) only, bounded in count.

The checkpoint file holds account scopes, per-message sha256 digests and
counters. It never holds paths, subjects, addresses, Message-IDs or labels;
paths only enter a digest. :class:`FileCheckpointStore` writes it ``0600``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from backend.mail_security.models import EvidenceFidelity
from backend.mail_security.takeout_import import (
    ImportManifest,
    ImportPolicy,
    ImportRecord,
    ImportRequest,
    IssueCode,
    SourceKind,
    SourceManifest,
    SourceStatus,
    _accept,
    _check_path,
    _check_suffix,
    _iter_eml,
    _iter_mbox,
    _open_regular,
    _RawMessage,
    _Refused,
    _walk_eml,
)
from backend.storage.contracts import ObjectStore

JOB_STATE_VERSION = 1
RAW_NOT_RETAINED = "RAW_NOT_RETAINED"
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")
_HEAD_FINGERPRINT_BYTES = 64 * 1024


class JobStatus(StrEnum):
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    REFUSED = "REFUSED"  # resume refused; nothing was written


class JobRefusal(StrEnum):
    JOB_MISMATCH = "JOB_MISMATCH"  # request or caps differ from the saved job
    SOURCE_CHANGED = "SOURCE_CHANGED"  # file bytes / listing differ from the saved job
    STATE_CORRUPT = "STATE_CORRUPT"
    STATE_VERSION = "STATE_VERSION"


# -- checkpoint stores -------------------------------------------------------------------


class CheckpointStore(Protocol):
    async def load(self, job_id: str) -> dict[str, Any] | None: ...

    async def save(self, job_id: str, state: dict[str, Any]) -> None: ...


def _require_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
        raise ValueError("job_id must match [A-Za-z0-9_-]{1,64}")
    return job_id


class InMemoryCheckpointStore:
    """Keeps a deep copy per save, so callers cannot mutate saved state."""

    def __init__(self) -> None:
        self.states: dict[str, str] = {}
        self.saves = 0

    async def load(self, job_id: str) -> dict[str, Any] | None:
        raw = self.states.get(_require_job_id(job_id))
        return None if raw is None else json.loads(raw)

    async def save(self, job_id: str, state: dict[str, Any]) -> None:
        self.states[_require_job_id(job_id)] = json.dumps(state, sort_keys=True)
        self.saves += 1


class FileCheckpointStore:
    """Atomic JSON checkpoints: temp file in the same dir, fsync, rename, fsync dir."""

    def __init__(self, directory: Path | str) -> None:
        self._dir = Path(directory)

    def _path(self, job_id: str) -> Path:
        return self._dir / f"{_require_job_id(job_id)}.json"

    async def load(self, job_id: str) -> dict[str, Any] | None:
        path = self._path(job_id)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        return json.loads(data)

    async def save(self, job_id: str, state: dict[str, Any]) -> None:
        target = self._path(job_id)
        self._dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state, sort_keys=True).encode()
        fd, tmp = tempfile.mkstemp(prefix=f".{job_id}.", suffix=".tmp", dir=self._dir)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        dir_fd = os.open(self._dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


# -- evidence ----------------------------------------------------------------------------


@dataclass(frozen=True)
class RawCapturePolicy:
    """Raw bytes are stored only when ``allowed`` and a store is supplied."""

    allowed: bool = False
    key_prefix: str = "mail-security/takeout"


@dataclass(frozen=True)
class EvidenceEntry:
    """Evidence-manifest row for one imported message (design §4.1). No content."""

    idempotency_key: str
    account_scope: str
    logical_key: str
    sha256: str
    source_index: int
    position: int
    acquisition_mode: str
    source_fidelity: EvidenceFidelity
    stored_fidelity: EvidenceFidelity
    raw_content_ref: str | None
    missing_parts: tuple[str, ...]
    inspection_complete: bool
    gmail_labels_raw: str | None = field(default=None, repr=False)


EntrySink = Callable[[EvidenceEntry], Awaitable[None]]


@dataclass
class JobResult:
    status: JobStatus
    manifest: ImportManifest
    refusal: JobRefusal | None = None
    quarantine: list[dict[str, Any]] = field(default_factory=list)
    quarantine_dropped: int = 0


# -- job ---------------------------------------------------------------------------------


class _Cancel(Exception):
    pass


class ImportJob:
    def __init__(
        self,
        job_id: str,
        request: ImportRequest,
        policy: ImportPolicy,
        checkpoints: CheckpointStore,
        *,
        entry_sink: EntrySink | None = None,
        raw_store: ObjectStore | None = None,
        raw_policy: RawCapturePolicy | None = None,
        checkpoint_every: int = 100,
        max_quarantine_entries: int = 10_000,
    ) -> None:
        self._job_id = _require_job_id(job_id)
        if not request.dry_run and entry_sink is None:
            raise ValueError("an entry_sink is required when dry_run is False")
        if not all(
            isinstance(a, str) and _ACCOUNT_RE.fullmatch(a) for a in request.selected_accounts
        ):
            raise ValueError("selected_accounts contains an invalid account scope")
        if checkpoint_every < 1 or max_quarantine_entries < 0:
            raise ValueError("checkpoint_every must be >= 1 and max_quarantine_entries >= 0")
        self._request = request
        self._policy = policy
        self._checkpoints = checkpoints
        self._sink = entry_sink
        self._raw_store = raw_store
        self._raw_policy = raw_policy or RawCapturePolicy()
        self._every = checkpoint_every
        self._max_quarantine = max_quarantine_entries
        self._fingerprint = _request_fingerprint(request, policy)

    @property
    def raw_capture_active(self) -> bool:
        return (
            self._raw_policy.allowed and self._raw_store is not None and not self._request.dry_run
        )

    async def run(self, cancel: Callable[[], bool] | None = None) -> JobResult:
        try:
            loaded = await self._checkpoints.load(self._job_id)
        except (ValueError, OSError):
            return self._refused(JobRefusal.STATE_CORRUPT)
        if loaded is None:
            state = self._new_state()
        else:
            problem = self._validate(loaded)
            if problem is not None:
                return self._refused(problem)
            state = loaded
        if state["done"]:
            return self._result(state, JobStatus.COMPLETED)
        try:
            await self._run_sources(state, cancel)
        except _Cancel:
            await self._save(state)
            return self._result(state, JobStatus.CANCELLED)
        except _SourceChanged:
            return self._result(state, JobStatus.REFUSED, JobRefusal.SOURCE_CHANGED)
        state["done"] = True
        await self._save(state)
        return self._result(state, JobStatus.COMPLETED)

    # -- state ---------------------------------------------------------------------------

    def _new_state(self) -> dict[str, Any]:
        return {
            "version": JOB_STATE_VERSION,
            "job_id": self._job_id,
            "request_fingerprint": self._fingerprint,
            "dry_run": self._request.dry_run,
            "current": 0,
            "done": False,
            "sources": [],
            "seen": {},
            "quarantine": [],
            "quarantine_dropped": 0,
        }

    def _validate(self, state: Any) -> JobRefusal | None:
        if not isinstance(state, dict):
            return JobRefusal.STATE_CORRUPT
        if state.get("version") != JOB_STATE_VERSION:
            return JobRefusal.STATE_VERSION
        required = {
            "job_id": str,
            "request_fingerprint": str,
            "current": int,
            "done": bool,
            "sources": list,
            "seen": dict,
            "quarantine": list,
            "quarantine_dropped": int,
        }
        for key, kind in required.items():
            if not isinstance(state.get(key), kind):
                return JobRefusal.STATE_CORRUPT
        if state["job_id"] != self._job_id:
            return JobRefusal.STATE_CORRUPT
        if state["request_fingerprint"] != self._fingerprint:
            return JobRefusal.JOB_MISMATCH
        if not 0 <= state["current"] <= len(self._request.sources):
            return JobRefusal.STATE_CORRUPT
        if len(state["sources"]) > len(self._request.sources):
            return JobRefusal.STATE_CORRUPT
        try:
            for item in state["sources"]:
                _source_from_dict(item)
                if not isinstance(item.get("position"), int) or item["position"] < 0:
                    return JobRefusal.STATE_CORRUPT
            for account, digests in state["seen"].items():
                if not _ACCOUNT_RE.fullmatch(account) or not isinstance(digests, list):
                    return JobRefusal.STATE_CORRUPT
        except (KeyError, TypeError, ValueError):
            return JobRefusal.STATE_CORRUPT
        return None

    async def _save(self, state: dict[str, Any]) -> None:
        await self._checkpoints.save(self._job_id, state)

    # -- sources -------------------------------------------------------------------------

    async def _run_sources(self, state: dict[str, Any], cancel: Callable[[], bool] | None) -> None:
        sources = self._request.sources
        seen_sets = {a: set(d) for a, d in state["seen"].items()}
        while state["current"] < len(sources):
            index = state["current"]
            source = sources[index]
            if len(state["sources"]) <= index:
                entry = SourceManifest(index, str(source.account_scope), SourceKind(source.kind))
                state["sources"].append({**entry.to_dict(), "position": 0, "fingerprint": None})
            slot = state["sources"][index]
            src = _source_from_dict(slot)
            if slot["status"] == SourceStatus.COMPLETED.value and slot.get("finished"):
                state["current"] += 1
                continue
            await self._run_one(index, source, src, slot, state, seen_sets, cancel)
            slot.update(src.to_dict())
            slot["finished"] = True
            state["seen"] = {a: sorted(d) for a, d in seen_sets.items()}
            state["current"] += 1
            await self._save(state)

    async def _run_one(
        self,
        index: int,
        source: Any,
        src: SourceManifest,
        slot: dict[str, Any],
        state: dict[str, Any],
        seen_sets: dict[str, set[str]],
        cancel: Callable[[], bool] | None,
    ) -> None:
        account = source.account_scope
        if not isinstance(account, str) or not _ACCOUNT_RE.fullmatch(account):
            src.status, src.status_code = SourceStatus.REFUSED, IssueCode.ACCOUNT_INVALID
            return
        if account not in self._request.selected_accounts:
            src.status, src.status_code = SourceStatus.SKIPPED, IssueCode.ACCOUNT_NOT_SELECTED
            return
        seen = seen_sets.setdefault(account, set())
        try:
            path = _check_path(source.path, self._policy)
            if src.kind is SourceKind.MBOX:
                _check_suffix(path, ".mbox")
                with _open_regular(path, self._policy) as handle:
                    fingerprint = _mbox_fingerprint(handle)
                    self._check_fingerprint(slot, fingerprint)
                    messages = _iter_mbox(handle, self._policy, src, slot["position"])
                    await self._consume(messages, index, src, slot, state, seen, seen_sets, cancel)
            else:
                if not path.is_file() and not path.is_dir():
                    raise _Refused(IssueCode.PATH_NOT_REGULAR)
                self._check_fingerprint(slot, _eml_fingerprint(path, self._policy))
                messages = _iter_eml(path, self._policy, src, slot["position"])
                await self._consume(messages, index, src, slot, state, seen, seen_sets, cancel)
        except _Refused as exc:
            limit = exc.code in (
                IssueCode.MESSAGE_COUNT_EXCEEDED,
                IssueCode.EML_FILE_COUNT_EXCEEDED,
            )
            src.status = SourceStatus.LIMIT_REACHED if limit else SourceStatus.REFUSED
            src.status_code = exc.code
        except OSError:
            src.status, src.status_code = SourceStatus.FAILED, IssueCode.READ_ERROR

    def _check_fingerprint(self, slot: dict[str, Any], fingerprint: str) -> None:
        saved = slot.get("fingerprint")
        if saved is None:
            slot["fingerprint"] = fingerprint
        elif saved != fingerprint:
            raise _SourceChanged

    async def _consume(
        self,
        messages: Iterator[_RawMessage],
        index: int,
        src: SourceManifest,
        slot: dict[str, Any],
        state: dict[str, Any],
        seen: set[str],
        seen_sets: dict[str, set[str]],
        cancel: Callable[[], bool] | None,
    ) -> None:
        since = 0

        def snapshot() -> None:
            slot.update(src.to_dict())
            state["seen"] = {a: sorted(d) for a, d in seen_sets.items()}

        if cancel is not None and cancel():
            snapshot()
            raise _Cancel
        for message in messages:
            before = sum(src.quarantined.values())
            record = _accept(message, src.account_scope, index, src, self._policy, seen)
            if sum(src.quarantined.values()) > before:
                self._quarantine(state, index, message, src)
            if record is not None and not self._request.dry_run:
                await self._emit(record, message)
            slot["position"] = message.next_position
            since += 1
            if cancel is not None and cancel():  # only between fully handled messages
                snapshot()
                raise _Cancel
            if since >= self._every:
                snapshot()
                await self._save(state)
                since = 0
        snapshot()

    def _quarantine(
        self, state: dict[str, Any], index: int, message: _RawMessage, src: SourceManifest
    ) -> None:
        code = message.issue.value if message.data is None else IssueCode.PARSE_FAILED.value
        if len(state["quarantine"]) < self._max_quarantine:
            state["quarantine"].append(
                {"source_index": index, "position": message.offset, "code": code}
            )
        else:
            state["quarantine_dropped"] += 1

    async def _emit(self, record: ImportRecord, message: _RawMessage) -> None:
        raw_ref: str | None = None
        if self.raw_capture_active:
            assert self._raw_store is not None  # noqa: S101 - raw_capture_active checks it
            raw_ref = f"{self._raw_policy.key_prefix}/{record.account_scope}/{record.sha256}"
            if not await self._raw_store.exists(raw_ref):
                await self._raw_store.put(
                    raw_ref,
                    record.raw,
                    content_type="message/rfc822",
                    metadata={
                        "sha256": record.sha256,
                        "acquisition_mode": record.acquisition_mode.value,
                    },
                )
        stored = record.evidence_fidelity if raw_ref else EvidenceFidelity.METADATA_ONLY
        entry = EvidenceEntry(
            idempotency_key=f"{record.account_scope}|{record.logical_key}|{record.sha256}",
            account_scope=record.account_scope,
            logical_key=record.logical_key,
            sha256=record.sha256,
            source_index=record.source_index,
            position=message.offset,
            acquisition_mode=record.acquisition_mode.value,
            source_fidelity=record.evidence_fidelity,
            stored_fidelity=stored,
            raw_content_ref=raw_ref,
            missing_parts=() if raw_ref else (RAW_NOT_RETAINED,),
            inspection_complete=record.inspection_complete,
            gmail_labels_raw=record.gmail_labels_raw,
        )
        assert self._sink is not None  # noqa: S101 - checked in __init__
        await self._sink(entry)

    # -- results -------------------------------------------------------------------------

    def _refused(self, refusal: JobRefusal) -> JobResult:
        return JobResult(JobStatus.REFUSED, ImportManifest(self._request.dry_run), refusal)

    def _result(
        self, state: dict[str, Any], status: JobStatus, refusal: JobRefusal | None = None
    ) -> JobResult:
        manifest = ImportManifest(dry_run=bool(state["dry_run"]))
        manifest.sources = [_source_from_dict(item) for item in state["sources"]]
        seen = {a: set(d) for a, d in state["seen"].items()}
        for account, digests in seen.items():
            others = set().union(*(d for a, d in seen.items() if a != account))
            shared = len(digests & others)
            if shared:
                manifest.cross_account_duplicates[account] = shared
        return JobResult(
            status=status,
            manifest=manifest,
            refusal=refusal,
            quarantine=list(state["quarantine"]),
            quarantine_dropped=int(state["quarantine_dropped"]),
        )


class _SourceChanged(Exception):
    pass


# -- helpers -----------------------------------------------------------------------------


def _source_from_dict(item: dict[str, Any]) -> SourceManifest:
    src = SourceManifest(
        source_index=int(item["source_index"]),
        account_scope=str(item["account_scope"]),
        kind=SourceKind(item["kind"]),
        status=SourceStatus(item["status"]),
        status_code=IssueCode(item["status_code"]) if item.get("status_code") else None,
        bytes_read=int(item["bytes_read"]),
        messages_seen=int(item["messages_seen"]),
        records=int(item["records"]),
        duplicates_in_account=int(item["duplicates_in_account"]),
        inspection_incomplete=int(item["inspection_incomplete"]),
        with_gmail_labels=int(item["with_gmail_labels"]),
        without_message_id=int(item["without_message_id"]),
    )
    src.quarantined = Counter({str(k): int(v) for k, v in dict(item["quarantined"]).items()})
    return src


def _request_fingerprint(request: ImportRequest, policy: ImportPolicy) -> str:
    """Digest of everything that changes what a run produces. Paths are hashed only."""
    material = {
        "accounts": sorted(request.selected_accounts),
        "sources": [
            [str(s.account_scope), os.path.abspath(os.fspath(s.path)), str(s.kind)]
            for s in request.sources
        ],
        "dry_run": request.dry_run,
        "caps": [
            policy.max_message_bytes,
            policy.max_line_bytes,
            policy.max_messages_per_source,
            policy.max_eml_files,
            policy.max_eml_depth,
            policy.max_header_scan_bytes,
            repr(policy.parser_limits),
        ],
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def _mbox_fingerprint(handle: Any) -> str:
    info = os.fstat(handle.fileno())
    position = handle.tell()
    head = handle.read(_HEAD_FINGERPRINT_BYTES)
    handle.seek(position)
    digest = hashlib.sha256(head).hexdigest()
    return f"mbox:{info.st_size}:{info.st_mtime_ns}:{digest}"


def _eml_fingerprint(path: Path, policy: ImportPolicy) -> str:
    """Digest of the walk order (relative names and sizes); contents are not read."""
    hasher = hashlib.sha256()
    if path.is_file():
        entries: list[Path] = [path]
    else:
        entries = []
        for count, entry in enumerate(_walk_eml(path, policy, Counter()), 1):
            if count > policy.max_eml_files:
                break
            entries.append(entry)
    for entry in entries:
        try:
            size = os.lstat(entry).st_size
        except OSError:
            size = -1
        rel = os.path.relpath(entry, path) if path.is_dir() else entry.name
        hasher.update(f"{rel}\x1f{size}\x1e".encode())
    return f"eml:{len(entries)}:{hasher.hexdigest()}"


__all__ = [
    "JOB_STATE_VERSION",
    "RAW_NOT_RETAINED",
    "CheckpointStore",
    "EntrySink",
    "EvidenceEntry",
    "FileCheckpointStore",
    "ImportJob",
    "InMemoryCheckpointStore",
    "JobRefusal",
    "JobResult",
    "JobStatus",
    "RawCapturePolicy",
]
