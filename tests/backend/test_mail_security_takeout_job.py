"""NerveShield task 5.2: resumable, idempotent Takeout import job.

Synthetic multi-account fixtures only; network disabled. The central property:
crashing at every checkpoint, every evidence write and every raw-store write,
or cancelling after any message, then resuming, yields exactly the result of
an uninterrupted run (same entries, raw objects, manifest and quarantine) with
no duplicate entries.
"""

from __future__ import annotations

import json
import os
import re
import socket
import stat
from pathlib import Path
from typing import Any

import pytest

from backend.mail_security.models import EvidenceFidelity
from backend.mail_security.takeout_import import (
    ImportPolicy,
    ImportRequest,
    ImportSource,
    IssueCode,
    SourceKind,
    SourceStatus,
)
from backend.mail_security.takeout_job import (
    RAW_NOT_RETAINED,
    EvidenceEntry,
    FileCheckpointStore,
    ImportJob,
    InMemoryCheckpointStore,
    JobRefusal,
    JobStatus,
    RawCapturePolicy,
)
from backend.storage.memory import InMemoryObjectStore

SECRET_ADDR = "bob.sentinel@example.test"
SECRET_SUBJECT = "SUBJECT-SENTINEL-5-2"
SECRET_LABEL = "LabelSentinel52"


class SimulatedCrash(Exception):
    pass


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any, **__: Any) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def _eml(n: int, *, body: str = "Body.", message_id: bool = True) -> bytes:
    lines = [
        f"From: S{n} <{SECRET_ADDR}>",
        "To: me@example.test",
        f"Subject: {SECRET_SUBJECT} {n}",
        f"X-Gmail-Labels: Inbox,{SECRET_LABEL}",
    ]
    if message_id:
        lines.append(f"Message-ID: <m{n}@example.test>")
    lines += ["Content-Type: text/plain", "", body, ""]
    return "\n".join(lines).encode()


def _mbox(*messages: bytes) -> bytes:
    out = b""
    for i, msg in enumerate(messages):
        msg = re.sub(rb"(?m)^(>*From )", rb">\1", msg)
        out += f"From {i}@xxx Mon Nov 02 10:00:00 +0000 2026\n".encode() + msg + b"\n"
    return out


@pytest.fixture
def corpus(tmp_path: Path) -> tuple[Path, ImportRequest]:
    a = tmp_path / "a"
    b = tmp_path / "b"
    eml = tmp_path / "a_eml"
    for d in (a, b, eml):
        d.mkdir()
    shared = _eml(50)
    (a / "All mail.mbox").write_bytes(
        _mbox(
            _eml(1),
            _eml(2, message_id=False),
            _eml(1),  # in-account duplicate
            _eml(3, body="X" * 4000),  # oversize -> quarantine
            shared,
            _eml(4),
        )
        + b"From 9@xxx Mon Nov 02 10:00:00 +0000 2026\n\n"  # empty -> quarantine
        + _mbox(_eml(5))
    )
    (b / "All mail.mbox").write_bytes(_mbox(_eml(10), shared, _eml(11)))
    (eml / "x.eml").write_bytes(_eml(20))
    (eml / "y.eml").write_bytes(_eml(21))
    (eml / "big.eml").write_bytes(_eml(22, body="Y" * 4000))
    (eml / "dup.eml").write_bytes(_eml(1))  # same bytes as mbox a #1: in-account duplicate
    request = ImportRequest(
        selected_accounts=frozenset({"gmail:a", "gmail:b"}),
        sources=(
            ImportSource("gmail:a", a / "All mail.mbox", SourceKind.MBOX),
            ImportSource("gmail:b", b / "All mail.mbox", SourceKind.MBOX),
            ImportSource("gmail:a", eml, SourceKind.EML),
        ),
        dry_run=False,
    )
    return tmp_path, request


def _policy(root: Path) -> ImportPolicy:
    return ImportPolicy(allowed_roots=(root,), max_message_bytes=1500)


class Sink:
    """Upserting evidence sink, as the contract requires; records call count."""

    def __init__(self, crash_at: int | None = None) -> None:
        self.entries: dict[str, EvidenceEntry] = {}
        self.calls = 0
        self.crash_at = crash_at

    async def __call__(self, entry: EvidenceEntry) -> None:
        self.calls += 1
        if self.crash_at is not None and self.calls == self.crash_at:
            raise SimulatedCrash
        self.entries[entry.idempotency_key] = entry


