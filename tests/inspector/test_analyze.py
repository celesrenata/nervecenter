"""Static deep inspection: planted structures are found, benign files stay quiet,
bounds give INCOMPLETE (never clean), and the parse child cannot open sockets."""
from __future__ import annotations

import json
import subprocess
import sys
import zlib

import pytest
import samples as S
from conftest import INSPECTOR_DIR
from nerve_inspector.analyze import CODES, INCOMPLETE_REASONS, Limits, analyze


def codes(data: bytes, **kw) -> set[str]:
    return set(analyze(data, **kw)["codes"])


# -- benign ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        S.pdf(),
        S.pdf(link_uri="https://shop.example.com/orders/1"),
        S.docx(),
        S.docx(hyperlink="https://docs.example.com/q4"),
        S.zip_of({"report.pdf": S.pdf(), "notes.txt": b"hello"}),
        b"\x89PNG\r\n\x1a\nnot inspected",
    ],
)
def test_benign_samples_have_no_codes_and_are_complete(data: bytes) -> None:
    rep = analyze(data)
    assert rep["codes"] == [] and rep["complete"] is True


def test_report_shape_is_closed() -> None:
    rep = analyze(S.pdf(js=True, link_uri="https://a.example.com/x"))
    assert set(rep) == {"v", "kind", "complete", "incomplete_reasons", "codes", "counts",
                        "link_hosts"}
    assert set(rep["codes"]) <= CODES and set(rep["incomplete_reasons"]) <= INCOMPLETE_REASONS


# -- PDF ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"js": True}, "PDF_JAVASCRIPT"),
        ({"js": True, "js_in_object_stream": True}, "PDF_JAVASCRIPT"),
        ({"launch": True}, "PDF_LAUNCH_ACTION"),
        ({"embedded": True}, "PDF_EMBEDDED_FILE"),
        ({"open_uri": "https://evil.example/x"}, "PDF_OPENACTION_URI"),
        ({"submit": True}, "PDF_REMOTE_ACTION"),
        ({"xfa": True}, "PDF_XFA_FORM"),
        ({"link_uri": "https://evil.example/x", "big_link": True}, "PDF_LARGE_LINK_AREA"),
    ],
)
def test_pdf_structures(kw: dict, code: str) -> None:
    assert code in codes(S.pdf(**kw))


def test_pdf_javascript_hidden_in_compressed_object_streams_is_found() -> None:
    data = S.pdf(js=True, js_in_object_stream=True)
    assert b"/JavaScript" not in data  # the old byte-prefix keyword scan cannot see it
    assert "PDF_JAVASCRIPT" in codes(data)


def test_pdf_link_hosts_are_hosts_only() -> None:
    rep = analyze(S.pdf(link_uri="https://Login.PayPa1.com/secure?u=victim@example.com"))
    assert rep["link_hosts"] == ["login.paypa1.com"]
    assert "victim" not in json.dumps(rep)


def test_password_protected_pdf_is_incomplete() -> None:
    rep = analyze(S.pdf(password="pw"))
    assert rep["complete"] is False and rep["incomplete_reasons"] == ["PASSWORD_PROTECTED"]


def test_garbage_pdf_is_incomplete_not_clean() -> None:
    rep = analyze(b"%PDF-1.7\n" + b"\x00garbage" * 50)
    assert rep["complete"] is False and rep["codes"] == []


# -- OOXML ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"macro": True}, "OOXML_MACRO_PROJECT"),
        ({"template": "https://tpl.example/t.dotm"}, "OOXML_EXTERNAL_TEMPLATE"),
        ({"dde": True}, "OOXML_DDE_FIELD"),
        ({"ole": True}, "OOXML_EMBEDDED_OLE"),
        ({"activex": True}, "OOXML_ACTIVEX"),
    ],
)
def test_ooxml_structures(kw: dict, code: str) -> None:
    rep = analyze(S.docx(**kw))
    assert rep["kind"] == "ooxml" and code in rep["codes"]


def test_ooxml_hyperlink_and_template_hosts() -> None:
    rep = analyze(S.docx(hyperlink="https://docs.example.com/a",
                         template="http://tpl.example.net/t.dotm"))
    assert sorted(rep["link_hosts"]) == ["docs.example.com", "tpl.example.net"]


def test_ooxml_entity_tricks_are_refused_not_expanded() -> None:
    rep = analyze(S.docx(xxe=True))
    assert rep["complete"] is False and "PARSE_FAILED" in rep["incomplete_reasons"]


