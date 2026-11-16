"""Run kubectl and ssh with vault credentials that exist only for one command (NS-ADR-120).

Each call makes a private directory (mode 0700) under ``NERVE_INFRA_TMP``
(a memory-backed emptyDir in the api pod), writes the kubeconfig or the SSH key
and pinned ``known_hosts`` there, runs the binary WITHOUT a shell, and removes
the directory afterwards. Credentials never appear in argv, logs, results or
errors. SSH keys must be RSA (>= 2048 bits) or Ed25519; an encrypted key is
decrypted in memory, so the passphrase never reaches a command line.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from typing import Any

from backend.infra.policy import redact

TMP_ENV = "NERVE_INFRA_TMP"
KUBECTL = os.environ.get("NERVE_KUBECTL_BIN", "kubectl")
SSH = os.environ.get("NERVE_SSH_BIN", "ssh")
SSH_KEYSCAN = os.environ.get("NERVE_SSH_KEYSCAN_BIN", "ssh-keyscan")
SSH_KEYGEN = os.environ.get("NERVE_SSH_KEYGEN_BIN", "ssh-keygen")
READ_TIMEOUT_S = 30.0
ACT_TIMEOUT_S = 180.0
_HOST = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9-]{1,63}$"
                   r"|^\d{1,3}(?:\.\d{1,3}){3}$")
_USER = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")


class ExecutorError(RuntimeError):
    """A failure with a stable code; the message never carries credentials."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RunResult:
    ok: bool
    exit_code: int | None
    output: str  # redacted, capped

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "exit": self.exit_code, "output": self.output}


def valid_host(host: Any) -> bool:
    return isinstance(host, str) and bool(_HOST.match(host))


def valid_user(user: Any) -> bool:
    return isinstance(user, str) and bool(_USER.match(user))