class CrashingCheckpoints(InMemoryCheckpointStore):
    def __init__(self, crash_at: int, *, after_write: bool) -> None:
        super().__init__()
        self.crash_at = crash_at
        self.after_write = after_write
        self.attempts = 0

    async def save(self, job_id: str, state: dict[str, Any]) -> None:
        self.attempts += 1
        if self.attempts == self.crash_at and not self.after_write:
            raise SimulatedCrash
        await super().save(job_id, state)
        if self.attempts == self.crash_at and self.after_write:
            raise SimulatedCrash


class CrashingStore(InMemoryObjectStore):
    def __init__(self, crash_at: int | None = None) -> None:
        super().__init__()
        self.crash_at = crash_at
        self.puts = 0

    async def put(self, key: str, data: bytes, **kw: Any) -> Any:
        self.puts += 1
        if self.crash_at is not None and self.puts == self.crash_at:
            raise SimulatedCrash
        return await super().put(key, data, **kw)


async def _snapshot(store: InMemoryObjectStore) -> dict[str, bytes]:
    return {k: v[0] for k, v in store._objects.items()}  # noqa: SLF001 - test introspection


def _job(
    root: Path,
    request: ImportRequest,
    checkpoints: Any,
    sink: Sink,
    store: InMemoryObjectStore | None = None,
    every: int = 1,
) -> ImportJob:
    return ImportJob(
        "job-1",
        request,
        _policy(root),
        checkpoints,
        entry_sink=sink,
        raw_store=store,
        raw_policy=RawCapturePolicy(allowed=store is not None),
        checkpoint_every=every,
    )


async def _clean(root: Path, request: ImportRequest, every: int = 1) -> Any:
    sink, store, cps = Sink(), CrashingStore(), InMemoryCheckpointStore()
    result = await _job(root, request, cps, sink, store, every).run()
    assert result.status is JobStatus.COMPLETED
    return result, sink, store, cps


def _same(a: Any, b: Any) -> None:
    assert a.manifest.to_dict() == b.manifest.to_dict()
    assert a.quarantine == b.quarantine


# -- baseline ----------------------------------------------------------------------------


async def test_clean_run_counts_provenance_and_cross_account(corpus: Any) -> None:
    root, request = corpus
    result, sink, store, _ = await _clean(root, request)
    a_mbox, b_mbox, a_eml = result.manifest.sources
    assert (a_mbox.records, a_mbox.duplicates_in_account) == (5, 1)
    assert a_mbox.quarantined == {"MESSAGE_TOO_LARGE": 1, "PARSE_FAILED": 1}
    assert b_mbox.records == 3
    assert (a_eml.records, a_eml.duplicates_in_account) == (2, 1)
    assert a_eml.quarantined == {"MESSAGE_TOO_LARGE": 1}
    assert dict(result.manifest.cross_account_duplicates) == {"gmail:a": 1, "gmail:b": 1}
    assert len(sink.entries) == 10
    assert {e.account_scope for e in sink.entries.values()} == {"gmail:a", "gmail:b"}
    shared = [
        e
        for e in sink.entries.values()
        if e.raw_content_ref
        and e.sha256 in {x.sha256 for x in sink.entries.values() if x.account_scope == "gmail:b"}
        and e.account_scope == "gmail:a"
    ]
    assert len(shared) == 1
    raw = await _snapshot(store)
    assert len(raw) == 10
    assert {k.split("/")[2] for k in raw} == {"gmail:a", "gmail:b"}
    assert [q["code"] for q in result.quarantine] == [
        "MESSAGE_TOO_LARGE",
        "PARSE_FAILED",
        "MESSAGE_TOO_LARGE",
    ]


async def test_raw_capture_is_off_by_default_and_labelled_honestly(corpus: Any) -> None:
    root, request = corpus
    sink, store = Sink(), InMemoryObjectStore()
    job = ImportJob(
        "job-1", request, _policy(root), InMemoryCheckpointStore(), entry_sink=sink, raw_store=store
    )
    assert job.raw_capture_active is False
    result = await job.run()
    assert result.status is JobStatus.COMPLETED
    assert await _snapshot(store) == {}
    for entry in sink.entries.values():
        assert entry.raw_content_ref is None
        assert entry.stored_fidelity is EvidenceFidelity.METADATA_ONLY
        assert entry.source_fidelity is EvidenceFidelity.ORIGINAL_RFC822
        assert entry.missing_parts == (RAW_NOT_RETAINED,)


