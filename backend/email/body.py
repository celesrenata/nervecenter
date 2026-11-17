"""Decode an email body into a readable text view and an HTML view.

The Email screen used to show whatever string reached it. Mail often arrives
as HTML (sometimes mislabelled ``text/plain``), as a raw MIME message, or with
quoted-printable / base64 / non-UTF-8 charsets still applied, and the result
was unreadable markup. This module turns any of those into:

- ``text``: always readable plain text (HTML converted, paragraphs kept);
- ``html``: the raw (still hostile) HTML document when the mail has one, else
  ``None``. It is NOT safe to render as is: the web app sanitises it and shows
  it only inside a sandboxed iframe.

Pure functions, no I/O, never raise.
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from typing import Any, Iterable

TEXT_CAP = 20_000
HTML_CAP = 400_000

_TAG_HINT = re.compile(
    r"<\s*/?\s*(?:!doctype\s+html|html|head|body|div|p|br|table|tr|td|span|a|img|"
    r"style|font|center|h[1-6]|ul|ol|li|b|i|u|strong|em|blockquote|section|meta)\b[^<>]{0,500}>",
    re.IGNORECASE,
)
_DOC_HINT = re.compile(r"<\s*(?:!doctype\s+html|html|body)\b", re.IGNORECASE)
_HEADER_LINE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,40}:[ \t]")
_SAFE_HEADER_VALUE = re.compile(r"^[A-Za-z0-9/+.;= \"'_-]{1,120}$")


@dataclass(frozen=True)
class EmailBody:
    text: str
    html: str | None = None


def looks_like_html(value: str) -> bool:
    """True when ``value`` is HTML rather than prose that mentions a tag."""
    if not value:
        return False
    if _DOC_HINT.search(value):
        return True
    return len(_TAG_HINT.findall(value[:200_000])) >= 2


def looks_like_mime(value: str) -> bool:
    """A raw RFC 822 / MIME message: header block with Content-Type or MIME-Version."""
    head = value.lstrip("\r\n")[:20_000]
    block = re.split(r"\r?\n\r?\n", head, maxsplit=1)[0]
    lines = block.splitlines()
    if len(lines) < 2 or not _HEADER_LINE.match(lines[0]):
        return False
    names = {ln.split(":", 1)[0].strip().lower() for ln in lines if _HEADER_LINE.match(ln)}
    return bool(names & {"content-type", "mime-version", "content-transfer-encoding"})


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "head", "title", "template", "noscript"}
    _BLOCK = {
        "p", "div", "tr", "table", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
        "blockquote", "section", "article", "header", "footer", "pre", "hr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP:
            self._skip += 1
        elif tag == "br":
            self.out.append("\n")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in self._BLOCK:
            self.out.append("\n\n" if tag in {"p", "h1", "h2", "h3", "h4", "h5", "h6"} else "\n")
        elif tag in {"td", "th"}:
            self.out.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.out.append(data)


def html_to_text(value: str) -> str:
    """Readable text from HTML: scripts/styles dropped, paragraphs kept."""
    parser = _TextExtractor()
    try:
        parser.feed(value[:HTML_CAP])
        parser.close()
        raw = "".join(parser.out)
    except Exception:  # noqa: BLE001 - hostile markup must never break the feed
        raw = _html.unescape(re.sub(r"<[^<>]{0,2000}>", " ", value[:HTML_CAP]))
    raw = raw.replace("\xa0", " ").replace("\u200b", "")
    lines = [" ".join(line.split()) for line in raw.splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text[:TEXT_CAP]


def _combine(plain: Iterable[str], html_parts: Iterable[str]) -> EmailBody:
    docs = [h for h in html_parts if h.strip()]
    prose: list[str] = []
    for part in plain:
        if not part.strip():
            continue
        if looks_like_html(part):
            docs.append(part)  # HTML mislabelled as text/plain
        else:
            prose.append(part)
    html_doc = "\n".join(docs)[:HTML_CAP] if docs else None
    if prose:
        text = "\n\n".join(p.strip() for p in prose)[:TEXT_CAP]
    elif html_doc:
        text = html_to_text(html_doc)
    else:
        text = ""
    return EmailBody(text=text, html=html_doc)


def body_from_parts(parts: Iterable[tuple[str, str]]) -> EmailBody:
    """Combine already-decoded ``(subtype, text)`` parts ("plain" | "html")."""
    items = list(parts)
    return _combine(
        [t for s, t in items if s == "plain"], [t for s, t in items if s == "html"]
    )


def _decode_leaf(part: Any) -> str:
    try:
        return str(part.get_content())
    except Exception:  # noqa: BLE001 - unknown charset or malformed encoding
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")


def _collect(part: Any, plain: list[str], html_parts: list[str], depth: int = 0) -> None:
    if depth > 12:
        return
    if part.is_multipart():
        children = [c for c in part.get_payload() if hasattr(c, "get_content_type")]
        for child in children:
            _collect(child, plain, html_parts, depth + 1)
        return
    if (part.get_content_disposition() or "").lower() == "attachment" or part.get_filename():
        return
    ctype = part.get_content_type()
    if ctype == "text/plain":
        plain.append(_decode_leaf(part))
    elif ctype == "text/html":
        html_parts.append(_decode_leaf(part))


def _parse_mime(raw: bytes) -> EmailBody:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    plain: list[str] = []
    html_parts: list[str] = []
    _collect(message, plain, html_parts)
    return _combine(plain, html_parts)


def decode_body(
    raw: str | None,
    *,
    content_type: str | None = None,
    transfer_encoding: str | None = None,
    charset: str | None = None,
) -> EmailBody:
    """Best-effort decode of a posted body. Never raises.

    ``raw`` may be a full MIME message, a bare HTML or plain body, or a body
    whose transfer encoding / charset is given separately.
    """
    value = raw or ""
    try:
        has_hints = bool(content_type or transfer_encoding)
        if has_hints and not looks_like_mime(value):
            ctype = (content_type or "text/plain").strip()
            if charset:
                ctype = f"{ctype}; charset={charset.strip()}"
            headers = [f"Content-Type: {ctype}"]
            if transfer_encoding:
                headers.append(f"Content-Transfer-Encoding: {transfer_encoding.strip()}")
            if all(_SAFE_HEADER_VALUE.match(h.split(": ", 1)[1]) for h in headers):
                value = "MIME-Version: 1.0\r\n" + "\r\n".join(headers) + "\r\n\r\n" + value
        if looks_like_mime(value):
            return _parse_mime(value.encode("utf-8", "surrogateescape"))
        return _combine([] if looks_like_html(value) else [value], [value] if looks_like_html(value) else [])
    except Exception:  # noqa: BLE001 - never break ingestion over a body
        return EmailBody(text=(raw or "")[:TEXT_CAP], html=None)


__all__ = [
    "EmailBody",
    "body_from_parts",
    "decode_body",
    "html_to_text",
    "looks_like_html",
    "looks_like_mime",
]
