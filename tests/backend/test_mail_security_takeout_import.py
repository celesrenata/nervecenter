"""NerveShield task 5.1: offline opt-in Takeout MBOX/EML import.

Synthetic multi-account fixtures only, generated per test under ``tmp_path``.
Network is disabled for every test in this module.
"""

from __future__ import annotations

import json
import os
import re
import socket
from pathlib import Path
from typing import Any

import pytest

from backend.mail_security import takeout_import as ti
from backend.mail_security.models import AcquisitionMode, EvidenceFidelity
from backend.mail_security.takeout_import import (
    ImportPolicy,
    ImportRecord,
    ImportRequest,
    ImportSource,
    IssueCode,
    SourceKind,
    SourceStatus,
    run_import,
)

SECRET_SUBJECT = "SUBJECT-SENTINEL wire funds"
SECRET_ADDR = "alice.sentinel@example.test"
SECRET_LABEL = "LabelSentinel"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any, **__: Any) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def _eml(
    n: int,
    *,
    subject: str = "Hello",
    body: str = "Body text.",
    message_id: bool = True,
    labels: str | None = "Inbox,Important",
) -> bytes:
    lines = [f"From: Sender {n} <{SECRET_ADDR}>", "To: me@example.test", f"Subject: {subject} {n}"]
    if message_id:
        lines.append(f"Message-ID: <msg{n}@example.test>")
    if labels is not None:
        lines.append(f"X-Gmail-Labels: {labels}")
    lines += ["Date: Mon, 2 Nov 2026 10:00:00 +0000", "Content-Type: text/plain", "", body, ""]
    return "\n".join(lines).encode()


def _mbox(*messages: bytes, quote: bool = True) -> bytes:
    out = b""
    for i, msg in enumerate(messages):
        if quote:
            msg = re.sub(rb"(?m)^(>*From )", rb">\1", msg)
        out += f"From {1000 + i}@xxx Mon Nov 02 10:00:00 +0000 2026\n".encode() + msg + b"\n"
    return out


def _policy(root: Path, **over: Any) -> ImportPolicy:
    return ImportPolicy(allowed_roots=(root,), **over)


def _run(
    root: Path,
    sources: list[ImportSource],
    *,
    accounts: set[str] | None = None,
    dry_run: bool = False,
    **policy: Any,
) -> tuple[ti.ImportManifest, list[ImportRecord]]:
    got: list[ImportRecord] = []
    req = ImportRequest(
        selected_accounts=frozenset(
            accounts if accounts is not None else {s.account_scope for s in sources}
        ),
        sources=tuple(sources),
        dry_run=dry_run,
    )
    manifest = run_import(req, _policy(root, **policy), sink=None if dry_run else got.append)
    return manifest, got


@pytest.fixture
def two_accounts(tmp_path: Path) -> tuple[Path, list[ImportSource]]:
    shared = _eml(99, subject=SECRET_SUBJECT, labels=SECRET_LABEL)
    a = tmp_path / "acct_a" / "All mail.mbox"
    b = tmp_path / "acct_b" / "All mail.mbox"
    a.parent.mkdir()
    b.parent.mkdir()
    a.write_bytes(_mbox(_eml(1), _eml(2, message_id=False), _eml(1), shared))
    b.write_bytes(_mbox(_eml(3, labels=None), shared))
    return tmp_path, [
        ImportSource("gmail:a", a, SourceKind.MBOX),
        ImportSource("gmail:b", b, SourceKind.MBOX),
    ]


# -- multi-account import ----------------------------------------------------------------


def test_multi_account_import_keeps_provenance_and_counts(two_accounts: Any) -> None:
    root, sources = two_accounts
    manifest, records = _run(root, sources)
    a, b = manifest.sources
    assert (a.status, b.status) == (SourceStatus.COMPLETED, SourceStatus.COMPLETED)
    assert (a.messages_seen, a.records, a.duplicates_in_account) == (4, 3, 1)
    assert (b.messages_seen, b.records) == (2, 2)
    assert a.without_message_id == 1 and a.with_gmail_labels == 3 and b.with_gmail_labels == 1
    assert [r.account_scope for r in records] == ["gmail:a"] * 3 + ["gmail:b"] * 2
    shared = [r for r in records if b"Message-ID: <msg99@" in r.raw]
    assert {r.account_scope for r in shared} == {"gmail:a", "gmail:b"}
    assert shared[0].sha256 == shared[1].sha256
    assert shared[0].logical_key != shared[1].logical_key
    assert dict(manifest.cross_account_duplicates) == {"gmail:a": 1, "gmail:b": 1}
    totals = manifest.account_totals()
    assert totals["gmail:a"]["records"] == 3 and totals["gmail:b"]["cross_account_duplicates"] == 1


