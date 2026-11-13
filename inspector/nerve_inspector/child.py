"""One-shot parse child: sample on stdin, report JSON on stdout (NS-ADR-086).

Started per sample by :mod:`nerve_inspector.server` with CPU, memory and
file-size rlimits already applied. Before touching the sample it removes its
own ability to reach anything:

1. imports the parsers (the only files it ever needs to open);
2. reads the sample from stdin;
3. best-effort ``unshare(CLONE_NEWUSER | CLONE_NEWNET)`` into an empty network
   namespace (works only where the kernel/seccomp allow it; recorded in the
   report as ``sandbox.netns``);
4. closes every descriptor above 2 and sets ``RLIMIT_NOFILE`` to 3, so no new
   socket, file or pipe can be created at all: networking is impossible even
   without the namespace (recorded as ``sandbox.nofile``).

Then it parses and writes one JSON object. Any failure is reported as
``INTERNAL_ERROR``; text from exceptions never leaves.
"""
from __future__ import annotations

import ctypes
import json
import os
import resource
import sys

_CLONE_NEWUSER = 0x10000000
_CLONE_NEWNET = 0x40000000


def _try_unshare_net() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.unshare(_CLONE_NEWUSER | _CLONE_NEWNET) == 0
    except Exception:  # noqa: BLE001
        return False


def _seal_descriptors() -> bool:
    try:
        os.closerange(3, 65536)
        resource.setrlimit(resource.RLIMIT_NOFILE, (3, 3))
    except (OSError, ValueError):
        return False
    return True


def _warm_up(az: object) -> None:
    """Trigger every lazy import (codecs, compressors, expat, qpdf) by running
    each format once on a tiny built-in sample, BEFORE descriptors are sealed;
    afterwards no module can be loaded from disk."""
    import importlib
    import io
    import zipfile

    import pikepdf

    for mod in ("encodings.cp437", "encodings.utf_8", "encodings.latin_1", "encodings.idna",
                "encodings.ascii", "unicodedata", "stringprep", "zlib", "bz2", "lzma",
                "pyexpat", "xml.etree.ElementTree", "defusedxml.ElementTree"):
        try:
            importlib.import_module(mod)
        except ImportError:
            pass
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/_rels/document.xml.rels",
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                    'relationships"/>')
        zf.writestr("word/document.xml", "<d/>")
    pdf = io.BytesIO()
    doc = pikepdf.new()
    doc.add_blank_page()
    doc.save(pdf)
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("x.pdf", pdf.getvalue())
    for sample in (buf.getvalue(), pdf.getvalue(), inner.getvalue()):
        az.analyze(sample)  # type: ignore[attr-defined]
    "https://warm.example/".encode("idna")


def main() -> int:
    # (1) load everything the parse needs while files can still be opened.
    from nerve_inspector import analyze as az

    _warm_up(az)

    limit = az.DEFAULT_LIMITS.max_input_bytes + 1
    data = sys.stdin.buffer.read(limit)  # (2)
    netns = _try_unshare_net()  # (3)
    nofile = _seal_descriptors()  # (4)
    if not nofile:
        report = {"v": az.REPORT_VERSION, "kind": "not_applicable", "complete": False,
                  "incomplete_reasons": ["INTERNAL_ERROR"], "codes": [], "counts": {},
                  "link_hosts": []}
    else:
        report = az.analyze(data)
    report["sandbox"] = {"netns": netns, "nofile": nofile}
    sys.stdout.write(json.dumps(report, separators=(",", ":")))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - never echo exception text
        sys.stdout.write('{"v":1,"kind":"not_applicable","complete":false,'
                         '"incomplete_reasons":["INTERNAL_ERROR"],"codes":[],"counts":{},'
                         '"link_hosts":[]}')
        sys.exit(0)