# -- ZIP ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("members", "code"),
    [
        ({"invoice.exe": b"MZ"}, "ZIP_EXECUTABLE_ENTRY"),
        ({"invoice.pdf.exe": b"MZ"}, "ZIP_DOUBLE_EXTENSION"),
        ({"../../etc/x": b"x"}, "ZIP_PATH_TRAVERSAL"),
        ({"open-me.html": b"<html>"}, "ZIP_HTML_ENTRY"),
        ({"inner.zip": S.zip_of({"a.txt": b"a"})}, "ZIP_NESTED_ARCHIVE"),
    ],
)
def test_zip_structures(members: dict, code: str) -> None:
    assert code in codes(S.zip_of(members))


def test_zip_recurses_into_documents_and_archives() -> None:
    inner = S.zip_of({"doc.pdf": S.pdf(js=True)})
    rep = analyze(S.zip_of({"bundle.zip": inner}))
    assert {"ZIP_NESTED_ARCHIVE", "PDF_JAVASCRIPT"} <= set(rep["codes"])
    assert rep["counts"]["nested_inspected"] == 2


def test_zip_depth_is_bounded() -> None:
    deep = S.zip_of({"x.pdf": S.pdf(js=True)})
    for _ in range(3):
        deep = S.zip_of({"n.zip": deep})
    rep = analyze(deep)
    assert rep["complete"] is False and "DEPTH_EXCEEDED" in rep["incomplete_reasons"]


def test_encrypted_entry_is_flagged_and_incomplete() -> None:
    rep = analyze(S.encrypted_flag_zip())
    assert "ZIP_ENCRYPTED_ENTRY" in rep["codes"] and rep["complete"] is False


def test_zip_bomb_is_bounded() -> None:
    bomb = S.zip_of({"big.pdf": b"%PDF-" + b"\x00" * (40 * 1024 * 1024)})
    assert len(bomb) < 200_000 and zlib  # tiny on the wire
    rep = analyze(bomb, limits=Limits(max_member_bytes=8 * 1024 * 1024))
    assert rep["complete"] is False
    assert set(rep["incomplete_reasons"]) & {"TOO_LARGE", "BUDGET_EXCEEDED"}


def test_oversized_input_is_incomplete() -> None:
    rep = analyze(b"%PDF-" + b"x" * 100, limits=Limits(max_input_bytes=50))
    assert rep["incomplete_reasons"] == ["TOO_LARGE"]


def test_fuzzed_inputs_never_raise_and_never_invent_codes() -> None:
    import random

    rng = random.Random(7)
    seeds = [S.pdf(js=True), S.docx(macro=True), S.zip_of({"a.exe": b"MZ"})]
    for seed in seeds:
        for _ in range(40):
            data = bytearray(seed)
            for _ in range(rng.randint(1, 30)):
                data[rng.randrange(len(data))] = rng.randrange(256)
            rep = analyze(bytes(data))
            assert set(rep["codes"]) <= CODES
            assert set(rep["incomplete_reasons"]) <= INCOMPLETE_REASONS


# -- sandbox --------------------------------------------------------------------------


def _run_child(sample: bytes) -> dict:
    proc = subprocess.run(
        [sys.executable, "-s", "-B", "-m", "nerve_inspector.child"], input=sample,
        capture_output=True, timeout=60, env={"PYTHONPATH": str(INSPECTOR_DIR)},
    )
    return json.loads(proc.stdout)


@pytest.mark.parametrize(
    ("sample", "code"),
    [
        (S.pdf(js=True, js_in_object_stream=True), "PDF_JAVASCRIPT"),
        (S.docx(template="https://tpl.example/t.dotm"), "OOXML_EXTERNAL_TEMPLATE"),
        (S.docx(dde=True), "OOXML_DDE_FIELD"),
        (S.zip_of({"a.pdf.exe": b"MZ"}), "ZIP_DOUBLE_EXTENSION"),
        (S.zip_of({"in.zip": S.zip_of({"d.pdf": S.pdf(js=True)})}), "PDF_JAVASCRIPT"),
    ],
)
def test_sealed_child_still_parses_every_format(sample: bytes, code: str) -> None:
    # Regression: lazy imports (cp437 codec, zlib, expat) used to fail after sealing.
    rep = _run_child(sample)
    assert rep["sandbox"]["nofile"] is True
    assert code in rep["codes"] and rep["complete"] is True, rep


def test_sealed_child_cannot_open_a_socket_or_file() -> None:
    code = (
        "import socket, sys; from nerve_inspector import child; assert child._seal_descriptors()\n"
        "for f in (lambda: socket.socket(), lambda: open('/etc/hosts')):\n"
        "    try:\n        f(); print('OPENED'); sys.exit(1)\n"
        "    except OSError: pass\n"
        "print('SEALED')"
    )
    out = subprocess.run([sys.executable, "-s", "-B", "-c", code], capture_output=True,
                         text=True, timeout=60, env={"PYTHONPATH": str(INSPECTOR_DIR)})
    assert out.stdout.strip() == "SEALED"
