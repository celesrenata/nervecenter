"""Sidecar server + api client end to end, hostile sidecar replies, and the
attachment-stage wiring (NS-ADR-086)."""
from __future__ import annotations

import asyncio
import struct
import tempfile
from pathlib import Path

import pytest
import samples as S
from conftest import INSPECTOR_DIR

from backend.api.models import MailConnector  # noqa: F401 - import-order guard
from backend.mail_security.attachment_wiring import collect_attachment_results
from backend.mail_security.parser import parse_mime_evidence
from backend.mail_security.static_inspection import (
    CODE_DEEP_INCOMPLETE,
    CODE_DEEP_NOT_RUN,
    StaticInspector,
    deep_inspect,
    inspector_from_env,
    wants_deep_inspection,
)


@pytest.fixture
def sock_dir():
    # macOS caps AF_UNIX paths at ~104 bytes; keep it short.
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="nsi") as d:
        yield Path(d)


@pytest.fixture
async def server(sock_dir, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(INSPECTOR_DIR))
    from nerve_inspector import server as srv

    path = str(sock_dir / "i.sock")
    task = asyncio.create_task(srv.serve(path))
    for _ in range(100):
        if Path(path).exists():
            break
        await asyncio.sleep(0.02)
    yield path
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_end_to_end_findings_and_doc_link_deception(server) -> None:
    ref = "part:attachment:0"
    res = await deep_inspect(StaticInspector(server),
                             ref, S.pdf(js=True, link_uri="https://paypa1.com/login"))
    codes = {f.explanation_code for f in res.findings}
    assert {"PDF_JAVASCRIPT", "ASCII_BRAND_TYPOSQUAT"} <= codes and not res.incomplete
    js = next(f for f in res.findings if f.explanation_code == "PDF_JAVASCRIPT")
    assert js.severity.value == "HIGH" and js.category.value == "ATTACHMENT"
    assert js.evidence_refs == [ref]


async def test_end_to_end_incomplete_is_never_clean(server) -> None:
    res = await deep_inspect(StaticInspector(server), "part:attachment:1",
                             S.pdf(password="pw"))
    assert res.incomplete
    assert {f.explanation_code for f in res.findings} == {"DEEP_INSPECTION_PASSWORD_PROTECTED"}


async def test_wall_clock_kill_reports_timeout(server, monkeypatch) -> None:
    from nerve_inspector import server as srv

    monkeypatch.setattr(srv, "WALL_SECONDS", 0.001)
    res = await deep_inspect(StaticInspector(server), "part:attachment:0", S.pdf())
    assert res.incomplete
    assert {f.explanation_code for f in res.findings} == {"DEEP_INSPECTION_TIMEOUT"}


async def test_unreachable_sidecar_is_incomplete(sock_dir) -> None:
    res = await deep_inspect(StaticInspector(str(sock_dir / "missing.sock")),
                             "part:attachment:0", S.pdf())
    assert res.incomplete
    assert [f.explanation_code for f in res.findings] == ["DEEP_INSPECTION_UNAVAILABLE"]


@pytest.mark.parametrize(
    "reply",
    [
        b'{"v":1,"kind":"pdf","complete":true,"incomplete_reasons":[],"codes":["SAFE"],'
        b'"counts":{},"link_hosts":[]}',
        b'{"v":1,"kind":"pdf","complete":true,"incomplete_reasons":[],"codes":[],'
        b'"counts":{},"link_hosts":["evil.example/<script>"]}',
        b'{"v":1,"kind":"pdf","complete":true,"incomplete_reasons":[],"codes":[],'
        b'"counts":{},"link_hosts":[],"verdict":"clean"}',
        b'{"v":2,"kind":"pdf","complete":true,"incomplete_reasons":[],"codes":[],'
        b'"counts":{},"link_hosts":[]}',
        b"not json",
    ],
)
async def test_hostile_sidecar_replies_become_incomplete(sock_dir, reply: bytes) -> None:
    path = str(sock_dir / "h.sock")

    async def handle(reader, writer):
        await reader.readexactly(8)
        writer.write(struct.pack(">I", len(reply)) + reply)
        await writer.drain()
        writer.close()

    srv = await asyncio.start_unix_server(handle, path=path)
    async with srv:
        res = await deep_inspect(StaticInspector(path), "part:attachment:0", b"%PDF-1.4")
    assert res.incomplete
    assert [f.explanation_code for f in res.findings] == ["DEEP_INSPECTION_UNAVAILABLE"]


