"""Static deep inspection of one attachment (NS-ADR-086). Pure; never executes.

Input: the attachment's bytes. Output: a closed report of finding codes,
counts and link hosts. Nothing else leaves: no filenames, member names, text,
URLs (only their host), or exception text.

Formats: PDF (pikepdf/qpdf object model, so compressed object streams and
``#xx``-escaped names are seen as the reader would see them), OOXML (zip +
``defusedxml``), and ZIP (stdlib ``zipfile``, recursing into members of
interest up to :data:`MAX_DEPTH`). Anything else is ``NOT_APPLICABLE``.

Every read is bounded (:class:`Limits`). Exceeding a bound sets ``complete``
false with a typed reason; it never yields a clean report.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

REPORT_VERSION = 1

#: Closed vocabulary. The api rejects any other code.
CODES = frozenset(
    {
        # PDF
        "PDF_JAVASCRIPT",
        "PDF_LAUNCH_ACTION",
        "PDF_EMBEDDED_FILE",
        "PDF_OPENACTION_URI",
        "PDF_REMOTE_ACTION",
        "PDF_RICH_MEDIA",
        "PDF_XFA_FORM",
        "PDF_LARGE_LINK_AREA",
        # OOXML
        "OOXML_MACRO_PROJECT",
        "OOXML_EXTERNAL_TEMPLATE",
        "OOXML_EXTERNAL_OBJECT",
        "OOXML_EMBEDDED_OLE",
        "OOXML_DDE_FIELD",
        "OOXML_ACTIVEX",
        # ZIP
        "ZIP_ENCRYPTED_ENTRY",
        "ZIP_EXECUTABLE_ENTRY",
        "ZIP_DOUBLE_EXTENSION",
        "ZIP_PATH_TRAVERSAL",
        "ZIP_NESTED_ARCHIVE",
        "ZIP_HTML_ENTRY",
    }
)
INCOMPLETE_REASONS = frozenset(
    {
        "TOO_LARGE",
        "PARSE_FAILED",
        "PASSWORD_PROTECTED",
        "DEPTH_EXCEEDED",
        "BUDGET_EXCEEDED",
        "OBJECT_CAP_EXCEEDED",
        "ENTRY_CAP_EXCEEDED",
        "TIMEOUT",
        "RESOURCE_LIMIT",
        "INTERNAL_ERROR",
    }
)
KINDS = frozenset({"pdf", "ooxml", "zip", "not_applicable"})
MAX_DEPTH = 2

_EXEC_EXTS = frozenset(
    {
        "exe", "scr", "com", "bat", "cmd", "js", "jse", "vbs", "vbe", "wsf", "wsh",
        "hta", "jar", "ps1", "msi", "lnk", "pif", "cpl", "dll", "reg", "sh", "app",
        "iso", "img", "apk", "msc", "inf", "scf", "msix", "appx", "url",
    }
)  # fmt: skip
_DOC_EXTS = frozenset(
    {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "rtf", "jpg", "jpeg", "png"}
)
_ARCHIVE_EXTS = frozenset({"zip", "rar", "7z", "gz", "tgz", "tar", "cab", "jar", "iso"})
_HTML_EXTS = frozenset({"html", "htm", "shtml", "xhtml", "svg", "mht", "mhtml"})
_HOST_RE = re.compile(r"^[a-z0-9.-]{1,253}$")
_DDE_RE = re.compile(rb"\bDDE(?:AUTO)?\b", re.IGNORECASE)


@dataclass(frozen=True)
class Limits:
    max_input_bytes: int = 16 * 1024 * 1024
    max_total_decompressed: int = 64 * 1024 * 1024
    max_member_bytes: int = 16 * 1024 * 1024
    max_xml_bytes: int = 8 * 1024 * 1024
    max_ratio: int = 200
    max_zip_entries: int = 2000
    max_pdf_objects: int = 50_000
    max_link_hosts: int = 64


DEFAULT_LIMITS = Limits()


class _Budget(Exception):
    """A bound was hit; carries the typed incomplete reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Report:
    kind: str = "not_applicable"
    complete: bool = True
    incomplete_reasons: set[str] = field(default_factory=set)
    codes: set[str] = field(default_factory=set)
    counts: dict[str, int] = field(default_factory=dict)
    link_hosts: list[str] = field(default_factory=list)
    _decompressed: int = 0

    def incomplete(self, reason: str) -> None:
        self.complete = False
        self.incomplete_reasons.add(reason)

    def count(self, name: str, n: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + n

    def add_url(self, url: object, limits: Limits) -> None:
        host = _host_of(url)
        if host and host not in self.link_hosts and len(self.link_hosts) < limits.max_link_hosts:
            self.link_hosts.append(host)

    def as_dict(self) -> dict[str, Any]:
        return {
            "v": REPORT_VERSION,
            "kind": self.kind,
            "complete": self.complete,
            "incomplete_reasons": sorted(self.incomplete_reasons),
            "codes": sorted(self.codes),
            "counts": dict(sorted(self.counts.items())),
            "link_hosts": list(self.link_hosts),
        }


def _host_of(url: object) -> str | None:
    if isinstance(url, bytes):
        url = url.decode("latin-1", "replace")
    if not isinstance(url, str):
        return None
    url = url.strip()[:2048]
    if not re.match(r"^(?i:https?)://", url):
        return None
    try:
        host = (urlsplit(url).hostname or "").rstrip(".").lower()
    except ValueError:
        return None
    try:
        host = host.encode("idna").decode("ascii") if host and not host.isascii() else host
    except UnicodeError:
        return None
    return host if host and _HOST_RE.fullmatch(host) and "." in host else None


def _sniff(data: bytes) -> str:
    if data.startswith(b"%PDF-") or b"%PDF-" in data[:1024]:
        return "pdf"
    if data.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return "zip"
    return "not_applicable"


def analyze(data: bytes, limits: Limits = DEFAULT_LIMITS) -> dict[str, Any]:
    """Analyze one attachment; always returns a report dict (never raises)."""
    rep = Report()
    if len(data) > limits.max_input_bytes:
        rep.kind = _sniff(data[:1024])
        rep.incomplete("TOO_LARGE")
        return rep.as_dict()
    try:
        _dispatch(data, rep, limits, depth=0, top=True)
    except _Budget as exc:
        rep.incomplete(exc.reason)
    except MemoryError:
        rep.incomplete("RESOURCE_LIMIT")
    except Exception:  # noqa: BLE001 - parser failure is incomplete, never clean
        rep.incomplete("INTERNAL_ERROR")
    return rep.as_dict()


def _dispatch(data: bytes, rep: Report, limits: Limits, *, depth: int, top: bool) -> None:
    kind = _sniff(data)
    if kind == "zip":
        kind = "ooxml" if _is_ooxml(data) else "zip"
    if top:
        rep.kind = kind
    if kind == "pdf":
        _pdf(data, rep, limits)
    elif kind == "ooxml":
        _ooxml(data, rep, limits)
    elif kind == "zip":
        _zip(data, rep, limits, depth=depth)


# -- ZIP ------------------------------------------------------------------------------


def _open_zip(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, OSError, EOFError):
        raise _Budget("PARSE_FAILED") from None


def _is_ooxml(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return "[Content_Types].xml" in zf.namelist()
    except Exception:  # noqa: BLE001
        return False


def _read_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, rep: Report, limits: Limits,
                 cap: int) -> bytes:
    """Read one member with absolute, ratio and total-decompressed caps."""
    if info.flag_bits & 0x1:
        raise _Budget("PASSWORD_PROTECTED")
    if info.file_size > cap:
        raise _Budget("TOO_LARGE")
    if info.file_size > limits.max_ratio * max(info.compress_size, 1) and info.file_size > 1 << 20:
        raise _Budget("BUDGET_EXCEEDED")
    out = bytearray()
    try:
        with zf.open(info) as fh:
            while True:
                chunk = fh.read(1 << 16)
                if not chunk:
                    break
                out += chunk
                rep._decompressed += len(chunk)
                if len(out) > cap:
                    raise _Budget("TOO_LARGE")
                if rep._decompressed > limits.max_total_decompressed:
                    raise _Budget("BUDGET_EXCEEDED")
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError, EOFError,
            ValueError):
        raise _Budget("PARSE_FAILED") from None
    return bytes(out)