async def test_raw_capture_when_allowed_stores_exact_bytes(corpus: Any) -> None:
    root, request = corpus
    _, sink, store, _ = await _clean(root, request)
    raw = await _snapshot(store)
    for entry in sink.entries.values():
        assert entry.stored_fidelity is EvidenceFidelity.ORIGINAL_RFC822
        assert entry.missing_parts == ()
        assert entry.raw_content_ref in raw
    assert _eml(1) in raw.values() and _eml(20) in raw.values()


async def test_dry_run_job_writes_nothing(corpus: Any) -> None:
    root, request = corpus
    dry = ImportRequest(request.selected_accounts, request.sources, dry_run=True)
    store = InMemoryObjectStore()
    result = await ImportJob(
        "job-1",
        dry,
        _policy(root),
        InMemoryCheckpointStore(),
        raw_store=store,
        raw_policy=RawCapturePolicy(allowed=True),
    ).run()
    assert result.status is JobStatus.COMPLETED and result.manifest.dry_run is True
    assert await _snapshot(store) == {}
    assert sum(s.records for s in result.manifest.sources) == 10


# -- crash at every checkpoint / write ---------------------------------------------------


@pytest.mark.parametrize("every", [1, 2, 5])
@pytest.mark.parametrize("after_write", [False, True])
async def test_crash_at_every_checkpoint_then_resume_matches_clean_run(
    corpus: Any, every: int, after_write: bool
) -> None:
    root, request = corpus
    clean, clean_sink, clean_store, clean_cps = await _clean(root, request, every)
    for k in range(1, clean_cps.saves + 1):
        sink, store = Sink(), CrashingStore()
        cps = CrashingCheckpoints(k, after_write=after_write)
        with pytest.raises(SimulatedCrash):
            await _job(root, request, cps, sink, store, every).run()
        cps.crash_at = -1  # the process restarts; storage survives
        resumed = await _job(root, request, cps, sink, store, every).run()
        assert resumed.status is JobStatus.COMPLETED, k
        _same(resumed, clean)
        assert sink.entries.keys() == clean_sink.entries.keys(), k
        assert await _snapshot(store) == await _snapshot(clean_store), k


@pytest.mark.parametrize("every", [1, 3])
async def test_crash_at_every_evidence_write_then_resume_matches(corpus: Any, every: int) -> None:
    root, request = corpus
    clean, clean_sink, clean_store, _ = await _clean(root, request, every)
    for k in range(1, clean_sink.calls + 1):
        sink, store, cps = Sink(crash_at=k), CrashingStore(), InMemoryCheckpointStore()
        with pytest.raises(SimulatedCrash):
            await _job(root, request, cps, sink, store, every).run()
        sink.crash_at = None
        resumed = await _job(root, request, cps, sink, store, every).run()
        _same(resumed, clean)
        assert sink.entries.keys() == clean_sink.entries.keys(), k
        assert await _snapshot(store) == await _snapshot(clean_store), k


async def test_crash_at_every_raw_write_then_resume_matches(corpus: Any) -> None:
    root, request = corpus
    clean, clean_sink, clean_store, _ = await _clean(root, request, 2)
    for k in range(1, clean_store.puts + 1):
        sink, store, cps = Sink(), CrashingStore(crash_at=k), InMemoryCheckpointStore()
        with pytest.raises(SimulatedCrash):
            await _job(root, request, cps, sink, store, 2).run()
        store.crash_at = None
        resumed = await _job(root, request, cps, sink, store, 2).run()
        _same(resumed, clean)
        assert await _snapshot(store) == await _snapshot(clean_store), k
        assert sink.entries.keys() == clean_sink.entries.keys(), k


async def test_raw_objects_are_written_once_per_account_and_digest(corpus: Any) -> None:
    root, request = corpus
    _, _, store, _ = await _clean(root, request)
    assert store.puts == len(await _snapshot(store))


# -- cancel / resume ---------------------------------------------------------------------


