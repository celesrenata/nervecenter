#!/usr/bin/env python3
"""Generate the NerveCenter self-host download manifest.

`manifest.json` describes the exported Apple artifacts so the web
`DownloadsView` and any install tool can locate and present them without
hardcoding paths or versions.

The script scans a destination directory (where the artifacts have been
copied by `export_app.sh` / fastlane `publish`) and emits a JSON manifest
of the form:

    {
      "name": "NerveCenter",
      "generated_at": "<UTC ISO-8601>",
      "versions": { "phone": "1.0", "watch": "1.0", "widget": "1.0" },
      "artifacts": [
        { "name": "NervePhone.ipa", "kind": "ios",
          "path": "NervePhone.ipa", "sha256": "...", "bytes": 123,
          "min_ios": "17.0" },
        { "name": "NerveWatch.app", "kind": "watch",
          "path": "NerveWatch.app", "sha256": "...", "bytes": 456,
          "min_watch": "10.0" },
        { "name": "NerveWatchWidgetExtension.watchkitarchive",
          "kind": "watch-widget",
          "path": "NerveWatchWidgetExtension.watchkitarchive",
          "sha256": "...", "bytes": 789 }
      ]
    }

Usage:
    python3 generate_manifest.py --destination web/dist/downloads \
        [--artifact-root build/artifacts] [--version 1.0] [--name NerveCenter]

The `--destination` directory must already exist (it is where `manifest.json`
is written). When `--artifact-root` is provided, the artifact contents
(sha256 / byte counts) are read from that directory; otherwise they are read
from `--destination`. Missing artifacts are recorded in `artifacts` with
`present: false` so the UI can show a "not built yet" state.
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


# Canonical artifact roster. The build always produces these (in this order).
ARTIFACTS: List[Dict[str, Any]] = [
    {
        "name": "NervePhone.ipa",
        "kind": "ios",
        "label": "NervePhone (iPhone / iPad)",
        "min_ios": "17.0",
        "bundle_id": "com.celesrenata.NervePhone",
        "embedded": False,
    },
    {
        "name": "NerveWatch.app",
        "kind": "watch",
        "label": "NerveWatch (watchOS)",
        "min_watch": "10.0",
        "bundle_id": "com.celesrenata.nervewatch",
        "embedded": False,
    },
    {
        "name": "NerveWatchWidgetExtension.watchkitarchive",
        "kind": "watch-widget",
        "label": "NerveWatch widget",
        "bundle_id": "com.celesrenata.nervewatch.widgets",
        "embedded": False,
    },
    {
        # The embedded iOS widget ships inside NervePhone.ipa; we do not
        # expose it as a separate downloadable file.
        "name": "NerveCenterWidgetExtension.appex",
        "kind": "ios-widget",
        "label": "NerveCenter widget (embedded in app)",
        "bundle_id": "com.celesrenata.NervePhone.widgets",
        "embedded": True,
    },
]


def sha256_of(directory: str, name: str) -> Tuple[Optional[str], Optional[int]]:
    """Return (sha256_hex, byte_count) for a file or directory.

    For a directory we hash a deterministic concatenation of all regular
    file contents (sorted by relative path) so the digest is reproducible.
    """
    path = os.path.join(directory, name)
    if not os.path.exists(path):
        return None, None

    if os.path.isfile(path):
        h = hashlib.sha256()
        size = 0
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
                size += len(chunk)
        return h.hexdigest(), size

    # Directory: deterministic aggregation.
    h = hashlib.sha256()
    size = 0
    for root, _dirs, files in os.walk(path):
        for fn in sorted(files):
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, path)
            h.update(rel.encode("utf-8"))
            with open(fp, "rb") as f:
                data = f.read()
                h.update(data)
                size += len(data)
    return h.hexdigest(), size


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--destination", required=True,
                    help="Directory where manifest.json is written and, by "
                         "default, where the exported artifacts live.")
    ap.add_argument("--artifact-root", default=None,
                    help="Optional directory containing the source artifacts. "
                         "When provided, this directory is scanned for the "
                         "sha256 / byte count of each artifact (the manifest "
                         "is still written to --destination). Defaults to "
                         "--destination when omitted.")
    ap.add_argument("--version", default="1.0",
                    help="Version string for the app(s).")
    ap.add_argument("--name", default="NerveCenter",
                    help="Product name recorded in the manifest.")
    args = ap.parse_args()

    destination = os.path.abspath(args.destination)
    if not os.path.isdir(destination):
        print(f"error: destination does not exist: {destination}", file=sys.stderr)
        return 2

    # Directory scanned for artifact contents: the artifact-root when supplied,
    # otherwise the destination.
    if args.artifact_root:
        scan_root = os.path.abspath(args.artifact_root)
        if not os.path.isdir(scan_root):
            print(f"error: artifact-root does not exist: {scan_root}", file=sys.stderr)
            return 2
    else:
        scan_root = destination

    entries: List[Dict[str, Any]] = []
    for meta in ARTIFACTS:
        digest, size = sha256_of(scan_root, str(meta["name"]))
        entry: Dict[str, Any] = {
            "name": meta["name"],
            "kind": meta["kind"],
            "label": meta["label"],
            "path": meta["name"],
            "present": digest is not None,
            "embedded": bool(meta.get("embedded", False)),
            "bundle_id": meta["bundle_id"],
        }
        # Version / platform constraints.
        if "min_ios" in meta:
            entry["min_ios"] = meta["min_ios"]
        if "min_watch" in meta:
            entry["min_watch"] = meta["min_watch"]
        if digest is not None:
            entry["sha256"] = digest
            entry["bytes"] = size
        entries.append(entry)

    manifest: Dict[str, Any] = {
        "name": args.name,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "versions": {"phone": args.version, "watch": args.version, "widget": args.version},
        "artifacts": entries,
    }

    out = os.path.join(destination, "manifest.json")
    with open(out, "w", encoding="utf-8") as f:
        f.write(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
