"""Task 2.4 tests: static attachment inspection + scanner interface.

Synthetic fixtures only (structure, no real malware). Covers R-003, R-005,
R-022, I-02, I-08, T-07, T-08, T-18.
"""

from __future__ import annotations

import ast
import asyncio
import io
import logging
import random
import struct
import time
import zipfile
from pathlib import Path

import pytest

from backend.mail_security import attachments as att
from backend.mail_security.attachments import (
    AttachmentLimits,
    NullAttachmentScanner,
    ScanOutcome,
    ScanStatus,
    inspect_attachment,
    run_scan,
    safe_filename_form,
    summarize_scans,
)
from backend.mail_security.models import FindingStatus
from backend.mail_security.parser import AttachmentDescriptor

CANARY = "CANARYZXQ9"


def desc(
    filename: str = "file.bin",
    *,
    size: int = 100,
    double: bool = False,
    mismatch: bool = False,
    sniffed: str | None = None,
    archive: bool = False,
) -> AttachmentDescriptor:
    return AttachmentDescriptor(
        evidence_ref="part:attachment:0",
        filename=filename[:64],
        filename_raw=filename,
        filename_truncated=False,
        claimed_content_type="application/octet-stream",
        size_bytes=size,
        sha256=None,
        sniffed_type=sniffed,
        double_extension=double,
        magic_mismatch=mismatch,
        is_archive=archive,
    )


def rules(result) -> set[str]:
    return {f.rule_id for f in result.findings}


def codes(result) -> set[str]:
    return {f.explanation_code for f in result.findings}


def run(name: str, data: bytes, **kw):
    return inspect_attachment(desc(name, size=len(data), **kw), data)


def make_zip(entries, *, comment: bytes = b"") -> bytes:
    """Hand-built zip: entries are (name, comp_size, uncomp_size, flags).

    Local headers carry no member data, so declared sizes can be enormous
    without any inflation being possible or attempted.
    """
    body = b""
    central = b""
    for name, comp, uncomp, flags in entries:
        nb = name.encode()
        offset = len(body)
        body += (
            b"PK\x03\x04"
            + struct.pack("<HHHHHIIIHH", 20, flags, 8, 0, 0, 0, comp, uncomp, len(nb), 0)
            + nb
        )
        central += (
            b"PK\x01\x02"
            + struct.pack(
                "<HHHHHHIIIHHHHHII",
                20,
                20,
                flags,
                8,
                0,
                0,
                0,
                comp,
                uncomp,
                len(nb),
                0,
                0,
                0,
                0,
                0,
                offset,
            )
            + nb
        )
    eocd = (
        b"PK\x05\x06"
        + struct.pack(
            "<HHHHIIH", 0, 0, len(entries), len(entries), len(central), len(body), len(comment)
        )
        + comment
    )
    return body + central + eocd


def real_zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in members.items():
            z.writestr(n, d)
    return buf.getvalue()


# ----------------------------- static checks ----------------------------------


def test_double_extension_by_name_and_descriptor():
    assert att.RULE_DOUBLE_EXTENSION in rules(inspect_attachment(desc("invoice.pdf.exe")))
    assert att.RULE_DOUBLE_EXTENSION in rules(inspect_attachment(desc("x", double=True)))


def test_extension_sniff_mismatch():
    r = run("photo.png", b"%PDF-1.4\n%%EOF")
    assert att.RULE_TYPE_MISMATCH in rules(r)


def test_executable_disguised_as_document():
    r = run("report.pdf", b"MZ" + b"\x00" * 64)
    assert att.RULE_EXECUTABLE_DISGUISED in rules(r)
    r = run("notes.txt", b"\x7fELF" + b"\x00" * 64)
    assert att.RULE_EXECUTABLE_DISGUISED in rules(r)


def test_dangerous_extension_flagged():
    assert att.RULE_DANGEROUS_TYPE in rules(inspect_attachment(desc("run.js")))


