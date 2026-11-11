"""Sign dataset manifests with the owner's SSH key (OpenSSH SSHSIG).

Signing (``backend.owner_tools.ssh_signer``) shells out to ``ssh-keygen -Y sign``
with a dedicated namespace, so NerveShield never reads the private key:
ssh-keygen (or ssh-agent) does. This module only verifies. The
namespace makes the signature useless as an SSH login, git commit or any other
SSHSIG use, and those signatures useless here.

Verification is pure Python against *pinned* OpenSSH public key lines (what you
would put in ``allowed_signers``); the key inside a signature is only compared
to the pins, never trusted on its own. Only ``ssh-ed25519`` keys are accepted.

Format reference: OpenSSH PROTOCOL.sshsig.
"""

from __future__ import annotations

import base64
import hashlib
import struct
from dataclasses import dataclass
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

NAMESPACE = "nervecenter-dataset-manifest@1"
_MAGIC = b"SSHSIG"
_BEGIN = "-----BEGIN SSH SIGNATURE-----"
_END = "-----END SSH SIGNATURE-----"
_HASHES = {"sha512": hashlib.sha512, "sha256": hashlib.sha256}


class SshSignatureError(ValueError):
    pass


def _string(value: bytes) -> bytes:
    return struct.pack(">I", len(value)) + value


def _read_string(buf: bytes, pos: int) -> tuple[bytes, int]:
    if pos + 4 > len(buf):
        raise SshSignatureError("truncated")
    (n,) = struct.unpack(">I", buf[pos : pos + 4])
    end = pos + 4 + n
    if end > len(buf):
        raise SshSignatureError("truncated")
    return buf[pos + 4 : end], end


@dataclass(frozen=True)
class SshPublicKey:
    blob: bytes  # wire-format public key
    raw: bytes  # 32-byte Ed25519 key

    @property
    def fingerprint(self) -> str:
        digest = base64.b64encode(hashlib.sha256(self.blob).digest()).decode().rstrip("=")
        return f"SHA256:{digest}"

    @classmethod
    def from_line(cls, line: str) -> SshPublicKey:
        parts = line.strip().split()
        if len(parts) < 2 or parts[0] != "ssh-ed25519":
            raise SshSignatureError("only ssh-ed25519 public keys are supported")
        try:
            blob = base64.b64decode(parts[1], validate=True)
        except ValueError:
            raise SshSignatureError("malformed public key") from None
        return cls.from_blob(blob)

    @classmethod
    def from_blob(cls, blob: bytes) -> SshPublicKey:
        kind, pos = _read_string(blob, 0)
        raw, pos = _read_string(blob, pos)
        if kind != b"ssh-ed25519" or len(raw) != 32 or pos != len(blob):
            raise SshSignatureError("only ssh-ed25519 public keys are supported")
        return cls(blob, raw)


def pinned_ssh_keys(lines: list[str]) -> dict[str, SshPublicKey]:
    keys = [SshPublicKey.from_line(line) for line in lines]
    return {k.fingerprint: k for k in keys}


def armor_decode(text: str) -> bytes:
    body = text.strip()
    if not body.startswith(_BEGIN) or not body.endswith(_END):
        raise SshSignatureError("not an armored SSH signature")
    inner = "".join(body[len(_BEGIN) : -len(_END)].split())
    try:
        return base64.b64decode(inner, validate=True)
    except ValueError:
        raise SshSignatureError("malformed signature encoding") from None


def verify_sshsig(
    message: bytes, armored: str, pinned: dict[str, SshPublicKey], *, namespace: str = NAMESPACE
) -> str:
    """Verify ``armored`` over ``message``. Returns the signer fingerprint."""
    blob = armor_decode(armored)
    if blob[:6] != _MAGIC or len(blob) < 10:
        raise SshSignatureError("not an SSHSIG blob")
    (version,) = struct.unpack(">I", blob[6:10])
    if version != 1:
        raise SshSignatureError("unsupported SSHSIG version")
    pos = 10
    pub_blob, pos = _read_string(blob, pos)
    ns, pos = _read_string(blob, pos)
    reserved, pos = _read_string(blob, pos)
    hash_alg, pos = _read_string(blob, pos)
    sig_blob, pos = _read_string(blob, pos)
    if pos != len(blob):
        raise SshSignatureError("trailing data")
    if ns.decode(errors="replace") != namespace:
        raise SshSignatureError("wrong signature namespace")
    if hash_alg.decode(errors="replace") not in _HASHES:
        raise SshSignatureError("unsupported hash")
    key = SshPublicKey.from_blob(pub_blob)
    pinned_key = pinned.get(key.fingerprint)
    if pinned_key is None or pinned_key.blob != key.blob:
        raise SshSignatureError("signed by an unpinned key")
    kind, spos = _read_string(sig_blob, 0)
    raw_sig, spos = _read_string(sig_blob, spos)
    if kind != b"ssh-ed25519" or len(raw_sig) != 64 or spos != len(sig_blob):
        raise SshSignatureError("unexpected signature type")
    digest = _HASHES[hash_alg.decode()](message).digest()
    signed = _MAGIC + _string(ns) + _string(reserved) + _string(hash_alg) + _string(digest)
    try:
        Ed25519PublicKey.from_public_bytes(pinned_key.raw).verify(raw_sig, signed)
    except InvalidSignature:
        raise SshSignatureError("bad signature") from None
    return key.fingerprint


class ManifestSigner(Protocol):
    """Produces an armored SSHSIG over ``message`` in :data:`NAMESPACE`.

    The ssh-keygen implementation lives in ``backend.owner_tools.ssh_signer``:
    signing is an owner action and this package stays free of subprocess use.
    """

    def sign(self, message: bytes) -> str: ...


__all__ = [
    "NAMESPACE",
    "ManifestSigner",
    "SshPublicKey",
    "SshSignatureError",
    "armor_decode",
    "pinned_ssh_keys",
    "verify_sshsig",
]