def _ext_chain(name: str) -> list[str]:
    base = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
    parts = [p.strip() for p in base.split(".")]
    return parts[1:] if len(parts) > 1 else []


def _zip(data: bytes, rep: Report, limits: Limits, *, depth: int) -> None:
    with _open_zip(data) as zf:
        infos = zf.infolist()
        rep.count("zip_entries", len(infos))
        if len(infos) > limits.max_zip_entries:
            rep.incomplete("ENTRY_CAP_EXCEEDED")
            infos = infos[: limits.max_zip_entries]
        for info in infos:
            name = info.filename
            low = name.replace("\\", "/").lower()
            chain = _ext_chain(low)
            ext = chain[-1] if chain else ""
            if info.flag_bits & 0x1:
                rep.codes.add("ZIP_ENCRYPTED_ENTRY")
                rep.incomplete("PASSWORD_PROTECTED")
            if ".." in low.split("/") or low.startswith("/") or re.match(r"^[a-z]:", low):
                rep.codes.add("ZIP_PATH_TRAVERSAL")
            if ext in _EXEC_EXTS:
                rep.codes.add("ZIP_EXECUTABLE_ENTRY")
                if len(chain) >= 2 and chain[-2] in _DOC_EXTS:
                    rep.codes.add("ZIP_DOUBLE_EXTENSION")
            if ext in _HTML_EXTS:
                rep.codes.add("ZIP_HTML_ENTRY")
            if info.is_dir() or info.flag_bits & 0x1:
                continue
            interesting = ext in _ARCHIVE_EXTS | {"pdf", "docx", "docm", "xlsx", "xlsm",
                                                    "pptx", "pptm", "dotm", "xlam"}
            if ext in _ARCHIVE_EXTS:
                rep.codes.add("ZIP_NESTED_ARCHIVE")
            if not interesting:
                continue
            if depth + 1 > MAX_DEPTH:
                rep.incomplete("DEPTH_EXCEEDED")
                continue
            try:
                inner = _read_member(zf, info, rep, limits, limits.max_member_bytes)
            except _Budget as exc:
                rep.incomplete(exc.reason)
                continue
            rep.count("nested_inspected")
            try:
                _dispatch(inner, rep, limits, depth=depth + 1, top=False)
            except _Budget as exc:
                rep.incomplete(exc.reason)


