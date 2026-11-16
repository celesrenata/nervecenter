"""Owner tool: upload the NerveCenter agent skill pack to the Skill Hub (NS-ADR-112).

Subcommands (run from a checkout with ``kubectl`` access)::

    python -m backend.owner_tools.skillhub_upload plan [--full]
    python -m backend.owner_tools.skillhub_upload apply --approved
    python -m backend.owner_tools.skillhub_upload refresh
    python -m backend.owner_tools.skillhub_upload verify

``plan`` is read-only: it prints the NFS export, the single writable subPath,
the file list and the rendered Job, and writes nothing. ``apply`` refuses
without ``--approved``; it creates ConfigMap ``nervecenter-skillpack`` and Job
``nervecenter-skillpack-upload`` in namespace ``nervecenter``, and the Job
writes only ``nervecenter/<slug>/SKILL.md`` on the share. ``refresh`` asks
OmniRoute to rescan the catalog and ``verify`` checks the new skills are listed
under domain ``nervecenter``. OmniRoute caches the catalog per replica, so both
talk to every replica through ``kubectl port-forward`` (read-only access to the
omniroute namespace; nothing there is changed); without kubectl access, or with
``NERVE_SKILLHUB_API_URL`` set, they use that one URL. The privileged key comes from
``NERVE_OMNIROUTE_PRIVILEGED_KEY`` or the cluster Secret and is never printed.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

import httpx

from backend.agents.skillpack import DOMAIN, MANIFEST_FILE, build_pack, manifest_text

NFS_SERVER = "192.168.42.8"
NFS_PATH = "/volume1/Kubernetes/omniroute/skills"
SUB_PATH = DOMAIN
NAMESPACE = "nervecenter"
CONFIGMAP = "nervecenter-skillpack"
JOB = "nervecenter-skillpack-upload"
DEFAULT_IMAGE = "registry.celestium.life/nervecenter/api:latest"
DEFAULT_API_URL = "https://omniroute.celestium.life"
KEY_ENV = "NERVE_OMNIROUTE_PRIVILEGED_KEY"
KEY_SECRET = "nervecenter-omniroute-privileged"
OMNIROUTE_NAMESPACE = "omniroute"
OMNIROUTE_SELECTOR = "app.kubernetes.io/name=omniroute"
OMNIROUTE_PORT = 20128
TEMPLATE_DIR = (
    Path(__file__).resolve().parents[2]
    / "deployments"
    / "kubernetes"
    / "optional"
    / "skillhub-upload"
)

Runner = Callable[..., subprocess.CompletedProcess[str]]
Forwarder = Callable[[str], AbstractContextManager[str]]


def _run(args: Sequence[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, check=False, **kw)  # noqa: S603


def render_configmap(pack: dict[str, str] | None = None) -> dict[str, Any]:
    """The ConfigMap: one ``<slug>.SKILL.md`` key per skill, the manifest and the script."""
    pack = build_pack() if pack is None else pack
    data = {f"{slug}.SKILL.md": text for slug, text in pack.items()}
    data["pack-manifest.json"] = manifest_text(pack)
    data["upload.sh"] = (TEMPLATE_DIR / "upload.sh").read_text()
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": CONFIGMAP, "namespace": NAMESPACE, "labels": {"app": JOB}},
        "data": data,
    }


def render_job(image: str = DEFAULT_IMAGE, uid: int = 10001, gid: int = 10001) -> str:
    text = (TEMPLATE_DIR / "job.yaml").read_text()
    return (
        text.replace("__IMAGE__", image).replace("__UID__", str(uid)).replace("__GID__", str(gid))
    )


def live_image(run: Runner = _run) -> str:
    """The api image from the live Deployment (read-only), else the default."""
    out = run(
        [
            "kubectl",
            "-n",
            NAMESPACE,
            "get",
            "deploy",
            "api",
            "-o",
            'jsonpath={.spec.template.spec.containers[?(@.name=="api")].image}',
        ]
    )
    image = (out.stdout or "").strip() if out.returncode == 0 else ""
    return image or DEFAULT_IMAGE


def _api_key(env: dict[str, str], run: Runner) -> str | None:
    key = env.get(KEY_ENV, "").strip()
    if key:
        return key
    out = run(
        ["kubectl", "-n", NAMESPACE, "get", "secret", KEY_SECRET, "-o", "jsonpath={.data.key}"]
    )
    if out.returncode == 0 and out.stdout.strip():
        try:
            return base64.b64decode(out.stdout.strip()).decode().strip() or None
        except ValueError:
            return None
    return None


def cmd_plan(args: argparse.Namespace, run: Runner) -> int:
    pack = build_pack()
    configmap = render_configmap(pack)
    image = args.image or live_image(run)
    print(f"NFS export : {NFS_SERVER}:{NFS_PATH}")
    print(f"Mounted    : read-write at subPath '{SUB_PATH}' only (/skills/{SUB_PATH} in the pod)")
    print(f"Target     : {NAMESPACE}/{JOB}, ConfigMap {CONFIGMAP}, image {image}")
    print(f"Files ({len(pack)} skills, {len(pack) + 1} written incl. {MANIFEST_FILE}):")
    for slug in pack:
        print(f"  {SUB_PATH}/{slug}/SKILL.md")
    print(f"  {SUB_PATH}/{MANIFEST_FILE}")
    print("\n--- Job ---")
    print(render_job(image, args.uid, args.gid))
    print("--- ConfigMap keys ---")
    for key, value in configmap["data"].items():
        print(f"  {key} ({len(value)} bytes)")
    if args.full:
        print("\n--- ConfigMap ---")
        print(json.dumps(configmap, indent=2))
    print("\nNothing was written. Apply with: apply --approved")
    return 0


def cmd_apply(args: argparse.Namespace, run: Runner) -> int:
    if not args.approved:
        print(
            "refusing: apply writes to the NAS share; "
            "pass --approved after the owner approves the plan",
            file=sys.stderr,
        )
        return 2
    image = args.image or live_image(run)
    steps = [
        (["kubectl", "apply", "-f", "-"], json.dumps(render_configmap())),
        (["kubectl", "-n", NAMESPACE, "delete", "job", JOB, "--ignore-not-found"], None),
        (["kubectl", "apply", "-f", "-"], render_job(image, args.uid, args.gid)),
        (
            [
                "kubectl",
                "-n",
                NAMESPACE,
                "wait",
                "--for=condition=complete",
                f"job/{JOB}",
                "--timeout=150s",
            ],
            None,
        ),
    ]
    for argv, stdin in steps:
        result = run(argv, input=stdin) if stdin is not None else run(argv)
        if result.returncode != 0:
            print(f"failed: {' '.join(argv[:4])} (exit {result.returncode})", file=sys.stderr)
            return 1
    print("upload Job completed; run `refresh` then `verify`")
    return 0


def _client(transport: httpx.BaseTransport | None) -> httpx.Client:
    return httpx.Client(transport=transport, timeout=30.0)


def _replica_pods(run: Runner) -> list[str]:
    out = run(
        [
            "kubectl",
            "-n",
            OMNIROUTE_NAMESPACE,
            "get",
            "pods",
            "-l",
            OMNIROUTE_SELECTOR,
            "--field-selector=status.phase=Running",
            "-o",
            "jsonpath={.items[*].metadata.name}",
        ]
    )
    return sorted((out.stdout or "").split()) if out.returncode == 0 else []


@contextmanager
def port_forward(pod: str) -> Iterator[str]:
    """``kubectl port-forward`` to one OmniRoute pod; yields its local base URL."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen(  # noqa: S603
        [
            "kubectl",
            "-n",
            OMNIROUTE_NAMESPACE,
            "port-forward",
            f"pod/{pod}",
            f"{port}:{OMNIROUTE_PORT}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.2)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@contextmanager
def _fixed(url: str) -> Iterator[str]:
    yield url


def _targets(env: dict[str, str], run: Runner, forward: Forwarder) -> list[tuple[str, Any]]:
    """``(label, context manager yielding a base URL)`` for every OmniRoute replica."""
    url = env.get("NERVE_SKILLHUB_API_URL", "").rstrip("/")
    if url:
        return [(url, _fixed(url))]
    pods = _replica_pods(run)
    if not pods:
        return [(DEFAULT_API_URL, _fixed(DEFAULT_API_URL))]
    return [(pod, forward(pod)) for pod in pods]


def cmd_refresh(
    args: argparse.Namespace,
    run: Runner,
    env: dict[str, str],
    transport: httpx.BaseTransport | None,
    forward: Forwarder = port_forward,
) -> int:
    key = _api_key(env, run)
    if not key:
        print(
            "no privileged key available (set NERVE_OMNIROUTE_PRIVILEGED_KEY or fix the Secret)",
            file=sys.stderr,
        )
        return 1
    failed = 0
    for label, target in _targets(env, run, forward):
        with target as base, _client(transport) as client:
            try:
                resp = client.post(
                    f"{base}/api/skills/catalog/refresh",
                    headers={"Authorization": f"Bearer {key}"},
                )
                status = str(resp.status_code)
            except httpx.HTTPError:
                status = "unreachable"
        print(f"refresh {label}: HTTP {status}")
        failed += status != "200"
    return 0 if not failed else 1


def cmd_verify(
    args: argparse.Namespace,
    run: Runner,
    env: dict[str, str],
    transport: httpx.BaseTransport | None,
    forward: Forwarder = port_forward,
) -> int:
    key = _api_key(env, run)
    if not key:
        print("no privileged key available", file=sys.stderr)
        return 1
    pack = build_pack()
    expected = [f"{DOMAIN}-{slug}" for slug in pack]
    ok = True
    listed: dict[str, Any] = {}
    for label, target in _targets(env, run, forward):
        with target as base, _client(transport) as client:
            try:
                resp = client.get(
                    f"{base}/api/skills/catalog",
                    params={"domain": DOMAIN, "limit": 2000},
                    headers={"Authorization": f"Bearer {key}"},
                )
            except httpx.HTTPError:
                print(f"verify {label}: unreachable", file=sys.stderr)
                ok = False
                continue
        if resp.status_code != 200:
            print(f"verify {label}: HTTP {resp.status_code}", file=sys.stderr)
            ok = False
            continue
        listed = {
            r.get("name"): r.get("id")
            for r in resp.json().get("results", [])
            if isinstance(r, dict)
        }
        missing = [name for name in expected if name not in listed]
        extra = sorted(n for n in listed if n not in expected)
        print(f"verify {label}: {len(expected) - len(missing)}/{len(expected)} nervecenter skills")
        for name in missing:
            print(f"  missing: {name}")
        for name in extra:
            print(f"  not in pack: {name}")
        ok = ok and not missing and not extra
    for name in expected:
        if name in listed:
            print(f"  id: {listed[name]}")
    return 0 if ok else 1


def main(
    argv: Sequence[str] | None = None,
    *,
    run: Runner = _run,
    env: dict[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    forward: Forwarder = port_forward,
) -> int:
    parser = argparse.ArgumentParser(prog="skillhub_upload", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "apply"):
        p = sub.add_parser(name)
        p.add_argument("--image", default="", help="Job image (default: the live api image)")
        p.add_argument("--uid", type=int, default=10001)
        p.add_argument("--gid", type=int, default=10001)
        if name == "plan":
            p.add_argument("--full", action="store_true", help="also print the whole ConfigMap")
        else:
            p.add_argument("--approved", action="store_true", help="owner approved the NAS write")
    sub.add_parser("refresh")
    sub.add_parser("verify")
    args = parser.parse_args(argv)
    env = dict(os.environ) if env is None else env
    if args.command == "plan":
        return cmd_plan(args, run)
    if args.command == "apply":
        return cmd_apply(args, run)
    if args.command == "refresh":
        return cmd_refresh(args, run, env, transport, forward)
    return cmd_verify(args, run, env, transport, forward)


if __name__ == "__main__":
    raise SystemExit(main())
