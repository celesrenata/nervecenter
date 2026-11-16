"""Column-level encryption for form answers and write-ins (NS-ADR-101).

Uses the same keyring and AES-256-GCM as the encrypted object store
(``NERVECENTER_OBJECT_STORE_ENCRYPTION_KEYS``). The row context is bound as
associated data, so a value copied to another row fails to decrypt. There is
no plaintext fallback: without a key, nothing is stored.
"""
from __future__ import annotations

import base64
import os

from backend.storage.encrypted import Keyring, ObjectDecryptionError, keyring_from_env
from backend.storage.errors import ObjectStoreConfigError


class EncryptionUnavailable(RuntimeError):
    """No usable encryption key is configured."""


def load_keyring() -> Keyring:
    try:
        return keyring_from_env()
    except ObjectStoreConfigError:
        raise EncryptionUnavailable("encryption key is not configured") from None


def seal(keyring: Keyring, plaintext: str, context: str) -> str:
    kid = keyring.active
    nonce = os.urandom(12)
    ct = keyring.aead(kid).encrypt(nonce, plaintext.encode(), f"mh1:{context}".encode())
    return f"mh1.{kid}." + base64.b64encode(nonce + ct).decode()


def open_(keyring: Keyring, stored: str, context: str) -> str:
    try:
        tag, kid, b64 = stored.split(".", 2)
        if tag != "mh1":
            raise ValueError
        blob = base64.b64decode(b64, validate=True)
        out = keyring.aead(kid).decrypt(blob[:12], blob[12:], f"mh1:{context}".encode())
    except ObjectDecryptionError:
        raise
    except Exception:  # noqa: BLE001 - never leak detail
        raise ObjectDecryptionError("authentication failed") from None
    return out.decode()