# -- OOXML ----------------------------------------------------------------------------

_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"


def _parse_xml(blob: bytes) -> Any:
    from defusedxml import ElementTree as SafeET  # no DTDs, entities or external refs

    try:
        return SafeET.fromstring(blob)
    except Exception:  # noqa: BLE001 - defusedxml raises on any forbidden construct
        raise _Budget("PARSE_FAILED") from None


def _ooxml(data: bytes, rep: Report, limits: Limits) -> None:
    with _open_zip(data) as zf:
        infos = zf.infolist()
        rep.count("ooxml_parts", len(infos))
        if len(infos) > limits.max_zip_entries:
            raise _Budget("ENTRY_CAP_EXCEEDED")
        for info in infos:
            low = info.filename.replace("\\", "/").lower()
            if info.flag_bits & 0x1:
                rep.incomplete("PASSWORD_PROTECTED")
                continue
            if low.endswith("vbaproject.bin") or "/macrosheets/" in low:
                rep.codes.add("OOXML_MACRO_PROJECT")
            if "/activex/" in low:
                rep.codes.add("OOXML_ACTIVEX")
            chain = _ext_chain(low)
            if "/embeddings/" in low and chain and chain[-1] in _EXEC_EXTS | {"bin"}:
                rep.codes.add("OOXML_EMBEDDED_OLE")
            if low.endswith(".rels"):
                _rels(_read_member(zf, info, rep, limits, limits.max_xml_bytes), rep, limits)
            elif low.endswith(".xml") and (
                low.startswith(("word/document", "word/header", "word/footer"))
                or low.startswith("xl/externallinks/")
            ):
                blob = _read_member(zf, info, rep, limits, limits.max_xml_bytes)
                _parse_xml(blob)  # well-formed and free of DTD/entity tricks
                if low.startswith("xl/externallinks/") and b"ddeLink" in blob:
                    rep.codes.add("OOXML_DDE_FIELD")
                elif (b"instrText" in blob or b"fldSimple" in blob) and _DDE_RE.search(blob):
                    rep.codes.add("OOXML_DDE_FIELD")


def _rels(blob: bytes, rep: Report, limits: Limits) -> None:
    root = _parse_xml(blob)
    for rel in root.iter(_REL_NS):
        rtype = (rel.get("Type") or "").rsplit("/", 1)[-1].lower()
        external = (rel.get("TargetMode") or "").lower() == "external"
        target = rel.get("Target") or ""
        if not external:
            continue
        rep.count("external_relationships")
        if rtype == "hyperlink":
            rep.add_url(target, limits)
        elif rtype == "attachedtemplate":
            rep.codes.add("OOXML_EXTERNAL_TEMPLATE")
            rep.add_url(target, limits)
        elif rtype in {"oleobject", "frame", "subdocument", "externallinkpath", "package"}:
            rep.codes.add("OOXML_EXTERNAL_OBJECT")
            rep.add_url(target, limits)
        else:
            rep.add_url(target, limits)


# -- PDF ------------------------------------------------------------------------------

_REMOTE_ACTIONS = frozenset({"/GoToR", "/GoToE", "/ImportData", "/SubmitForm"})