@pytest.mark.parametrize("bad", ["", "relative.sock", "/run/x/../y.sock", "/run/x/no-suffix",
                                 "tcp://127.0.0.1:1"])
def test_only_absolute_socket_paths_are_accepted(bad: str) -> None:
    assert inspector_from_env({"NERVE_MAIL_SECURITY_STATIC_INSPECTION_SOCKET": bad}) is None


def test_kill_switch_defaults_off() -> None:
    assert inspector_from_env({}) is None
    ok = inspector_from_env({"NERVE_MAIL_SECURITY_STATIC_INSPECTION_SOCKET":
                             "/run/inspector/inspector.sock"})
    assert ok is not None and ok.socket_path == "/run/inspector/inspector.sock"


def test_only_pdf_and_zip_families_are_sent() -> None:
    assert wants_deep_inspection(S.pdf()) and wants_deep_inspection(S.docx())
    assert not wants_deep_inspection(b"\x89PNG\r\n\x1a\n") and not wants_deep_inspection(None)


# -- attachment stage wiring ------------------------------------------------------------


def _mime(payload: bytes, ctype: str = "application/pdf", name: str = "a.pdf") -> bytes:
    import base64

    b64 = base64.encodebytes(payload).decode()
    return (
        "From: a@example.com\r\nSubject: s\r\nMIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="b"\r\n\r\n--b\r\n'
        "Content-Type: text/plain\r\n\r\nhi\r\n--b\r\n"
        f'Content-Type: {ctype}\r\nContent-Transfer-Encoding: base64\r\n'
        f'Content-Disposition: attachment; filename="{name}"\r\n\r\n{b64}\r\n--b--\r\n'
    ).encode()


class FakeInspector:
    def __init__(self, inner=None):
        self.calls = 0
        self.inner = inner

    async def inspect(self, sample: bytes):
        self.calls += 1
        from nerve_inspector.analyze import analyze

        from backend.mail_security.static_inspection import DeepReport

        return DeepReport.model_validate(analyze(sample))


async def test_stage_adds_deep_findings_and_clears_payloads() -> None:
    ev = parse_mime_evidence(_mime(S.docx(template="https://tpl.example/t.dotm"),
                                   "application/octet-stream", "q.docx"),
                             retain_attachment_payloads=True)
    insp = FakeInspector()
    res = await collect_attachment_results(ev, scanner=None, deep_inspector=insp)  # type: ignore[arg-type]
    assert insp.calls == 1
    assert "OOXML_EXTERNAL_TEMPLATE" in {f.explanation_code for f in res.findings}
    assert CODE_DEEP_NOT_RUN not in res.checks_not_run
    assert all(d.payload is None for d in ev.attachments)


async def test_stage_without_inspector_records_not_run_only_for_documents() -> None:
    pdf_ev = parse_mime_evidence(_mime(S.pdf()), retain_attachment_payloads=True)
    res = await collect_attachment_results(pdf_ev, scanner=None)
    assert CODE_DEEP_NOT_RUN in res.checks_not_run
    png_ev = parse_mime_evidence(_mime(b"\x89PNG\r\n\x1a\nxx", "image/png", "a.png"),
                                 retain_attachment_payloads=True)
    res2 = await collect_attachment_results(png_ev, scanner=None)
    assert CODE_DEEP_NOT_RUN not in res2.checks_not_run


async def test_stage_incomplete_report_marks_assessment_incomplete() -> None:
    ev = parse_mime_evidence(_mime(S.pdf(password="pw")), retain_attachment_payloads=True)
    res = await collect_attachment_results(ev, scanner=None, deep_inspector=FakeInspector())  # type: ignore[arg-type]
    assert CODE_DEEP_INCOMPLETE in res.incomplete_codes
