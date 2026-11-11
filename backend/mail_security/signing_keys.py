"""Dataset-manifest signing key custody (closes the 6.2 key-custody gap).

Default custody: one Ed25519 private key in a file the owner holds, outside
any git work tree, mode ``0600`` in a ``0700`` directory, owned by the current
user. Verification never trusts a key carried inside a manifest; it uses a
pinned ``key_id -> public key`` set.

Defaults to ``~/.config/nervecenter/dataset-signing/ed25519.key``. Moving the
key to a Kubernetes secret, keychain or hardware token is an owner decision;
any of them can implement :class:`SigningKeySource`.

Nothing here generates a key implicitly: :meth:`FileSigningKeyStore.create`
must be called explicitly and refuses to overwrite.
"""

from __future__ import annotations

import base64
import hashlib
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

DEFAULT_KEY_PATH = Path("~/.config/nervecenter/dataset-signing/ed25519.key")


class KeyCustodyError(ValueError):
    pass


def key_id(public_key: bytes) -> str:
    if len(public_key) != 32:
        raise KeyCustodyError("not an Ed25519 public key")
    return hashlib.sha256(public_key).hexdigest()[:16]


class SigningKeySource(Protocol):
    def load(self) -> Ed25519PrivateKey: ...


def _inside_git_tree(path: Path) -> bool:
    for parent in [path, *path.parents]:
        if (parent / ".git").exists():
            return True
    return False


class FileSigningKeyStore:
    def __init__(self, path: Path | str = DEFAULT_KEY_PATH) -> None:
        self.path = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))

    def _check_location(self) -> None:
        if _inside_git_tree(self.path.parent):
            raise KeyCustodyError("signing key must not live inside a git work tree")

    def create(self) -> bytes:
        """Generate and store a new key. Returns its public key bytes."""
        self._check_location()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes_raw()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except FileExistsError:
            raise KeyCustodyError("a signing key already exists; refusing to overwrite") from None
        with os.fdopen(fd, "wb") as handle:
            handle.write(base64.b64encode(raw) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        return key.public_key().public_bytes_raw()

    def load(self) -> Ed25519PrivateKey:
        self._check_location()
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            raise KeyCustodyError("no signing key; create one explicitly") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise KeyCustodyError("signing key must be a regular file, not a link")
        if info.st_mode & 0o077:
            raise KeyCustodyError("signing key is readable by group or others")
        if info.st_uid != os.getuid():
            raise KeyCustodyError("signing key is not owned by the current user")
        if os.stat(self.path.parent).st_mode & 0o077:
            raise KeyCustodyError("signing key directory is accessible by group or others")
        fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(256)
        try:
            return Ed25519PrivateKey.from_private_bytes(
                base64.b64decode(data.strip(), validate=True)
            )
        except ValueError:
            raise KeyCustodyError("signing key file is malformed") from None


def pinned_keys(public_keys: list[bytes]) -> dict[str, bytes]:
    """Build the trusted set from public keys obtained out of band."""
    out: dict[str, bytes] = {}
    for pub in public_keys:
        Ed25519PublicKey.from_public_bytes(pub)  # validates
        out[key_id(pub)] = pub
    return out


def resolve_trusted(trusted: Mapping[str, bytes], wanted_id: object) -> bytes:
    if not isinstance(wanted_id, str) or wanted_id not in trusted:
        raise KeyCustodyError("manifest signed by an unpinned key")
    pub = trusted[wanted_id]
    if key_id(pub) != wanted_id:
        raise KeyCustodyError("pinned key does not match its id")
    return pub


__all__ = [
    "DEFAULT_KEY_PATH",
    "FileSigningKeyStore",
    "KeyCustodyError",
    "SigningKeySource",
    "key_id",
    "pinned_keys",
    "resolve_trusted",
]