def normalize_ssh_key(private_key: str, passphrase: str | None = None) -> str:
    """An unencrypted OpenSSH private key (RSA >= 2048 or Ed25519), or ExecutorError."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

    data = (private_key or "").strip().encode()
    if not data:
        raise ExecutorError("ssh_key_missing")
    password = passphrase.encode() if passphrase else None
    try:
        key = serialization.load_ssh_private_key(data, password=password)
    except (ValueError, TypeError):
        try:
            key = serialization.load_pem_private_key(data, password=password)
        except (ValueError, TypeError) as exc:
            raise ExecutorError("ssh_key_unreadable") from exc
    if isinstance(key, rsa.RSAPrivateKey):
        if key.key_size < 2048:
            raise ExecutorError("ssh_key_too_weak")
    elif not isinstance(key, ed25519.Ed25519PrivateKey):
        raise ExecutorError("ssh_key_type_not_supported")
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode()


def _tmpdir() -> str:
    base = os.environ.get(TMP_ENV) or None
    if base:
        os.makedirs(base, mode=0o700, exist_ok=True)
    return tempfile.mkdtemp(prefix="infra-", dir=base)


def _write(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)


def _env(workdir: str, **extra: str) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": workdir, "LANG": "C.UTF-8", **extra}


async def _run(argv: list[str], *, env: dict[str, str], timeout: float,
               stdin: str | None = None, cwd: str | None = None) -> RunResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        raise ExecutorError("binary_missing") from exc
    try:
        out, _ = await asyncio.wait_for(
            proc.communicate(stdin.encode() if stdin is not None else None), timeout
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return RunResult(False, None, "[timed out]")
    text = redact(out.decode("utf-8", "replace"))
    return RunResult(proc.returncode == 0, proc.returncode, text)


def kubeconfig(config: dict[str, Any], token: str) -> str:
    server = str(config.get("server") or "")
    if not server.startswith("https://"):
        raise ExecutorError("kube_server_invalid")
    cluster: dict[str, Any] = {"server": server}
    ca = str(config.get("ca_pem") or "").strip()
    if ca:
        cluster["certificate-authority-data"] = base64.b64encode(ca.encode()).decode()
    elif config.get("insecure_skip_tls_verify"):
        cluster["insecure-skip-tls-verify"] = True
    context: dict[str, Any] = {"cluster": "c", "user": "u"}
    if config.get("namespace"):
        context["namespace"] = str(config["namespace"])
    return json.dumps({
        "apiVersion": "v1", "kind": "Config", "current-context": "x",
        "clusters": [{"name": "c", "cluster": cluster}],
        "users": [{"name": "u", "user": {"token": token}}],
        "contexts": [{"name": "x", "context": context}],
    })


async def run_kubectl(config: dict[str, Any], token: str | None, args: list[str], *,
                      stdin: str | None = None, act: bool = False) -> RunResult:
    if not token:
        raise ExecutorError("kube_token_missing")
    workdir = _tmpdir()
    try:
        path = os.path.join(workdir, "kubeconfig")
        _write(path, kubeconfig(config, token))
        return await _run([KUBECTL, "--request-timeout=30s", *args],
                          env=_env(workdir, KUBECONFIG=path),
                          timeout=ACT_TIMEOUT_S if act else READ_TIMEOUT_S, stdin=stdin)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _ssh_target(config: dict[str, Any]) -> tuple[str, int, str]:
    host, user = config.get("host"), config.get("user")
    port = config.get("port", 22)
    if not valid_host(host):
        raise ExecutorError("ssh_host_invalid")
    if not valid_user(user):
        raise ExecutorError("ssh_user_invalid")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ExecutorError("ssh_port_invalid")
    return host, port, user


async def run_ssh(config: dict[str, Any], secret: str | None, command: str, *,
                  act: bool = False) -> RunResult:
    host, port, user = _ssh_target(config)
    known = str(config.get("known_hosts") or "").strip()
    if not known:
        raise ExecutorError("host_key_not_pinned")
    try:
        payload = json.loads(secret or "")
    except ValueError as exc:
        raise ExecutorError("ssh_key_missing") from exc
    key_text = normalize_ssh_key(str(payload.get("private_key") or ""), payload.get("passphrase"))
    workdir = _tmpdir()
    try:
        key_path, kh_path = os.path.join(workdir, "id"), os.path.join(workdir, "known_hosts")
        _write(key_path, key_text)
        _write(kh_path, known + "\n")
        argv = [
            SSH, "-F", "/dev/null", "-i", key_path, "-p", str(port), "-l", user,
            "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={kh_path}",
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10",
            "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "ClearAllForwardings=yes",
            "-o", "PermitLocalCommand=no", "-o", "ServerAliveInterval=10",
            "-T", "--", host, command,
        ]
        return await _run(argv, env=_env(workdir),
                          timeout=ACT_TIMEOUT_S if act else READ_TIMEOUT_S)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def scan_host_keys(config: dict[str, Any]) -> tuple[str, list[str]]:
    """The host's public keys (known_hosts lines) and their SHA256 fingerprints."""
    host, port, _user = _ssh_target(config)
    workdir = _tmpdir()
    try:
        res = await _run([SSH_KEYSCAN, "-T", "5", "-p", str(port), "-t", "ed25519,ecdsa,rsa",
                          host], env=_env(workdir), timeout=20.0)
        lines = [ln for ln in res.output.splitlines() if ln and not ln.startswith("#")]
        if not lines:
            raise ExecutorError("host_unreachable")
        kh_path = os.path.join(workdir, "scan")
        _write(kh_path, "\n".join(lines) + "\n")
        fp = await _run([SSH_KEYGEN, "-l", "-E", "sha256", "-f", kh_path],
                        env=_env(workdir), timeout=10.0)
        prints = [" ".join(ln.split()[i] for i in (1, -1)) for ln in fp.output.splitlines()
                  if "SHA256:" in ln]
        return "\n".join(lines), prints
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


__all__ = [
    "ExecutorError",
    "RunResult",
    "normalize_ssh_key",
    "run_kubectl",
    "run_ssh",
    "scan_host_keys",
    "valid_host",
    "valid_user",
]