def test_ooxml_macro_via_central_directory_only():
    z = make_zip([("[Content_Types].xml", 10, 10, 0), ("word/vbaProject.bin", 10, 10, 0)])
    r = run("doc.docm", z)
    assert att.RULE_MACRO_CAPABLE in rules(r)
    r2 = run("doc.docx", z)
    f = next(f for f in r2.findings if f.rule_id == att.RULE_MACRO_CAPABLE)
    assert f.severity.value == "HIGH"  # macro hidden behind non-macro extension


def test_zip_bomb_indicator_without_inflation(monkeypatch):
    # Members declare multi-GiB sizes but carry NO data: inflating is impossible.
    z = make_zip([(f"f{i}.bin", 10, 4_000_000_000, 0) for i in range(5)])
    assert len(z) < 2000

    def boom(*a, **k):  # any decompression/zipfile use would fail the test
        raise AssertionError("decompression attempted")

    monkeypatch.setattr(zipfile, "ZipFile", boom)
    import zlib

    monkeypatch.setattr(zlib, "decompress", boom)
    monkeypatch.setattr(zlib, "decompressobj", boom)
    start = time.perf_counter()
    r = run("data.zip", z)
    assert time.perf_counter() - start < 1.0
    assert att.RULE_ARCHIVE_BOMB in rules(r)


def test_encrypted_zip_flag():
    z = make_zip([("secret.txt", 10, 10, 0x1)])
    assert att.RULE_ENCRYPTED in rules(run("a.zip", z))


def test_nested_archive_listed_not_opened(monkeypatch):
    inner = real_zip({"x.txt": b"hi"})
    outer = real_zip({"inner.zip": inner})
    monkeypatch.setattr(
        zipfile, "ZipFile", lambda *a, **k: (_ for _ in ()).throw(AssertionError("opened"))
    )
    r = run("outer.zip", outer)
    assert att.RULE_ARCHIVE_NESTED in rules(r)


def test_archive_traversal_and_executable_member():
    z = make_zip([("../../etc/x", 5, 5, 0), ("a/setup.exe", 5, 5, 0)])
    r = run("pack.zip", z)
    assert {att.RULE_ARCHIVE_TRAVERSAL, att.RULE_ARCHIVE_EXECUTABLE} <= rules(r)


def test_oversized_is_too_large_incomplete():
    r = inspect_attachment(
        desc("big.pdf", size=10_000), b"%PDF-" + b"A" * 100, limits=AttachmentLimits(max_bytes=1000)
    )
    f = next(f for f in r.findings if f.rule_id == att.RULE_SCAN_INCOMPLETE)
    assert f.status is FindingStatus.INCOMPLETE and f.explanation_code == "ATTACHMENT_TOO_LARGE"
    assert r.incomplete and not r.payload_inspected


def test_entry_cap_is_incomplete():
    z = make_zip([(f"f{i}.txt", 1, 1, 0) for i in range(30)])
    r = inspect_attachment(
        desc("a.zip", size=len(z)), z, limits=AttachmentLimits(max_zip_entries=10)
    )
    assert r.incomplete and "ZIP_ENTRY_CAP_EXCEEDED" in codes(r)


def test_prefix_only_zip_is_incomplete_not_clean():
    z = real_zip({"a.txt": b"x" * 100})
    r = inspect_attachment(desc("a.zip", size=len(z) + 500), z[:20])
    assert r.incomplete