async def test_cancel_after_any_message_then_resume_matches(corpus: Any) -> None:
    root, request = corpus
    clean, clean_sink, clean_store, _ = await _clean(root, request, 4)
    total = sum(s.messages_seen for s in clean.manifest.sources) + len(request.sources)
    for n in range(1, total + 1):
        sink, store, cps = Sink(), CrashingStore(), InMemoryCheckpointStore()
        checks = {"n": 0}

        def cancel(n: int = n, checks: dict[str, int] = checks) -> bool:
            checks["n"] += 1
            return checks["n"] == n

        first = await _job(root, request, cps, sink, store, 4).run(cancel)
        if first.status is JobStatus.COMPLETED:
            continue
        assert first.status is JobStatus.CANCELLED
        resumed = await _job(root, request, cps, sink, store, 4).run()
        assert resumed.status is JobStatus.COMPLETED, n
        _same(resumed, clean)
        assert sink.entries.keys() == clean_sink.entries.keys(), n
        assert await _snapshot(store) == await _snapshot(clean_store), n


async def test_rerun_of_a_finished_job_does_no_work(corpus: Any) -> None:
    root, request = corpus
    clean, sink, store, cps = await _clean(root, request)
    calls, puts = sink.calls, store.puts
    again = await _job(root, request, cps, sink, store).run()
    assert again.status is JobStatus.COMPLETED
    _same(again, clean)
    assert (sink.calls, store.puts) == (calls, puts)


# -- fail closed on drift ----------------------------------------------------------------


async def _cancelled_midway(root: Path, request: ImportRequest) -> tuple[Any, Sink, Any]:
    sink, store, cps = Sink(), CrashingStore(), InMemoryCheckpointStore()
    checks = {"n": 0}

    def cancel() -> bool:
        checks["n"] += 1
        return checks["n"] == 3

    first = await _job(root, request, cps, sink, store).run(cancel)
    assert first.status is JobStatus.CANCELLED
    return cps, sink, store


async def test_resume_refused_when_source_file_changed(corpus: Any) -> None:
    root, request = corpus
    cps, sink, store = await _cancelled_midway(root, request)
    path = Path(request.sources[0].path)
    path.write_bytes(path.read_bytes() + _mbox(_eml(99)))
    calls = sink.calls
    result = await _job(root, request, cps, sink, store).run()
    assert result.status is JobStatus.REFUSED and result.refusal is JobRefusal.SOURCE_CHANGED
    assert sink.calls == calls


async def test_resume_refused_when_eml_listing_changed(corpus: Any) -> None:
    root, request = corpus
    sink, store, cps = Sink(), CrashingStore(), InMemoryCheckpointStore()
    checks = {"n": 0}

    def cancel() -> bool:  # stop inside the EML source (after both mbox sources)
        checks["n"] += 1
        return checks["n"] == 14

    first = await _job(root, request, cps, sink, store).run(cancel)
    assert first.status is JobStatus.CANCELLED
    (Path(request.sources[2].path) / "new.eml").write_bytes(_eml(77))
    result = await _job(root, request, cps, sink, store).run()
    assert result.refusal is JobRefusal.SOURCE_CHANGED


async def test_resume_refused_when_request_changed(corpus: Any) -> None:
    root, request = corpus
    cps, sink, store = await _cancelled_midway(root, request)
    other = ImportRequest(frozenset({"gmail:a"}), request.sources, dry_run=False)
    result = await _job(root, other, cps, sink, store).run()
    assert result.status is JobStatus.REFUSED and result.refusal is JobRefusal.JOB_MISMATCH


async def test_resume_refused_when_caps_changed(corpus: Any) -> None:
    root, request = corpus
    cps, sink, store = await _cancelled_midway(root, request)
    job = ImportJob(
        "job-1",
        request,
        ImportPolicy(allowed_roots=(root,), max_message_bytes=9999),
        cps,
        entry_sink=sink,
    )
    assert (await job.run()).refusal is JobRefusal.JOB_MISMATCH


@pytest.mark.parametrize(
    ("mutate", "refusal"),
    [
        (lambda s: s.update(version=99), JobRefusal.STATE_VERSION),
        (lambda s: s.update(current="x"), JobRefusal.STATE_CORRUPT),
        (lambda s: s.update(current=99), JobRefusal.STATE_CORRUPT),
        (lambda s: s.update(job_id="other"), JobRefusal.STATE_CORRUPT),
        (lambda s: s.update(seen={"bad scope": []}), JobRefusal.STATE_CORRUPT),
        (lambda s: s["sources"][0].update(position=-1), JobRefusal.STATE_CORRUPT),
        (lambda s: s["sources"][0].update(kind="ZIP"), JobRefusal.STATE_CORRUPT),
        (lambda s: s.pop("quarantine"), JobRefusal.STATE_CORRUPT),
    ],
)
async def test_corrupt_or_foreign_state_is_refused(
    corpus: Any, mutate: Any, refusal: JobRefusal
) -> None:
    root, request = corpus
    cps, sink, store = await _cancelled_midway(root, request)
    state = json.loads(cps.states["job-1"])
    mutate(state)
    cps.states["job-1"] = json.dumps(state)
    calls = sink.calls
    result = await _job(root, request, cps, sink, store).run()
    assert result.status is JobStatus.REFUSED and result.refusal is refusal
    assert sink.calls == calls


