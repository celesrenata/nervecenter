#!/usr/bin/env python3
"""NerveCenter Mac handoff agent (NS-ADR-106, pull model). Standard library only.

Every ``NERVE_HANDOFF_POLL_SECONDS`` (default 60) it asks NerveCenter for one
queued Mac handoff (``POST /api/handoffs/device/claim``), runs it locally and
posts a short result (``POST /api/handoffs/device/result``). No inbound port.

Identity and token are the relay pairing (``nerve-personal-relay pair``):
``~/Library/Application Support/NerveCenterRelay/identity.json`` (deviceID,
baseURL) and the Keychain item service ``com.nervcenter.relay`` account
``relay-token`` (0600 ``credentials.json`` fallback). The token is never logged.

* opencode: ``opencode run --format json --title ... <brief>`` in
  ``$NERVE_HANDOFF_REPO_ROOT/<repo>`` (default ``~/sources/celesrenata``). A
  temporary ``OPENCODE_CONFIG`` denies ``git push`` and ``git remote`` unless the
  owner approved push for that handoff.
* menagerie: notify-only (a macOS notification with the title; the brief stays
  in NerveCenter). Menagerie's packages/ipc needs VS Code started with
  ``ROO_CODE_IPC_SOCKET_PATH``, which a launchd agent cannot rely on.
"""

from __future__ import annotations

import json
import os
import posixpath
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SUPPORT = Path.home() / "Library/Application Support/NerveCenterRelay"
IDENTITY = SUPPORT / "identity.json"
FALLBACK = SUPPORT / "credentials.json"
KEYCHAIN_SERVICE = "com.nervcenter.relay"
KEYCHAIN_ACCOUNT = "relay-token"
RUN_TIMEOUT_S = int(os.environ.get("NERVE_HANDOFF_RUN_TIMEOUT", "3600"))
MAX_SUMMARY = 7900

NO_PUSH = [
    {"action": "shell", "resource": "git push *", "effect": "deny"},
    {"action": "shell", "resource": "git remote *", "effect": "deny"},
]


def log(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}), flush=True)


def load_identity() -> tuple[str, str]:
    data = json.loads(IDENTITY.read_text())
    return data["deviceID"], data["baseURL"].rstrip("/")


def load_token() -> str:
    try:
        out = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                KEYCHAIN_ACCOUNT,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        if out.stdout.strip():
            return out.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return json.loads(FALLBACK.read_text())["token"]


def post(base: str, path: str, device: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Nerve-Device": device,
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https base from pairing
        return json.loads(resp.read() or b"{}")


def repo_dir(root: Path, repo: str | None) -> Path:
    if not repo:
        raise ValueError("handoff has no repo")
    joined = Path(posixpath.normpath(str(root / repo)))
    if root not in joined.parents and joined != root:
        raise ValueError("repo escapes the repo root")
    if not joined.is_dir():
        raise ValueError(f"repo not found locally: {repo}")
    return joined


def summarize_json_events(stdout: str) -> str:
    """Last assistant text from ``opencode run --format json`` output (NDJSON)."""
    texts: list[str] = []
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        text = ev.get("text") or (ev.get("part") or {}).get("text")
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    return texts[-1] if texts else stdout.strip()[-2000:]


def run_opencode(job: dict[str, Any], root: Path) -> tuple[bool, str]:
    cwd = repo_dir(root, job.get("repo"))
    perms = [{"action": "*", "resource": "*", "effect": "allow"}]
    if not job.get("push_approved"):
        perms += NO_PUSH
    brief = (
        job["brief"]
        + (
            "\n\nDo not push to any git remote; leave changes committed locally for owner review."
            if not job.get("push_approved")
            else ""
        )
        + "\nEnd with a short summary of what you changed and how you verified it."
    )
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as cfg:
        json.dump({"permissions": perms}, cfg)
    try:
        env = {**os.environ, "OPENCODE_CONFIG": cfg.name}
        proc = subprocess.run(
            ["opencode", "run", "--format", "json", "--title", job["title"][:120], brief],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_S,
        )
    finally:
        os.unlink(cfg.name)
    summary = summarize_json_events(proc.stdout)
    return proc.returncode == 0, summary[:MAX_SUMMARY] or f"opencode exited {proc.returncode}"


def notify_menagerie(job: dict[str, Any]) -> tuple[bool, str]:
    title = job["title"].replace('"', "'")[:100]
    subprocess.run(
        [
            "osascript",
            "-e",
            f'display notification "{title}" with title "NerveCenter: Menagerie handoff"',
        ],
        capture_output=True,
        timeout=10,
    )
    return (
        True,
        "Notified on the Mac (Menagerie is notify-only); open the Handoffs view for the brief.",
    )


def handle(job: dict[str, Any], root: Path) -> tuple[bool, str]:
    if job["target"] == "menagerie":
        return notify_menagerie(job)
    return run_opencode(job, root)


def once(base: str, device: str, token: str, root: Path) -> bool:
    job = post(base, "/api/handoffs/device/claim", device, token, {}).get("handoff")
    if not job:
        return False
    log("handoff.claimed", id=job["id"], target=job["target"])
    try:
        ok, summary = handle(job, root)
        error = None if ok else "run failed"
    except Exception as exc:  # noqa: BLE001 - reported back, agent keeps running
        ok, summary, error = False, "", f"{type(exc).__name__}: {exc}"[:500]
    post(
        base,
        "/api/handoffs/device/result",
        device,
        token,
        {"handoff_id": job["id"], "ok": ok, "summary": summary or None, "error": error},
    )
    log("handoff.finished", id=job["id"], ok=ok)
    return True


def main() -> int:
    poll = max(15, int(os.environ.get("NERVE_HANDOFF_POLL_SECONDS", "60")))
    root = Path(
        os.environ.get("NERVE_HANDOFF_REPO_ROOT", str(Path.home() / "sources/celesrenata"))
    ).resolve()
    device, base = load_identity()
    token = load_token()
    log("handoff_agent.start", device=device, poll_seconds=poll)
    while True:
        try:
            while once(base, device, token, root):
                pass
        except urllib.error.HTTPError as exc:
            log("handoff_agent.http_error", status=exc.code)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log("handoff_agent.unreachable", error=type(exc).__name__)
        if "--once" in sys.argv:
            return 0
        time.sleep(poll)


if __name__ == "__main__":
    raise SystemExit(main())