def test_records_carry_honest_acquisition_mode_and_exact_bytes(two_accounts: Any) -> None:
    root, sources = two_accounts
    _, records = _run(root, sources)
    first = records[0]
    assert first.acquisition_mode is AcquisitionMode.TAKEOUT_MBOX
    assert first.evidence_fidelity is EvidenceFidelity.ORIGINAL_RFC822
    assert first.raw == _eml(1)
    assert first.offset == 0
    assert first.gmail_labels_raw == "Inbox,Important"
    raw_file = sources[0].path.read_bytes()
    assert raw_file[records[1].offset :].startswith(b"From 1001@xxx")
    no_id = records[1]
    assert ":puid:" in no_id.logical_key and ":rfcmid:" in first.logical_key


def test_mboxrd_quoted_from_lines_are_unquoted(tmp_path: Path) -> None:
    body = "line one\nFrom the desk of someone\n>From already quoted\nend"
    path = tmp_path / "x.mbox"
    path.write_bytes(_mbox(_eml(1, body=body)))
    _, records = _run(tmp_path, [ImportSource("acct", path, SourceKind.MBOX)])
    assert records[0].raw == _eml(1, body=body)


def test_unquoted_from_line_inside_body_does_not_split_a_message(tmp_path: Path) -> None:
    body = "first line\nFrom here on, unquoted (mboxo)\nlast"
    path = tmp_path / "x.mbox"
    path.write_bytes(_mbox(_eml(1, body=body), _eml(2), quote=False))
    manifest, records = _run(tmp_path, [ImportSource("acct", path, SourceKind.MBOX)])
    assert manifest.sources[0].messages_seen == 2 and len(records) == 2
    assert b"From here on" in records[0].raw


# -- dry run -----------------------------------------------------------------------------


def test_dry_run_is_the_default_and_never_calls_a_sink(two_accounts: Any) -> None:
    root, sources = two_accounts
    req = ImportRequest(selected_accounts=frozenset({"gmail:a", "gmail:b"}), sources=tuple(sources))
    assert req.dry_run is True
    calls: list[Any] = []
    preview = run_import(req, _policy(root), sink=calls.append)
    assert calls == []
    real, _ = _run(root, sources)
    assert preview.to_dict()["accounts"] == real.to_dict()["accounts"]
    assert preview.dry_run is True and real.dry_run is False


def test_real_run_requires_a_sink(two_accounts: Any) -> None:
    root, sources = two_accounts
    req = ImportRequest(frozenset({"gmail:a"}), tuple(sources), dry_run=False)
    with pytest.raises(ValueError):
        run_import(req, _policy(root))


# -- explicit account selection ----------------------------------------------------------