@pytest.mark.parametrize(
    "name,data",
    [
        ("a.pdf", b"%PDF-1.7\n1 0 obj<</Type/Catalog>>endobj\n%%EOF"),
        ("a.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32),
        ("a.txt", b"hello world\n"),
        ("a.docx", real_zip({"[Content_Types].xml": b"<x/>", "word/document.xml": b"<w/>"})),
        ("a.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 16),
    ],
)
def test_benign_no_false_positives(name, data):
    r = run(name, data)
    assert r.findings == [] and not r.incomplete


def test_pdf_active_content_keywords():
    r = run("a.pdf", b"%PDF-1.4\n<</OpenAction<</S/JavaScript/JS(x)>>>>")
    assert att.RULE_PDF_ACTIVE in rules(r)
    r = run("a.pdf", b"%PDF-1.4\n<</Encrypt 5 0 R>>")
    assert att.RULE_ENCRYPTED in rules(r)


def test_filename_rtl_and_control_sanitized_and_capped():
    nasty = "invoice\u202efdp.exe\x00\x07" + CANARY + "A" * 500
    r = inspect_attachment(desc(nasty))
    assert att.RULE_FILENAME_SPOOF in rules(r)
    safe = safe_filename_form(nasty)
    assert len(safe) <= 64
    assert all(c.isalnum() or c in "._-" for c in safe) and "\u202e" not in safe
    assert CANARY not in repr(r.findings)


# ----------------------------- scanner interface -------------------------------


class FakeScanner:
    def __init__(self, outcome=None, exc=None, hang=False):
        self.outcome, self.exc, self.hang, self.calls = outcome, exc, hang, 0

    async def scan(self, payload, *, deadline_seconds):
        self.calls += 1
        if self.hang:
            await asyncio.Event().wait()
        if self.exc:
            raise self.exc
        return self.outcome


REF = "part:attachment:0"


async def go(scanner, payload=b"data", **kw):
    return await run_scan(scanner, payload, evidence_ref=REF, **kw)


async def test_detected_yields_finding():
    s = FakeScanner(ScanOutcome(ScanStatus.DETECTED, "fake", "1.0", ("Sig.Test-1", "bad sig/!")))
    rec = await go(s)
    assert rec.finding.rule_id == att.RULE_SCANNER_DETECTION
    assert rec.finding.severity.value == "HIGH" and not rec.clean_by_engine
    assert rec.signature_ids == ("Sig.Test-1", "bad_sig__")


@pytest.mark.parametrize(
    "scanner,kw,status",
    [
        (FakeScanner(ScanOutcome(ScanStatus.ERROR, "fake", "1")), {}, ScanStatus.ERROR),
        (FakeScanner(ScanOutcome(ScanStatus.TIMEOUT, "fake", "1")), {}, ScanStatus.TIMEOUT),
        (FakeScanner(exc=TimeoutError()), {}, ScanStatus.TIMEOUT),
        (FakeScanner(hang=True), {"timeout_seconds": 0.001}, ScanStatus.TIMEOUT),
        (FakeScanner(ScanOutcome(ScanStatus.UNAVAILABLE, "fake", "1")), {}, ScanStatus.UNAVAILABLE),
        (FakeScanner(ScanOutcome(ScanStatus.NOT_RUN, "fake", "1")), {}, ScanStatus.NOT_RUN),
        (FakeScanner(ScanOutcome(ScanStatus.TOO_LARGE, "fake", "1")), {}, ScanStatus.TOO_LARGE),
        (NullAttachmentScanner(), {}, ScanStatus.NOT_RUN),
        (FakeScanner(exc=RuntimeError(CANARY)), {}, ScanStatus.ERROR),
        (FakeScanner("not-an-outcome"), {}, ScanStatus.ERROR),
        (
            FakeScanner(ScanOutcome(ScanStatus.CLEAN_ENGINE_VERDICT, "fake", "")),
            {},
            ScanStatus.ERROR,
        ),
        (FakeScanner(ScanOutcome(ScanStatus.CLEAN_ENGINE_VERDICT, "", "1")), {}, ScanStatus.ERROR),
    ],
)
async def test_non_clean_outcomes_are_incomplete_never_clean(scanner, kw, status):
    rec = await go(scanner, **kw)
    assert rec.status is status
    assert rec.finding.rule_id == att.RULE_SCAN_INCOMPLETE
    assert rec.finding.status is FindingStatus.INCOMPLETE
    assert rec.incomplete and not rec.clean_by_engine
    summary = summarize_scans([rec])
    assert not summary.complete and summary.engine_clean == 0
    assert CANARY not in repr(rec)


async def test_cap_and_missing_payload_never_call_scanner():
    s = FakeScanner(ScanOutcome(ScanStatus.CLEAN_ENGINE_VERDICT, "fake", "1"))
    big = await go(s, b"x" * 50, max_bytes=10)
    none = await go(s, None)
    assert big.status is ScanStatus.TOO_LARGE and none.status is ScanStatus.NOT_RUN
    assert s.calls == 0


async def test_engine_clean_recorded_with_version_not_safe():
    s = FakeScanner(ScanOutcome(ScanStatus.CLEAN_ENGINE_VERDICT, "fakeav", "9.9/sig-2026"))
    rec = await go(s)
    assert rec.clean_by_engine and not rec.incomplete
    assert (rec.engine, rec.engine_version) == ("fakeav", "9.9_sig-2026")
    assert rec.finding.detector_version == "fakeav@9.9_sig-2026"
    assert rec.finding.explanation_code == "ENGINE_CLEAN_VERDICT_NOT_SAFETY"
    assert rec.finding.severity.value == "INFO"
    assert not hasattr(rec, "safe") and not hasattr(summarize_scans([rec]), "safe")


async def test_canary_in_bytes_and_filename_never_leaks(caplog):
    caplog.set_level(logging.DEBUG)
    payload = CANARY.encode() + b"%PDF-" + b"/JavaScript"
    d = desc(f"{CANARY}.pdf.exe", size=len(payload))
    res = inspect_attachment(d, payload)
    s = FakeScanner(ScanOutcome(ScanStatus.DETECTED, "fake", "1", ("Sig",)))
    rec = await go(s, payload)
    blob = (
        repr(res)
        + repr(rec)
        + "".join(f.model_dump_json() for f in res.findings)
        + rec.finding.model_dump_json()
    )
    assert CANARY not in blob and CANARY not in caplog.text


def test_scanner_protocol_accepts_no_url_and_null_is_protocol():
    import inspect

    assert isinstance(NullAttachmentScanner(), att.AttachmentScanner)
    params = set(inspect.signature(att.AttachmentScanner.scan).parameters)
    assert params == {"self", "payload", "deadline_seconds"}


def test_module_imports_no_network_or_subprocess():
    src = Path(att.__file__).read_text()
    tree = ast.parse(src)
    banned = {
        "socket",
        "http",
        "urllib",
        "requests",
        "httpx",
        "aiohttp",
        "subprocess",
        "os",
        "ssl",
        "zipfile",
        "tarfile",
        "ctypes",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not {a.name.split(".")[0] for a in node.names} & banned
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in banned
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            called.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    assert not called & {
        "system",
        "extract",
        "extractall",
        "Popen",
        "run_command",
        "open",
        "decompress",
        "decompressobj",
        "eval",
        "exec",
        "ZipFile",
    }


def test_malformed_container_fuzz_terminates():
    rng = random.Random(1234)
    base = real_zip({"a.txt": b"hello", "word/vbaProject.bin": b"x"})
    seeds = [
        base,
        make_zip([("a", 1, 1, 0)]),
        b"PK\x05\x06" + b"\xff" * 18,
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64,
    ]
    start = time.perf_counter()
    for _ in range(400):
        b = bytearray(rng.choice(seeds))
        for _ in range(rng.randint(1, 8)):
            if b:
                b[rng.randrange(len(b))] = rng.randrange(256)
        data = bytes(b[: rng.randint(0, len(b))] if rng.random() < 0.3 else b)
        r = inspect_attachment(desc("x.zip", size=len(data)), data)
        assert isinstance(r.findings, list)
    assert time.perf_counter() - start < 10


def test_ole_vba_indicator():
    data = (
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 16 + "VBA".encode("utf-16-le") + b"\x00" * 8
    )
    assert att.RULE_MACRO_CAPABLE in rules(run("a.doc", data))