async def test_unparseable_state_is_refused(corpus: Any) -> None:
    root, request = corpus
    cps, sink, store = await _cancelled_midway(root, request)
    cps.states["job-1"] = "{not json"
    assert (await _job(root, request, cps, sink, store).run()).refusal is JobRefusal.STATE_CORRUPT


async def test_tampered_offset_off_a_separator_refuses_the_source(corpus: Any) -> None:
    root, request = corpus
    cps, sink, store = await _cancelled_midway(root, request)
    state = json.loads(cps.states["job-1"])
    state["sources"][0]["position"] += 3
    cps.states["job-1"] = json.dumps(state)
    result = await _job(root, request, cps, sink, store).run()
    src = result.manifest.sources[0]
    assert src.status is SourceStatus.REFUSED and src.status_code is IssueCode.CHECKPOINT_MISMATCH


# -- checkpoint file store ---------------------------------------------------------------


async def test_file_checkpoint_store_is_atomic_private_and_contentless(
    corpus: Any, tmp_path: Path
) -> None:
    root, request = corpus
    directory = tmp_path / "ckpt"
    cps = FileCheckpointStore(directory)
    sink = Sink()
    result = await _job(root, request, cps, sink).run()
    assert result.status is JobStatus.COMPLETED
    files = list(directory.iterdir())
    assert [f.name for f in files] == ["job-1.json"]  # no leftover temp files
    assert stat.S_IMODE(os.stat(files[0]).st_mode) == 0o600
    text = files[0].read_text()
    for needle in (SECRET_ADDR, SECRET_SUBJECT, SECRET_LABEL, "m1@example", str(root), "All mail"):
        assert needle not in text
    again = await _job(root, request, FileCheckpointStore(directory), sink).run()
    _same(again, result)


@pytest.mark.parametrize("bad", ["../x", "a/b", "", "x" * 65, "j.json"])
async def test_job_ids_cannot_escape_the_checkpoint_directory(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        await FileCheckpointStore(tmp_path).save(bad, {})


async def test_file_store_crash_during_write_keeps_previous_state(
    corpus: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileCheckpointStore(tmp_path / "c")
    await store.save("job-1", {"v": 1})
    import backend.mail_security.takeout_job as tj

    def boom(*_: Any) -> None:
        raise SimulatedCrash

    monkeypatch.setattr(tj.os, "replace", boom)
    with pytest.raises(SimulatedCrash):
        await store.save("job-1", {"v": 2})
    assert await store.load("job-1") == {"v": 1}
    assert [p.name for p in (tmp_path / "c").iterdir()] == ["job-1.json"]


# -- quarantine bound / privacy ----------------------------------------------------------


async def test_quarantine_is_bounded_and_contentless(corpus: Any) -> None:
    root, request = corpus
    job = ImportJob(
        "job-1",
        request,
        _policy(root),
        InMemoryCheckpointStore(),
        entry_sink=Sink(),
        max_quarantine_entries=1,
    )
    result = await job.run()
    assert len(result.quarantine) == 1 and result.quarantine_dropped == 2
    assert set(result.quarantine[0]) == {"source_index", "position", "code"}


async def test_entries_repr_hides_labels(corpus: Any) -> None:
    root, request = corpus
    _, sink, _, _ = await _clean(root, request)
    for entry in sink.entries.values():
        assert SECRET_LABEL not in repr(entry) and SECRET_ADDR not in repr(entry)


def test_real_run_requires_a_sink(tmp_path: Path) -> None:
    req = ImportRequest(frozenset({"a"}), (), dry_run=False)
    with pytest.raises(ValueError):
        ImportJob("job-1", req, ImportPolicy(allowed_roots=(tmp_path,)), InMemoryCheckpointStore())
