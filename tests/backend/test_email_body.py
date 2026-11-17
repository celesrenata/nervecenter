"""Email body decoding (backend.email.body) and ingest wiring."""
from __future__ import annotations

import base64

import backend.api.app  # noqa: F401 - import-order guard
from backend.attention.service import AttentionService
from backend.email.body import body_from_parts, decode_body, html_to_text, looks_like_html
from backend.email.service import EmailService

HTML = "<html><head><style>p{color:red}</style></head><body><p>Hello <b>world</b></p><p>Second</p><script>x()</script></body></html>"


def test_html_to_text_drops_script_style_and_keeps_paragraphs() -> None:
    text = html_to_text(HTML)
    assert text == "Hello world\n\nSecond"


def test_looks_like_html_ignores_prose() -> None:
    assert looks_like_html(HTML)
    assert not looks_like_html("1 < 2 and 3 > 2, plain words")
    assert not looks_like_html("see <b> here")


def test_plain_body_unchanged() -> None:
    out = decode_body("Just words.\nSecond line.")
    assert out.html is None and out.text == "Just words.\nSecond line."


def test_html_mislabelled_as_plain_part_becomes_html() -> None:
    out = body_from_parts([("plain", HTML)])
    assert out.html is not None and "<" not in out.text and "Hello world" in out.text


def test_bare_html_body() -> None:
    out = decode_body(HTML)
    assert out.html == HTML and out.text.startswith("Hello world")


def test_multipart_alternative_prefers_html_and_keeps_plain_text() -> None:
    raw = (
        "From: a@example.test\r\nMIME-Version: 1.0\r\n"
        'Content-Type: multipart/alternative; boundary="b"\r\n\r\n'
        "--b\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nPlain version\r\n"
        "--b\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>Rich version</p>\r\n--b--\r\n"
    )
    out = decode_body(raw)
    assert out.text == "Plain version"
    assert out.html is not None and "Rich version" in out.html


def test_quoted_printable_and_charset_in_mime() -> None:
    raw = (
        "MIME-Version: 1.0\r\nContent-Type: text/html; charset=iso-8859-1\r\n"
        "Content-Transfer-Encoding: quoted-printable\r\n\r\n"
        "<p>Caf=E9 =3D soft=\r\nbreak</p>\r\n"
    )
    out = decode_body(raw)
    assert out.html is not None and "Café = softbreak" in out.html
    assert "Café = softbreak" in out.text


def test_base64_with_separate_hints() -> None:
    encoded = base64.b64encode("<p>Grüße</p>".encode("utf-8")).decode()
    out = decode_body(
        encoded, content_type="text/html", transfer_encoding="base64", charset="utf-8"
    )
    assert out.html == "<p>Grüße</p>\n" or out.html == "<p>Grüße</p>"
    assert out.text == "Grüße"


def test_quoted_printable_with_separate_hints_and_latin1() -> None:
    out = decode_body(
        "Caf=E9", content_type="text/plain", transfer_encoding="quoted-printable",
        charset="iso-8859-1",
    )
    assert out.text == "Café" and out.html is None


def test_unknown_charset_and_hostile_hints_do_not_raise() -> None:
    out = decode_body("Hi", content_type="text/plain\r\nX-Evil: 1", charset="nope")
    assert out.text == "Hi"
    out = decode_body("Hi", content_type="text/plain", charset="no-such-charset")
    assert "Hi" in out.text


def test_attachments_are_not_part_of_the_body() -> None:
    raw = (
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="m"\r\n\r\n'
        "--m\r\nContent-Type: text/plain\r\n\r\nBody text\r\n"
        "--m\r\nContent-Type: text/plain\r\nContent-Disposition: attachment; filename=a.txt\r\n\r\nSECRET\r\n--m--\r\n"
    )
    out = decode_body(raw)
    assert out.text == "Body text"


def test_ingest_decodes_html_and_exposes_both_views() -> None:
    svc = EmailService(attention=AttentionService())
    res = svc.ingest(
        {
            "message_id": "m1",
            "from_address": "a@example.test",
            "subject": "s",
            "body": HTML,
            "timestamp": "2026-11-01T10:00:00Z",
        },
        generate_attention=False,
    )
    msg = res["message"]
    assert msg.body_html == HTML and "<" not in msg.body
    assert "<" not in res["thread"].last_body


def test_ingest_plain_is_untouched() -> None:
    svc = EmailService(attention=AttentionService())
    res = svc.ingest(
        {"message_id": "m2", "from_address": "a@example.test", "subject": "s",
         "body": "  keep  spacing\n", "timestamp": "2026-11-01T10:00:00Z"},
        generate_attention=False,
    )
    assert res["message"].body == "  keep  spacing\n" and res["message"].body_html is None