def test_unselected_account_is_never_opened(
    two_accounts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, sources = two_accounts
    opened: list[str] = []
    real_open = os.open

    def spy(path: Any, *args: Any, **kw: Any) -> int:
        opened.append(os.fspath(path))
        return real_open(path, *args, **kw)

    monkeypatch.setattr(ti.os, "open", spy)
    manifest, records = _run(root, sources, accounts={"gmail:a"})
    assert manifest.sources[1].status is SourceStatus.SKIPPED
    assert manifest.sources[1].status_code is IssueCode.ACCOUNT_NOT_SELECTED
    assert {r.account_scope for r in records} == {"gmail:a"}
    assert all("acct_b" not in p for p in opened)


def test_no_accounts_selected_reads_nothing(two_accounts: Any) -> None:
    root, sources = two_accounts
    manifest, records = _run(root, sources, accounts=set())
    assert records == [] and all(s.status is SourceStatus.SKIPPED for s in manifest.sources)


@pytest.mark.parametrize("bad", ["", "has space", "a/b", "x" * 200])
def test_invalid_account_scope_is_refused(tmp_path: Path, bad: str) -> None:
    path = tmp_path / "x.mbox"
    path.write_bytes(_mbox(_eml(1)))
    req = ImportRequest(
        frozenset({"ok"}), (ImportSource(bad, path, SourceKind.MBOX),), dry_run=True
    )
    manifest = run_import(req, _policy(tmp_path))
    assert manifest.sources[0].status is SourceStatus.REFUSED
    assert manifest.sources[0].status_code is IssueCode.ACCOUNT_INVALID


def test_invalid_selected_account_rejected_up_front(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        run_import(ImportRequest(frozenset({"bad scope"}), ()), _policy(tmp_path))


# -- archive policy ----------------------------------------------------------------------


def _refusal(root: Path, path: Path, kind: SourceKind = SourceKind.MBOX, **pol: Any) -> Any:
    manifest, records = _run(root, [ImportSource("acct", path, kind)], **pol)
    assert records == []
    return manifest.sources[0]


def test_path_outside_allowed_roots_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "elsewhere.mbox"
    outside.write_bytes(_mbox(_eml(1)))
    src = _refusal(root, outside)
    assert (src.status, src.status_code) == (SourceStatus.REFUSED, IssueCode.PATH_OUTSIDE_ROOTS)


def test_dotdot_traversal_out_of_root_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "secret.mbox").write_bytes(_mbox(_eml(1)))
    src = _refusal(root, root / ".." / "secret.mbox")
    assert src.status_code is IssueCode.PATH_OUTSIDE_ROOTS


def test_symlinked_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "real.mbox"
    target.write_bytes(_mbox(_eml(1)))
    link = tmp_path / "link.mbox"
    link.symlink_to(target)
    assert _refusal(tmp_path, link).status_code is IssueCode.PATH_SYMLINK


def test_symlinked_directory_component_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.mbox").write_bytes(_mbox(_eml(1)))
    (tmp_path / "alias").symlink_to(real)
    assert _refusal(tmp_path, tmp_path / "alias" / "a.mbox").status_code is IssueCode.PATH_SYMLINK


@pytest.mark.parametrize("name", ["takeout.zip", "All mail.mbox.gz", "t.tgz", "x.mbox.zst"])
def test_compressed_archives_are_refused_without_extraction(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.write_bytes(b"PK\x03\x04 not really")
    assert _refusal(tmp_path, path).status_code is IssueCode.COMPRESSED_ARCHIVE_UNSUPPORTED


def test_wrong_suffix_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "mail.txt"
    path.write_bytes(_mbox(_eml(1)))
    assert _refusal(tmp_path, path).status_code is IssueCode.SUFFIX_NOT_ALLOWED


def test_missing_path_is_refused(tmp_path: Path) -> None:
    assert _refusal(tmp_path, tmp_path / "nope.mbox").status_code is IssueCode.PATH_MISSING


def test_directory_named_mbox_is_refused(tmp_path: Path) -> None:
    (tmp_path / "dir.mbox").mkdir()
    assert _refusal(tmp_path, tmp_path / "dir.mbox").status_code is IssueCode.PATH_NOT_REGULAR


def test_file_size_cap_refuses_before_reading(tmp_path: Path) -> None:
    path = tmp_path / "big.mbox"
    path.write_bytes(_mbox(_eml(1), _eml(2)))
    src = _refusal(tmp_path, path, max_file_bytes=10)
    assert src.status_code is IssueCode.FILE_TOO_LARGE and src.bytes_read == 0


# -- bounded streaming -------------------------------------------------------------------


def test_oversized_message_is_quarantined_and_stream_continues(tmp_path: Path) -> None:
    path = tmp_path / "x.mbox"
    path.write_bytes(_mbox(_eml(1), _eml(2, body="A" * 5000), _eml(3)))
    manifest, records = _run(
        tmp_path,
        [ImportSource("acct", path, SourceKind.MBOX)],
        max_message_bytes=1000,
    )
    src = manifest.sources[0]
    assert src.quarantined[IssueCode.MESSAGE_TOO_LARGE] == 1
    assert [r.raw for r in records] == [_eml(1), _eml(3)]


def test_very_long_line_without_newline_is_bounded(tmp_path: Path) -> None:
    path = tmp_path / "x.mbox"
    path.write_bytes(_mbox(_eml(1, body="B" * 50_000), _eml(2)))
    manifest, records = _run(
        tmp_path,
        [ImportSource("acct", path, SourceKind.MBOX)],
        max_line_bytes=64,
        max_message_bytes=10_000,
    )
    assert manifest.sources[0].quarantined[IssueCode.MESSAGE_TOO_LARGE] == 1
    assert [r.raw for r in records] == [_eml(2)]


def test_from_inside_a_long_line_continuation_never_splits(tmp_path: Path) -> None:
    # "From " starts exactly at a read-chunk boundary (max_line_bytes=64).
    body = "x" * 100 + "\n\n" + "y" * 64 + "From not-a-separator"
    path = tmp_path / "x.mbox"
    path.write_bytes(_mbox(_eml(1, body=body), quote=False))
    manifest, records = _run(
        tmp_path, [ImportSource("acct", path, SourceKind.MBOX)], max_line_bytes=64
    )
    assert manifest.sources[0].messages_seen == 1 and len(records) == 1


def test_message_count_cap_stops_with_limit_status(tmp_path: Path) -> None:
    path = tmp_path / "x.mbox"
    path.write_bytes(_mbox(*(_eml(i) for i in range(5))))
    manifest, records = _run(
        tmp_path, [ImportSource("acct", path, SourceKind.MBOX)], max_messages_per_source=3
    )
    src = manifest.sources[0]
    assert src.status is SourceStatus.LIMIT_REACHED
    assert src.status_code is IssueCode.MESSAGE_COUNT_EXCEEDED
    assert src.messages_seen == 3 and len(records) <= 3


def test_leading_data_before_first_separator_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "x.mbox"
    path.write_bytes(b"garbage line\nmore\n" + _mbox(_eml(1)))
    manifest, records = _run(tmp_path, [ImportSource("acct", path, SourceKind.MBOX)])
    assert manifest.sources[0].quarantined[IssueCode.LEADING_DATA_BEFORE_FIRST_SEPARATOR] == 1
    assert len(records) == 1


def test_empty_message_is_quarantined_as_parse_failure(tmp_path: Path) -> None:
    path = tmp_path / "x.mbox"
    path.write_bytes(b"From 1@xxx Mon Nov 02 10:00:00 +0000 2026\n\n" + _mbox(_eml(1)))
    manifest, records = _run(tmp_path, [ImportSource("acct", path, SourceKind.MBOX)])
    assert manifest.sources[0].quarantined[IssueCode.PARSE_FAILED] == 1
    assert len(records) == 1


def test_empty_mbox_completes_with_nothing(tmp_path: Path) -> None:
    path = tmp_path / "x.mbox"
    path.write_bytes(b"")
    manifest, records = _run(tmp_path, [ImportSource("acct", path, SourceKind.MBOX)])
    assert manifest.sources[0].status is SourceStatus.COMPLETED and records == []


def test_mime_limits_mark_incomplete_but_do_not_drop(tmp_path: Path) -> None:
    from backend.mail_security.parser import ParserLimits

    path = tmp_path / "x.mbox"
    path.write_bytes(_mbox(_eml(1, body="C" * 2000)))
    manifest, records = _run(
        tmp_path,
        [ImportSource("acct", path, SourceKind.MBOX)],
        parser_limits=ParserLimits(max_total_decoded_bytes=100, max_part_decoded_bytes=100),
    )
    assert manifest.sources[0].inspection_incomplete == 1
    assert records[0].inspection_complete is False


# -- EML ---------------------------------------------------------------------------------


def test_eml_directory_walk_is_bounded_and_skips_links(tmp_path: Path) -> None:
    d = tmp_path / "eml"
    (d / "sub").mkdir(parents=True)
    (d / "a.eml").write_bytes(_eml(1))
    (d / "sub" / "b.EML").write_bytes(_eml(2))
    (d / "notes.html").write_text("ignored")
    (d / "big.eml").write_bytes(_eml(3, body="D" * 5000))
    (d / "link.eml").symlink_to(d / "a.eml")
    manifest, records = _run(
        tmp_path, [ImportSource("acct", d, SourceKind.EML)], max_message_bytes=1000
    )
    src = manifest.sources[0]
    assert src.status is SourceStatus.COMPLETED
    assert sorted(r.raw for r in records) == sorted([_eml(1), _eml(2)])
    assert src.quarantined[IssueCode.MESSAGE_TOO_LARGE] == 1
    assert src.quarantined[IssueCode.EML_ENTRY_SKIPPED] == 1
    assert all(r.acquisition_mode is AcquisitionMode.EML for r in records)


def test_eml_single_file_and_depth_cap(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "x.eml").write_bytes(_eml(1))
    single = tmp_path / "one.eml"
    single.write_bytes(_eml(2))
    manifest, records = _run(
        tmp_path,
        [
            ImportSource("acct", tmp_path / "a", SourceKind.EML),
            ImportSource("acct", single, SourceKind.EML),
        ],
        max_eml_depth=1,
    )
    assert manifest.sources[0].records == 0
    assert manifest.sources[0].quarantined[IssueCode.EML_ENTRY_SKIPPED] == 1
    assert manifest.sources[1].records == 1 and records[0].raw == _eml(2)


def test_eml_file_count_cap(tmp_path: Path) -> None:
    d = tmp_path / "eml"
    d.mkdir()
    for i in range(4):
        (d / f"{i}.eml").write_bytes(_eml(i))
    manifest, _ = _run(tmp_path, [ImportSource("acct", d, SourceKind.EML)], max_eml_files=2)
    assert manifest.sources[0].status is SourceStatus.LIMIT_REACHED
    assert manifest.sources[0].status_code is IssueCode.EML_FILE_COUNT_EXCEEDED


# -- cancellation ------------------------------------------------------------------------


def test_cancel_stops_the_import(two_accounts: Any) -> None:
    root, sources = two_accounts
    got: list[ImportRecord] = []
    req = ImportRequest(frozenset({"gmail:a", "gmail:b"}), tuple(sources), dry_run=False)
    manifest = run_import(req, _policy(root), sink=got.append, cancel=lambda: len(got) >= 2)
    assert len(got) == 2
    assert manifest.sources[0].status is SourceStatus.CANCELLED
    assert len(manifest.sources) == 1


# -- manifest privacy and isolation ------------------------------------------------------


def test_manifest_contains_no_content_paths_or_identifiers(two_accounts: Any) -> None:
    root, sources = two_accounts
    manifest, records = _run(root, sources)
    text = json.dumps(manifest.to_dict())
    for needle in (SECRET_SUBJECT, SECRET_ADDR, SECRET_LABEL, "msg99@", str(root), "All mail"):
        assert needle not in text
    for record in records:
        assert SECRET_ADDR not in repr(record) and SECRET_LABEL not in repr(record)


def test_import_modules_are_not_wired_into_the_backend() -> None:
    """Only the 5.2 job may import the 5.1 importer; nothing imports either."""
    backend = Path(__file__).resolve().parents[2] / "backend"
    own = {"takeout_import.py", "takeout_job.py"}
    imports = re.compile(
        r"^\s*(?:from\s+\S*takeout_(?:import|job)\b|import\s+\S*takeout_(?:import|job)\b"
        r"|from\s+\S+\s+import\s+[^\n]*\btakeout_(?:import|job)\b)",
        re.MULTILINE,
    )
    offenders = [
        str(p.relative_to(backend))
        for p in backend.rglob("*.py")
        if p.name not in own and imports.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


@pytest.mark.parametrize("seed", range(25))
def test_random_bytes_never_raise(tmp_path: Path, seed: int) -> None:
    import random

    rng = random.Random(seed)
    chunks = [b"From x\n", b"\n", b">From y\n", b"\r\n", b"Subject: a\n", bytes(range(256))]
    blob = b"".join(rng.choice(chunks) + rng.randbytes(rng.randint(0, 300)) for _ in range(60))
    path = tmp_path / "fuzz.mbox"
    path.write_bytes(blob)
    manifest, records = _run(
        tmp_path, [ImportSource("acct", path, SourceKind.MBOX)], max_line_bytes=128
    )
    src = manifest.sources[0]
    assert src.status is SourceStatus.COMPLETED
    assert src.records == len(records) <= src.messages_seen


def test_allowed_root_behind_a_symlink_is_usable(tmp_path: Path) -> None:
    """e.g. macOS /var -> /private/var, or a linked home directory."""
    real_root = tmp_path / "real_root"
    real_root.mkdir()
    (real_root / "a.mbox").write_bytes(_mbox(_eml(1)))
    linked_root = tmp_path / "linked_root"
    linked_root.symlink_to(real_root)
    manifest, records = _run(
        linked_root, [ImportSource("acct", linked_root / "a.mbox", SourceKind.MBOX)]
    )
    assert manifest.sources[0].status is SourceStatus.COMPLETED and len(records) == 1


def test_symlink_below_a_linked_root_is_still_refused(tmp_path: Path) -> None:
    real_root = tmp_path / "real_root"
    (real_root / "sub").mkdir(parents=True)
    outside = tmp_path / "outside.mbox"
    outside.write_bytes(_mbox(_eml(1)))
    (real_root / "sub" / "a.mbox").symlink_to(outside)
    linked_root = tmp_path / "linked_root"
    linked_root.symlink_to(real_root)
    src = _refusal(linked_root, linked_root / "sub" / "a.mbox")
    assert src.status_code is IssueCode.PATH_SYMLINK


def test_unresolved_system_temp_path_works(tmp_path: Path) -> None:
    import tempfile

    unresolved = Path(tempfile.mkdtemp())  # /var/... on macOS (a symlink to /private/var)
    try:
        (unresolved / "a.mbox").write_bytes(_mbox(_eml(1)))
        manifest, records = _run(
            unresolved, [ImportSource("acct", unresolved / "a.mbox", SourceKind.MBOX)]
        )
        assert manifest.sources[0].status is SourceStatus.COMPLETED and len(records) == 1
    finally:
        import shutil

        shutil.rmtree(unresolved)
