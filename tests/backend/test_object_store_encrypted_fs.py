"""Encrypted object store on a durable filesystem volume (owner item: object-store encryption)."""

from __future__ import annotations

import base64
import shutil
import stat
from pathlib import Path

import pytest

from backend.storage.config import (
    ENV_ENCRYPTION_KEYS,
    create_object_store_from_env,
    object_store_status,
)
from backend.storage.encrypted import (
    EncryptedObjectStore,
    ObjectDecryptionError,
    keyring_from_value,
)
from backend.storage.errors import ObjectNotFoundError, ObjectStoreConfigError
from backend.storage.filesystem import FileObjectStore
from backend.storage.memory import InMemoryObjectStore

CANARY_BODY = b"CANARY-BODY-dear-friend-wire-money-7f3a"
CANARY_ACCOUNT = "canary.owner@example.com"
CANARY_MSGID = "gmail:canary-msgid-1234"
KEY = "mail/11111111-1111-4111-8111-111111111111/0123456789abcdef01234567"


def _k(byte: int) -> str:
    return base64.b64encode(bytes([byte]) * 32).decode()


RING = f"k1:{_k(1)}"


def _all_bytes(root: Path) -> bytes:
    return b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file())


# -- filesystem backend -------------------------------------------------------------------


async def test_fs_round_trip_and_reput_keeps_created_at(tmp_path):
    s = FileObjectStore(tmp_path)
    m1 = await s.put(KEY, b"one", content_type="text/plain", metadata={"a": "b"})
    m2 = await s.put(KEY, b"two!", metadata={"c": "d"})
    assert await s.get(KEY) == b"two!"
    head = await s.head(KEY)
    assert head.created_at == m1.created_at and head.updated_at >= m1.updated_at
    assert (head.size, head.metadata, head.content_type) == (4, {"c": "d"}, None)
    assert m2.etag == head.etag and await s.exists(KEY)
    assert await s.delete(KEY) and not await s.exists(KEY)
    assert await s.head(KEY) is None and not await s.delete(KEY)
    with pytest.raises(ObjectNotFoundError):
        await s.get(KEY)


@pytest.mark.parametrize(
    "bad", ["../x", "a/../../etc/passwd", "/abs", "a//b", "a/./b", "a b", "a/b\x00", "x" * 600]
)
async def test_fs_keys_cannot_escape_root(tmp_path, bad):
    root = tmp_path / "root"
    root.mkdir()
    s = FileObjectStore(root)
    with pytest.raises(ValueError):
        await s.put(bad, b"x")
    assert not any((tmp_path).rglob("passwd"))


async def test_fs_permissions_and_no_temp_leftovers(tmp_path):
    s = FileObjectStore(tmp_path)
    await s.put(KEY, b"x")
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert {p.suffix for p in files} == {".obj", ".meta"}
    for p in files:
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    for d in (p for p in tmp_path.rglob("*") if p.is_dir()):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700


# -- encryption ---------------------------------------------------------------------------


async def test_encrypted_round_trip_with_metadata(tmp_path):
    s = EncryptedObjectStore(FileObjectStore(tmp_path), keyring_from_value(RING))
    put = await s.put(
        KEY,
        CANARY_BODY,
        content_type="text/plain; charset=utf-8",
        metadata={"account": CANARY_ACCOUNT, "message_id": CANARY_MSGID},
    )
    assert await s.get(KEY) == CANARY_BODY
    head = await s.head(KEY)
    # Backfill reads message_id via head(): it must come back decrypted.
    assert head.metadata == {"account": CANARY_ACCOUNT, "message_id": CANARY_MSGID}
    assert head.size == len(CANARY_BODY) and head.content_type == "text/plain; charset=utf-8"
    assert head.etag == put.etag


async def test_disk_holds_no_plaintext_body_or_metadata(tmp_path):
    s = EncryptedObjectStore(FileObjectStore(tmp_path), keyring_from_value(RING))
    await s.put(KEY, CANARY_BODY, metadata={"account": CANARY_ACCOUNT, "message_id": CANARY_MSGID})
    raw = _all_bytes(tmp_path)
    for needle in (CANARY_BODY, CANARY_ACCOUNT.encode(), CANARY_MSGID.encode(), b"text/plain"):
        assert needle not in raw


async def test_renamed_ciphertext_fails_to_decrypt(tmp_path):
    s = EncryptedObjectStore(FileObjectStore(tmp_path), keyring_from_value(RING))
    await s.put(KEY, CANARY_BODY, metadata={"message_id": CANARY_MSGID})
    other = KEY[:-1] + "8"
    base = tmp_path / KEY
    dst = tmp_path / other
    for suffix in (".obj", ".meta"):
        shutil.copy(str(base) + suffix, str(dst) + suffix)
    with pytest.raises(ObjectDecryptionError):
        await s.get(other)
    with pytest.raises(ObjectDecryptionError):
        await s.head(other)


async def test_tampered_byte_fails(tmp_path):
    s = EncryptedObjectStore(FileObjectStore(tmp_path), keyring_from_value(RING))
    await s.put(KEY, CANARY_BODY)
    obj = Path(str(tmp_path / KEY) + ".obj")
    data = bytearray(obj.read_bytes())
    data[-1] ^= 0x01
    obj.write_bytes(bytes(data))
    with pytest.raises(ObjectDecryptionError):
        await s.get(KEY)


async def test_key_rotation_and_unknown_key():
    inner = InMemoryObjectStore()
    old = EncryptedObjectStore(inner, keyring_from_value(RING))
    await old.put(KEY, b"written-with-k1")
    rotated = EncryptedObjectStore(inner, keyring_from_value(f"k2:{_k(2)},{RING}"))
    assert await rotated.get(KEY) == b"written-with-k1"
    await rotated.put(KEY + "x", b"written-with-k2")
    assert (await inner.head(KEY + "x")).metadata["nse-kid"] == "k2"
    stranger = EncryptedObjectStore(inner, keyring_from_value(f"k3:{_k(3)}"))
    with pytest.raises(ObjectDecryptionError):
        await stranger.get(KEY)
    wrong_same_kid = EncryptedObjectStore(inner, keyring_from_value(f"k1:{_k(9)}"))
    with pytest.raises(ObjectDecryptionError):
        await wrong_same_kid.get(KEY)


async def test_plaintext_objects_are_refused_not_returned():
    inner = InMemoryObjectStore()
    await inner.put(KEY, CANARY_BODY, metadata={"message_id": CANARY_MSGID})
    s = EncryptedObjectStore(inner, keyring_from_value(RING))
    with pytest.raises(ObjectDecryptionError):
        await s.get(KEY)
    with pytest.raises(ObjectDecryptionError):
        await s.head(KEY)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "k1",
        f"k1:{base64.b64encode(b'short').decode()}",
        f"k1:{_k(1)},k1:{_k(2)}",
        "bad kid!:" + _k(1),
        "k1:not-base64!!",
    ],
)
def test_keyring_rejects_malformed(raw):
    with pytest.raises(ObjectStoreConfigError):
        keyring_from_value(raw)


# -- config / factory -----------------------------------------------------------------------


def test_file_store_requires_keys_and_is_always_encrypted(tmp_path):
    url = f"file://{tmp_path}"
    with pytest.raises(ObjectStoreConfigError) as e:
        create_object_store_from_env({"NERVECENTER_OBJECT_STORE_URL": url})
    assert ENV_ENCRYPTION_KEYS in e.value.missing
    store = create_object_store_from_env(
        {"NERVECENTER_OBJECT_STORE_URL": url, ENV_ENCRYPTION_KEYS: RING}
    )
    assert isinstance(store, EncryptedObjectStore)
    with pytest.raises(ObjectStoreConfigError):
        create_object_store_from_env(
            {"NERVECENTER_OBJECT_STORE_URL": "file://relative/path", ENV_ENCRYPTION_KEYS: RING}
        )


def test_status_probe_reports_file_store_without_values(tmp_path):
    st = object_store_status({"NERVECENTER_OBJECT_STORE_URL": f"file://{tmp_path}"})
    assert st["status"] == "incomplete" and st["missing"] == (ENV_ENCRYPTION_KEYS,)
    ok = object_store_status(
        {"NERVECENTER_OBJECT_STORE_URL": f"file://{tmp_path}", ENV_ENCRYPTION_KEYS: RING}
    )
    assert ok["status"] == "configured" and ok["encrypted"] is True
    assert _k(1) not in str(ok)


def test_s3_store_is_wrapped_when_keys_present():
    env = {
        "NERVECENTER_OBJECT_STORE_URL": "http://minio.local:9000",
        "NERVECENTER_OBJECT_STORE_BUCKET": "b",
        "NERVECENTER_OBJECT_STORE_ACCESS_KEY": "a",
        "NERVECENTER_OBJECT_STORE_SECRET_KEY": "s",
    }
    assert not isinstance(create_object_store_from_env(env), EncryptedObjectStore)
    assert isinstance(
        create_object_store_from_env({**env, ENV_ENCRYPTION_KEYS: RING}), EncryptedObjectStore
    )


def test_nonce_is_fresh_per_put():
    import asyncio

    inner = InMemoryObjectStore()
    s = EncryptedObjectStore(inner, keyring_from_value(RING))
    asyncio.run(s.put(KEY, b"same"))
    first = asyncio.run(inner.get(KEY))
    asyncio.run(s.put(KEY, b"same"))
    assert asyncio.run(inner.get(KEY)) != first


# -- migration owner tool -------------------------------------------------------------------


async def test_migration_copies_verifies_and_is_idempotent(tmp_path, monkeypatch):
    from backend.owner_tools import migrate_object_store as mig

    src = InMemoryObjectStore()
    keys = [f"mail/11111111-1111-4111-8111-111111111111/{i:024x}" for i in range(3)]
    for i, k in enumerate(keys[:2]):
        await src.put(
            k,
            CANARY_BODY + bytes([i]),
            content_type="text/plain",
            metadata={"message_id": f"{CANARY_MSGID}-{i}"},
        )

    async def fake_keys() -> list[str]:
        return keys  # third key has no source object

    class _Cfg:
        scheme = "s3"

    monkeypatch.setattr(mig, "_keys", fake_keys)
    monkeypatch.setattr(mig, "store_config", lambda env: _Cfg())
    monkeypatch.setattr(mig, "S3ObjectStore", lambda cfg: src)
    monkeypatch.setenv(ENV_ENCRYPTION_KEYS, RING)
    dest = f"file://{tmp_path}"
    assert await mig.run(dest, source_url="http://m:9000", verify_only=True) == {
        "keys": 3,
        "missing_at_dest": 2,
        "source_missing": 1,
    }
    assert await mig.run(dest, source_url="http://m:9000", verify_only=False) == {
        "copied": 2,
        "keys": 3,
        "source_missing": 1,
    }
    assert await mig.run(dest, source_url="http://m:9000", verify_only=False) == {
        "already_present": 2,
        "keys": 3,
        "source_missing": 1,
    }
    enc = EncryptedObjectStore(FileObjectStore(tmp_path), keyring_from_value(RING))
    assert await enc.get(keys[1]) == CANARY_BODY + b"\x01"
    assert (await enc.head(keys[0])).metadata == {"message_id": f"{CANARY_MSGID}-0"}
    assert CANARY_BODY not in _all_bytes(tmp_path)
    assert await src.get(keys[0]) == CANARY_BODY + b"\x00"  # source untouched