def _name(obj: Any) -> str:
    try:
        return str(obj) if obj is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def _action(action: Any, rep: Report, limits: Limits, *, open_action: bool) -> None:
    import pikepdf

    if not isinstance(action, pikepdf.Dictionary):
        return
    kind = _name(action.get("/S"))
    if kind == "/JavaScript" or "/JS" in action:
        rep.codes.add("PDF_JAVASCRIPT")
    elif kind == "/Launch":
        rep.codes.add("PDF_LAUNCH_ACTION")
    elif kind in _REMOTE_ACTIONS:
        rep.codes.add("PDF_REMOTE_ACTION")
    elif kind == "/URI":
        rep.add_url(_pdf_string(action.get("/URI")), limits)
        if open_action:
            rep.codes.add("PDF_OPENACTION_URI")
    elif kind == "/RichMediaExecute":
        rep.codes.add("PDF_RICH_MEDIA")


def _pdf_string(obj: Any) -> str | None:
    import pikepdf

    if isinstance(obj, pikepdf.String):
        try:
            return bytes(obj).decode("latin-1", "replace")
        except Exception:  # noqa: BLE001
            return None
    return None


def _rect_area(rect: Any) -> float:
    try:
        x0, y0, x1, y1 = (float(v) for v in list(rect)[:4])
    except Exception:  # noqa: BLE001
        return 0.0
    return abs(x1 - x0) * abs(y1 - y0)


def _pdf_dicts(pdf: Any, limits: Limits) -> Any:
    """Every dictionary in the file: each indirect object plus the DIRECT
    dictionaries nested inside it (an action or form dictionary need not be
    its own object). Indirect references are not followed here; they are
    visited as top-level objects. Bounded by ``max_pdf_objects`` nodes."""
    import pikepdf

    budget = limits.max_pdf_objects * 4
    for top in pdf.objects:
        stack = [top.stream_dict if isinstance(top, pikepdf.Stream) else top]
        while stack:
            budget -= 1
            if budget < 0:
                raise _Budget("OBJECT_CAP_EXCEEDED")  # codes found so far are kept
            node = stack.pop()
            if isinstance(node, pikepdf.Dictionary):
                yield node
                children = list(node.values())
            elif isinstance(node, pikepdf.Array):
                children = list(node)
            else:
                continue
            for child in children:
                if isinstance(child, pikepdf.Dictionary | pikepdf.Array) and not child.is_indirect:
                    stack.append(child)


def _pdf(data: bytes, rep: Report, limits: Limits) -> None:
    import pikepdf

    try:
        pdf = pikepdf.open(io.BytesIO(data))
    except pikepdf.PasswordError:
        raise _Budget("PASSWORD_PROTECTED") from None
    except Exception:  # noqa: BLE001 - qpdf could not make sense of it
        raise _Budget("PARSE_FAILED") from None
    with pdf:
        objects = 0
        for obj in _pdf_dicts(pdf, limits):
            objects += 1
            if objects > limits.max_pdf_objects:
                rep.incomplete("OBJECT_CAP_EXCEEDED")
                break
            typ = _name(obj.get("/Type"))
            if typ in ("/EmbeddedFile", "/Filespec") and ("/EF" in obj or typ == "/EmbeddedFile"):
                rep.codes.add("PDF_EMBEDDED_FILE")
            if "/JS" in obj or "/JavaScript" in obj:
                rep.codes.add("PDF_JAVASCRIPT")
            if "/RichMedia" in obj or _name(obj.get("/Subtype")) == "/RichMedia":
                rep.codes.add("PDF_RICH_MEDIA")
            if "/XFA" in obj:
                rep.codes.add("PDF_XFA_FORM")
            if "/S" in obj:
                _action(obj, rep, limits, open_action=False)
        rep.count("pdf_objects", objects)
        root = pdf.Root
        if "/OpenAction" in root:
            _action(root.get("/OpenAction"), rep, limits, open_action=True)
        pages = 0
        for page in pdf.pages:
            pages += 1
            if pages > 500:
                rep.incomplete("OBJECT_CAP_EXCEEDED")
                break
            page_area = _rect_area(page.obj.get("/MediaBox")) or 1.0
            link_area = 0.0
            for annot in page.obj.get("/Annots") or []:
                if not isinstance(annot, pikepdf.Dictionary):
                    continue
                if _name(annot.get("/Subtype")) != "/Link":
                    continue
                rep.count("pdf_link_annotations")
                action = annot.get("/A")
                if isinstance(action, pikepdf.Dictionary) and _name(action.get("/S")) == "/URI":
                    rep.add_url(_pdf_string(action.get("/URI")), limits)
                    link_area += _rect_area(annot.get("/Rect"))
            if link_area / page_area >= 0.25:
                rep.codes.add("PDF_LARGE_LINK_AREA")
        rep.count("pdf_pages", pages)


__all__ = ["CODES", "DEFAULT_LIMITS", "INCOMPLETE_REASONS", "KINDS", "Limits", "analyze"]
