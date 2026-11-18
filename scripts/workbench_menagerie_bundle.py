#!/usr/bin/env python3
"""Publish the Owner's Menagerie settings and skills to IDE pods.

Builds ConfigMap ``workbench-menagerie-settings`` in namespace ``workbench``:

* ``settings.json``: the Menagerie export (provider profiles + global settings)
  with every API key replaced by ``__OMNIROUTE_API_KEY__`` and every OmniRoute
  base URL by ``__OMNIROUTE_BASE_URL__``. The IDE entrypoint substitutes the
  pod's own key and the in-cluster URL, so no key is stored in the ConfigMap.
* ``home.tgz``: ``~/.roo`` (rules, skills, commands, references) and
  ``~/.agents`` (skillhub skills), extracted into the pod's home at start.

Codebase indexing is turned off: its Qdrant and embedder run on RFC 1918
addresses that the IDE NetworkPolicy blocks. Local ``/Users/...`` paths are
dropped. Run again after changing settings or skills; new IDE pods pick it up.

Usage: scripts/workbench_menagerie_bundle.py [--profiles F] [--settings F] [--dry-run]
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

HOME = Path.home()
KEY = "__OMNIROUTE_API_KEY__"
BASE = "__OMNIROUTE_BASE_URL__"
OMNI_HOSTS = ("omniroute.celestium.life", "omniroute.omniroute.svc")
SKIP_SETTINGS = {"lastSettingsExportPath"}
EXCLUDE_NAMES = {".DS_Store", "cli-debug.log"}
MAX_BYTES = 900_000  # ConfigMap limit is 1 MiB


def _scrub_profile(cfg: dict) -> dict:
    out = {}
    for k, v in cfg.items():
        if "apikey" in k.lower() or "api_key" in k.lower():
            out[k] = KEY if v else v
        elif (
            isinstance(v, str)
            and k.lower().endswith("baseurl")
            and any(h in v for h in OMNI_HOSTS)
        ):
            out[k] = BASE
        else:
            out[k] = v
    return out


def build_settings(profiles_path: Path, settings_path: Path | None) -> dict:
    newest = json.loads(profiles_path.read_text())
    base = json.loads(settings_path.read_text()) if settings_path and settings_path.exists() else {}
    gs = {**(base.get("globalSettings") or {}), **(newest.get("globalSettings") or {})}
    for k in SKIP_SETTINGS:
        gs.pop(k, None)
    if isinstance(gs.get("allowedCommands"), list):
        gs["allowedCommands"] = [c for c in gs["allowedCommands"] if "/Users/" not in c]
    idx = gs.get("codebaseIndexConfig")
    if isinstance(idx, dict):
        idx = dict(idx, codebaseIndexEnabled=False)
        for k, v in list(idx.items()):
            if isinstance(v, str) and any(h in v for h in OMNI_HOSTS):
                idx[k] = BASE
        gs["codebaseIndexConfig"] = idx
    pp = newest["providerProfiles"]
    pp = dict(pp, apiConfigs={n: _scrub_profile(c) for n, c in pp["apiConfigs"].items()})
    doc = {"providerProfiles": pp, "globalSettings": gs}
    text = json.dumps(doc)
    if re.search(r"\bsk-[A-Za-z0-9_-]{16,}", text) or "/Users/" in text:
        raise SystemExit("refusing: settings still contain a key-like value or a local path")
    return doc


VSCODE_SETTINGS = HOME / "Library/Application Support/Code/User/settings.json"


def build_vscode_settings(path: Path = VSCODE_SETTINGS) -> dict:
    """The ``menagerie.*`` editor settings, minus local paths and auto-import."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for k, v in raw.items():
        if not k.startswith("menagerie.") or k == "menagerie.autoImportSettingsPath":
            continue
        if isinstance(v, list):
            v = [x for x in v if "/Users/" not in json.dumps(x)]
        elif "/Users/" in json.dumps(v):
            continue
        out[k] = v
    return out


def build_home_tgz() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in (".roo", ".agents"):
            root = HOME / name
            if not root.is_dir():
                continue
            for p in sorted(root.rglob("*")):
                if p.name in EXCLUDE_NAMES or p.is_symlink() or not p.is_file():
                    continue
                info = tar.gettarinfo(str(p), arcname=str(p.relative_to(HOME)))
                info.uid = info.gid = 1000
                info.uname = info.gname = ""
                info.mode = 0o644
                with p.open("rb") as fh:
                    tar.addfile(info, fh)
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--profiles", type=Path, default=HOME / ".config/zoo-code/omniroute-zoo-profiles.json"
    )
    ap.add_argument("--settings", type=Path, default=HOME / "Downloads/zoo-code-settings.json")
    ap.add_argument("--namespace", default="workbench")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    settings = json.dumps(build_settings(a.profiles, a.settings), indent=2)
    tgz = build_home_tgz()
    if len(settings) + len(tgz) * 4 // 3 > MAX_BYTES:
        raise SystemExit("bundle too large for a ConfigMap")
    cm = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "workbench-menagerie-settings", "namespace": a.namespace},
        "data": {
            "settings.json": settings,
            "vscode-settings.json": json.dumps(build_vscode_settings(), indent=2),
        },
        "binaryData": {"home.tgz": base64.b64encode(tgz).decode()},
    }
    print(f"settings.json {len(settings)} bytes, home.tgz {len(tgz)} bytes", file=sys.stderr)
    if a.dry_run:
        return 0
    subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(cm).encode(), check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
