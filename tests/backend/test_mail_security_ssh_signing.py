"""SSH-key signing of dataset manifests (SSHSIG). Uses throwaway keys in tmp_path."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.mail_security.dataset_manifest import (
    HumanReview,
    ManifestError,
    build_manifest,
    sign_manifest_ssh,
    verify_manifest_ssh,
)
from backend.mail_security.ssh_signing import (
    NAMESPACE,
    SshPublicKey,
    SshSignatureError,
    pinned_ssh_keys,
    verify_sshsig,
)
from backend.mail_security.training_selection import SelectionManifest
from backend.owner_tools.ssh_signer import SshKeygenSigner

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen required")
NOW = datetime(2026, 11, 11, tzinfo=UTC)
REVIEW = HumanReview("rev-1", NOW)


def _keypair(tmp_path: Path, name: str, kind: str = "ed25519") -> tuple[Path, str]:
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", kind, "-N", "", "-C", name, "-f", str(key)], check=True
    )
    return key, (tmp_path / f"{name}.pub").read_text()


def _body() -> dict:
    return build_manifest(
        "ds-1", SelectionManifest("v", "a", NOW), [], {}, cutoff=NOW, created_at=NOW
    )


def test_python_verifier_accepts_ssh_keygen_signatures(tmp_path: Path) -> None:
    key, pub = _keypair(tmp_path, "k1")
    sig = SshKeygenSigner(key).sign(b"hello")
    assert (
        verify_sshsig(b"hello", sig, pinned_ssh_keys([pub]))
        == SshPublicKey.from_line(pub).fingerprint
    )


def test_ssh_keygen_accepts_our_signature_flow(tmp_path: Path) -> None:
    """Cross-check with OpenSSH's own verifier and allowed_signers."""
    key, pub = _keypair(tmp_path, "k1")
    sig = SshKeygenSigner(key).sign(b"payload")
    (tmp_path / "sig").write_text(sig)
    (tmp_path / "allowed").write_text(f'owner@example.test namespaces="{NAMESPACE}" {pub}')
    result = subprocess.run(
        [
            "ssh-keygen",
            "-Y",
            "verify",
            "-f",
            str(tmp_path / "allowed"),
            "-I",
            "owner@example.test",
            "-n",
            NAMESPACE,
            "-s",
            str(tmp_path / "sig"),
        ],
        input=b"payload",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0


def test_wrong_message_namespace_or_key_fail(tmp_path: Path) -> None:
    key, pub = _keypair(tmp_path, "k1")
    _, other_pub = _keypair(tmp_path, "k2")
    sig = SshKeygenSigner(key).sign(b"hello")
    with pytest.raises(SshSignatureError, match="bad signature"):
        verify_sshsig(b"hellp", sig, pinned_ssh_keys([pub]))
    with pytest.raises(SshSignatureError, match="namespace"):
        verify_sshsig(b"hello", sig, pinned_ssh_keys([pub]), namespace="git")
    with pytest.raises(SshSignatureError, match="unpinned"):
        verify_sshsig(b"hello", sig, pinned_ssh_keys([other_pub]))


def test_signature_from_another_namespace_is_rejected(tmp_path: Path) -> None:
    """A git/ssh signature by the same key cannot be replayed as a manifest signature."""
    key, pub = _keypair(tmp_path, "k1")
    git_sig = SshKeygenSigner(key, namespace="git").sign(b"hello")
    with pytest.raises(SshSignatureError, match="namespace"):
        verify_sshsig(b"hello", git_sig, pinned_ssh_keys([pub]))


def test_manifest_round_trip_and_tamper(tmp_path: Path) -> None:
    key, pub = _keypair(tmp_path, "k1")
    signer_key = SshPublicKey.from_line(pub)
    signed = sign_manifest_ssh(_body(), REVIEW, SshKeygenSigner(key), signer_key)
    assert signed.body["signing_key_id"] == "ssh:" + signer_key.fingerprint
    pins = pinned_ssh_keys([pub])
    assert verify_manifest_ssh(signed.to_json(), pins)["dataset_id"] == "ds-1"
    for tamper in (
        lambda d: d["body"].update(dataset_id="ds-2"),
        lambda d: d["body"]["review"].update(reviewer_ref="mallory"),
        lambda d: d["body"].update(signing_key_id="ssh:SHA256:forged"),
    ):
        doc = json.loads(signed.to_json())
        tamper(doc)
        with pytest.raises(ManifestError):
            verify_manifest_ssh(json.dumps(doc), pins)


def test_manifest_signed_by_unpinned_key_is_refused(tmp_path: Path) -> None:
    key, pub = _keypair(tmp_path, "k1")
    _, mine = _keypair(tmp_path, "k2")
    signed = sign_manifest_ssh(_body(), REVIEW, SshKeygenSigner(key), SshPublicKey.from_line(pub))
    with pytest.raises(ManifestError, match="unpinned"):
        verify_manifest_ssh(signed.to_json(), pinned_ssh_keys([mine]))


def test_declared_key_must_match_the_signing_key(tmp_path: Path) -> None:
    key, _ = _keypair(tmp_path, "k1")
    _, other = _keypair(tmp_path, "k2")
    with pytest.raises(ManifestError, match="declared"):
        sign_manifest_ssh(_body(), REVIEW, SshKeygenSigner(key), SshPublicKey.from_line(other))


def test_rsa_keys_are_refused(tmp_path: Path) -> None:
    _, pub = _keypair(tmp_path, "r1", "rsa")
    with pytest.raises(SshSignatureError, match="ed25519"):
        SshPublicKey.from_line(pub)


def test_locked_key_fails_cleanly_without_prompting(tmp_path: Path) -> None:
    key = tmp_path / "locked"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "secret-pass", "-f", str(key)], check=True
    )
    with pytest.raises(SshSignatureError):
        SshKeygenSigner(key, timeout_s=10).sign(b"x")


@pytest.mark.parametrize(
    "bad", ["", "not armored", "-----BEGIN SSH SIGNATURE-----\n!!\n-----END SSH SIGNATURE-----"]
)
def test_malformed_signatures(tmp_path: Path, bad: str) -> None:
    _, pub = _keypair(tmp_path, "k1")
    with pytest.raises(SshSignatureError):
        verify_sshsig(b"x", bad, pinned_ssh_keys([pub]))


def test_owner_tools_are_not_imported_by_the_service() -> None:
    import re

    backend = Path(__file__).resolve().parents[2] / "backend"
    pattern = re.compile(r"^\s*(from|import)\s+backend\.owner_tools\b", re.M)
    offenders = [
        str(p.relative_to(backend))
        for p in backend.rglob("*.py")
        if "owner_tools" not in p.parts and pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []
