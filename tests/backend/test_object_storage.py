"""T-H38 / D-H38: Section 14 object storage — async contract, memory backend, honest config.

Covers the ``backend.storage`` source-only package:

- **memory behavior**: ``put``/``get`` round-trip (empty and binary payloads),
  ``exists`` probes, ``get``/``head``/``delete`` on a missing key,
  delete-then-delete idempotency, and re-put replacing payload + metadata
  while preserving ``created_at``;
- **metadata**: caller-supplied ``str -> str`` object metadata survives a
  ``head`` round-trip (deep-copied both ways), ``content_type`` and the
  SHA-256 ``etag`` are recorded, and malformed keys/metadata are rejected;
- **deletion**: ``delete`` returns ``True`` once and ``False`` afterwards,
  leaving other keys untouched;
- **unconfigured adapter error**: with no ``NERVECENTER_OBJECT_STORE_URL``
  the factory raises :class:`ObjectStoreNotConfigured` (a subclass of
  ``ObjectStoreConfigError``) instead of silently substituting the in-memory
  store; with an S3 endpoint missing bucket/credentials it names every
  missing ``NERVECENTER_OBJECT_STORE_*`` variable;
- **S3/MinIO adapter**: the ``httpx``-based adapter signs requests (SigV4
  ``Authorization``) and round-trips put/exists/get/head/delete against an
  in-memory S3 emulator served through ``httpx.MockTransport`` — no network;
- **endpoint transport**: an ``http://`` endpoint (the in-cluster MinIO case)
  is dialed in plain text by default — ``secure`` is derived from the URL
  scheme, and ``NERVECENTER_OBJECT_STORE_SECURE`` only overrides it when set
  to an explicit ``0``/``1`` — and the portful netloc (``host:port``) is
  preserved in the ``Host`` header so MinIO's vhost-style routing works.
"""

from __future__ import annotations

import hashlib
from urllib.parse import unquote

import httpx
import pytest

from backend.storage import (
    ENV_OBJECT_STORE_ACCESS_KEY,
    ENV_OBJECT_STORE_BUCKET,
    ENV_OBJECT_STORE_REGION,
    ENV_OBJECT_STORE_SECRET_KEY,
    ENV_OBJECT_STORE_SECURE,
    ENV_OBJECT_STORE_URL,
    InMemoryObjectStore,
    ObjectNotFoundError,
    ObjectStoreConfig,
    ObjectStoreConfigError,
    ObjectStoreError,
    ObjectStoreNotConfigured,
    S3ObjectStore,
    config_from_env,
    create_object_store_from_env,
)

_SHA256_EMPTY = hashlib.sha256(b"").hexdigest()


def _env(**overrides: str) -> dict[str, str]:
    """A complete S3 endpoint env, with optional per-test overrides."""
    base = {
        ENV_OBJECT_STORE_URL: "http://minio.local:9000",
        ENV_OBJECT_STORE_BUCKET: "nervcenter-artifacts",
        ENV_OBJECT_STORE_ACCESS_KEY: "test-ak",
        ENV_OBJECT_STORE_SECRET_KEY: "test-sk",
        ENV_OBJECT_STORE_REGION: "us-east-1",
        ENV_OBJECT_STORE_SECURE: "0",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# memory — basic put/get behavior
# ---------------------------------------------------------------------------


class TestMemoryPutGet:
    async def test_round_trip_bytes(self) -> None:
        store = InMemoryObjectStore()
        await store.put("logs/run-1.log", b"hello nervecenter")
        assert await store.get("logs/run-1.log") == b"hello nervecenter"

    async def test_empty_payload(self) -> None:
        store = InMemoryObjectStore()
        meta = await store.put("artifacts/empty.bin", b"")
        assert await store.get("artifacts/empty.bin") == b""
        assert meta.size == 0
        assert meta.etag == _SHA256_EMPTY

    async def test_binary_round_trip(self) -> None:
        payload = bytes(range(256)) * 3  # 768 bytes, all byte values present
        store = InMemoryObjectStore()
        await store.put("snapshots/snap-1.bin", payload)
        assert await store.get("snapshots/snap-1.bin") == payload

    async def test_get_missing_key_raises(self) -> None:
        store = InMemoryObjectStore()
        with pytest.raises(ObjectNotFoundError, match="missing.log"):
            await store.get("logs/missing.log")

    async def test_exists(self) -> None:
        store = InMemoryObjectStore()
        assert await store.exists("diffs/abc.diff") is False
        await store.put("diffs/abc.diff", b"diff --git a/b c")
        assert await store.exists("diffs/abc.diff") is True

    async def test_etag_is_sha256_content_digest(self) -> None:
        store = InMemoryObjectStore()
        meta = await store.put("logs/a.log", b"line-one\n")
        assert meta.etag == hashlib.sha256(b"line-one\n").hexdigest()

    async def test_size_recorded(self) -> None:
        store = InMemoryObjectStore()
        meta = await store.put("logs/a.log", b"0123456789")
        assert meta.size == 10


# ---------------------------------------------------------------------------
# memory — deletion
# ---------------------------------------------------------------------------


class TestMemoryDeletion:
    async def test_delete_returns_true_then_false(self) -> None:
        store = InMemoryObjectStore()
        await store.put("logs/run-2.log", b"b")
        assert await store.delete("logs/run-2.log") is True
        assert await store.delete("logs/run-2.log") is False

    async def test_delete_missing_key_is_false(self) -> None:
        store = InMemoryObjectStore()
        assert await store.delete("logs/never.log") is False

    async def test_deleted_key_disappears(self) -> None:
        store = InMemoryObjectStore()
        await store.put("logs/run-3.log", b"c")
        await store.delete("logs/run-3.log")
        assert await store.exists("logs/run-3.log") is False
        assert await store.head("logs/run-3.log") is None
        with pytest.raises(ObjectNotFoundError):
            await store.get("logs/run-3.log")

    async def test_delete_leaves_other_keys(self) -> None:
        store = InMemoryObjectStore()
        await store.put("logs/a.log", b"a")
        await store.put("logs/b.log", b"b")
        await store.delete("logs/a.log")
        assert await store.exists("logs/a.log") is False
        assert await store.get("logs/b.log") == b"b"


# ---------------------------------------------------------------------------
# memory — re-put / overwrite semantics
# ---------------------------------------------------------------------------


class TestMemoryReput:
    async def test_reput_updates_payload_and_size(self) -> None:
        store = InMemoryObjectStore()
        first = await store.put("logs/run-4.log", b"v1")
        second = await store.put("logs/run-4.log", b"v2-the-longer-payload")
        assert await store.get("logs/run-4.log") == b"v2-the-longer-payload"
        assert second.size == len(b"v2-the-longer-payload")
        assert second.etag != first.etag

    async def test_reput_preserves_created_at(self) -> None:
        store = InMemoryObjectStore()
        first = await store.put("logs/run-5.log", b"v1")
        await store.put("logs/run-5.log", b"v2")
        head = await store.head("logs/run-5.log")
        assert head is not None
        assert head.created_at == first.created_at
        assert head.updated_at >= first.created_at


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------


class TestMemoryMetadata:
    async def test_metadata_round_trip(self) -> None:
        store = InMemoryObjectStore()
        meta = await store.put(
            "artifacts/model-transcript-1.md",
            b"thinking out loud...",
            content_type="text/markdown",
            metadata={"producer": "planner-1", "turns": "7"},
        )
        assert meta.content_type == "text/markdown"
        head = await store.head("artifacts/model-transcript-1.md")
        assert head is not None
        assert head.metadata == {"producer": "planner-1", "turns": "7"}
        assert head.content_type == "text/markdown"
        assert head.key == "artifacts/model-transcript-1.md"

    async def test_head_missing_key_is_none(self) -> None:
        store = InMemoryObjectStore()
        assert await store.head("artifacts/absent.md") is None

    async def test_defaults_when_unspecified(self) -> None:
        store = InMemoryObjectStore()
        meta = await store.put("logs/plain.log", b"x")
        assert meta.metadata == {}
        assert meta.content_type is None

    async def test_returned_metadata_is_isolated_copy(self) -> None:
        store = InMemoryObjectStore()
        await store.put(
            "logs/c.log", b"x", metadata={"origin": "worker-1"}
        )
        head = await store.head("logs/c.log")
        assert head is not None
        head.metadata["origin"] = "tampered"
        head.metadata["injected"] = "yes"
        again = await store.head("logs/c.log")
        assert again is not None
        assert again.metadata == {"origin": "worker-1"}

    async def test_reput_replaces_metadata(self) -> None:
        store = InMemoryObjectStore()
        await store.put(
            "logs/d.log", b"x", metadata={"origin": "worker-1"}
        )
        await store.put("logs/d.log", b"y")
        head = await store.head("logs/d.log")
        assert head is not None
        assert head.metadata == {}

    async def test_reput_with_none_metadata_clears_content_type(self) -> None:
        store = InMemoryObjectStore()
        await store.put(
            "logs/e.log", b"x", content_type="text/plain", metadata={}
        )
        await store.put("logs/e.log", b"y")
        head = await store.head("logs/e.log")
        assert head is not None
        assert head.content_type is None

    async def test_non_string_metadata_value_rejected(self) -> None:
        store = InMemoryObjectStore()
        with pytest.raises(ValueError, match="must be a string"):
            await store.put("logs/f.log", b"x", metadata={"n": 5})  # type: ignore[dict-item]

    async def test_empty_key_rejected(self) -> None:
        store = InMemoryObjectStore()
        with pytest.raises(ValueError, match="non-empty"):
            await store.put("", b"x")


# ---------------------------------------------------------------------------
# honest unconfigured-adapter behavior
# ---------------------------------------------------------------------------


class TestUnconfiguredAdapter:
    def test_config_from_env_empty_raises_not_configured(self) -> None:
        with pytest.raises(ObjectStoreNotConfigured) as exc:
            config_from_env({})
        assert isinstance(exc.value, ObjectStoreConfigError)
        assert ENV_OBJECT_STORE_URL in str(exc.value)

    def test_factory_empty_raises_not_configured(self) -> None:
        """The factory must fail honestly — never a silent in-memory swap."""
        with pytest.raises(ObjectStoreNotConfigured) as exc:
            create_object_store_from_env({})
        assert isinstance(exc.value, ObjectStoreConfigError)

    def test_factory_blank_url_raises_not_configured(self) -> None:
        with pytest.raises(ObjectStoreNotConfigured):
            config_from_env({ENV_OBJECT_STORE_URL: "   "})

    def test_s3_endpoint_missing_credentials_names_env_vars(self) -> None:
        env = {ENV_OBJECT_STORE_URL: "http://minio.local:9000"}
        with pytest.raises(ObjectStoreConfigError) as exc:
            config_from_env(env)
        missing = exc.value.missing
        assert ENV_OBJECT_STORE_BUCKET in missing
        assert ENV_OBJECT_STORE_ACCESS_KEY in missing
        assert ENV_OBJECT_STORE_SECRET_KEY in missing

    def test_s3_endpoint_partially_configured_names_rest(self) -> None:
        env = _env(
            **{ENV_OBJECT_STORE_BUCKET: "", ENV_OBJECT_STORE_SECRET_KEY: ""}
        )
        with pytest.raises(ObjectStoreConfigError) as exc:
            config_from_env(env)
        assert exc.value.missing == [
            ENV_OBJECT_STORE_BUCKET,
            ENV_OBJECT_STORE_SECRET_KEY,
        ]

    def test_unsupported_scheme_rejected(self) -> None:
        with pytest.raises(ObjectStoreConfigError, match="gs"):
            config_from_env({ENV_OBJECT_STORE_URL: "gs://nervcenter-bucket"})

    def test_explicit_memory_url_is_honored(self) -> None:
        config = config_from_env({ENV_OBJECT_STORE_URL: "memory://"})
        assert config.scheme == "memory"
        store = create_object_store_from_env({ENV_OBJECT_STORE_URL: "memory://"})
        assert isinstance(store, InMemoryObjectStore)


class TestS3ObjectStoreConstructor:
    def test_incomplete_config_rejected(self) -> None:
        config = ObjectStoreConfig(
            scheme="s3",
            url="",
            bucket=None,
            access_key=None,
            secret_key=None,
            region="us-east-1",
            secure=True,
        )
        with pytest.raises(ObjectStoreError):
            S3ObjectStore(config)

    def test_non_s3_scheme_rejected(self) -> None:
        config = config_from_env({ENV_OBJECT_STORE_URL: "memory://"})
        with pytest.raises(ValueError):
            S3ObjectStore(config)

    def test_factory_returns_s3_object_store(self) -> None:
        store = create_object_store_from_env(_env())
        assert isinstance(store, S3ObjectStore)


class TestEndpointTransportSecurity:
    """Scheme-derived transport security + explicit ``_SECURE`` override.

    An in-cluster MinIO endpoint is plain ``http://host:9000``; it must be
    dialed over plain text by default (never silently upgraded to TLS).
    """

    def test_http_endpoint_is_plain_by_default(self) -> None:
        env = _env()
        env.pop(ENV_OBJECT_STORE_SECURE)
        assert env[ENV_OBJECT_STORE_URL] == "http://minio.local:9000"
        config = config_from_env(env)
        assert config.scheme == "s3"
        assert config.secure is False

    def test_https_endpoint_is_secure_by_default(self) -> None:
        env = _env()
        env.pop(ENV_OBJECT_STORE_SECURE)
        env[ENV_OBJECT_STORE_URL] = "https://s3.example.com"
        config = config_from_env(env)
        assert config.secure is True

    def test_s3_scheme_defaults_secure(self) -> None:
        env = _env()
        env.pop(ENV_OBJECT_STORE_SECURE)
        env[ENV_OBJECT_STORE_URL] = "s3://s3.amazonaws.com"
        config = config_from_env(env)
        assert config.secure is True

    def test_secure_zero_explicitly_plains_http(self) -> None:
        config = config_from_env(_env(**{ENV_OBJECT_STORE_SECURE: "0"}))
        assert config.secure is False

    def test_secure_one_explicitly_tls_http(self) -> None:
        config = config_from_env(_env(**{ENV_OBJECT_STORE_SECURE: "1"}))
        assert config.secure is True

    def test_garbage_secure_falls_back_to_scheme(self) -> None:
        env = _env(**{ENV_OBJECT_STORE_SECURE: "bogus"})
        config = config_from_env(env)
        # ``bogus`` is not ``0``/``1`` -> scheme default (http => plain).
        assert config.secure is False


# ---------------------------------------------------------------------------
# S3/MinIO adapter — against an in-memory S3 emulator via httpx.MockTransport
# ---------------------------------------------------------------------------

BUCKET = "nervcenter-artifacts"


def _make_s3_emulator() -> tuple[dict, object]:
    """In-memory dict standing in for an S3 bucket + MockTransport handler."""
    objects: dict[str, tuple[bytes, dict[str, str]]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert path.startswith(f"/{BUCKET}/"), f"expected path-style request, got {path}"
        key = unquote(path.removeprefix(f"/{BUCKET}/"))
        entry = objects.get(key)

        if request.method == "PUT":
            meta_headers = {
                f"x-amz-meta-{k.removeprefix('x-amz-meta-')}": v
                for k, v in request.headers.items()
                if k.lower().startswith("x-amz-meta-")
            }
            content_type = request.headers.get("Content-Type", "")
            objects[key] = (
                request.content,
                {
                    "content-length": str(len(request.content)),
                    **meta_headers,
                    **({"content-type": content_type} if content_type else {}),
                },
            )
            etag = hashlib.sha256(request.content).hexdigest()
            return httpx.Response(
                200,
                headers={
                    "ETag": f'"{etag}"',
                    "Last-Modified": "Mon, 05 Oct 2026 12:00:00 GMT",
                },
            )
        if entry is None:
            return httpx.Response(404, text="<Error><Code>NoSuchKey</Code></Error>")
        data, extra = entry
        headers = {
            "ETag": f'"{hashlib.sha256(data).hexdigest()}"',
            "Content-Length": extra["content-length"],
            "Last-Modified": "Mon, 05 Oct 2026 12:00:00 GMT",
        }
        for hk, hv in extra.items():
            if hk.startswith("x-amz-meta-"):
                headers[hk] = hv
        if extra.get("content-type"):
            headers["Content-Type"] = extra["content-type"]
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        if request.method == "GET":
            return httpx.Response(200, content=data, headers=headers)
        if request.method == "DELETE":
            del objects[key]
            return httpx.Response(204)
        return httpx.Response(400, text=f"unexpected {request.method}")

    return objects, httpx.MockTransport(handler)


async def _emulator_store() -> tuple[ObjectStoreConfig, S3ObjectStore, dict]:
    objects, transport = _make_s3_emulator()
    config = ObjectStoreConfig(
        scheme="s3",
        url="http://minio.local:9000",
        bucket=BUCKET,
        access_key="emulator-ak",
        secret_key="emulator-sk",
        region="us-east-1",
        secure=False,
    )
    store = S3ObjectStore(config, transport=transport)
    return config, store, objects


async def test_incluster_host_header_keeps_port() -> None:
    """A plain-HTTP in-cluster endpoint must keep ``host:port`` in the Host header.

    MinIO routes path-style requests by the ``Host`` header; an in-cluster
    endpoint such as ``http://minio-crawler-hl.minio-service.svc:9000`` carries
    a port that the adapter must not strip.
    """

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("host", ""))
        if request.method == "PUT":
            return httpx.Response(200, headers={"ETag": '"x"'})
        return httpx.Response(404, text="NoSuchKey")

    config = ObjectStoreConfig(
        scheme="s3",
        url="http://minio-crawler-hl.minio-service.svc:9000",
        bucket=BUCKET,
        access_key="ak",
        secret_key="sk",
        region="us-east-1",
        secure=False,
    )
    store = S3ObjectStore(config, transport=httpx.MockTransport(handler))
    await store.put("probe.txt", b"hi")
    # Every request (the exists probe + the PUT) carries the portful host.
    assert seen
    assert all(h == "minio-crawler-hl.minio-service.svc:9000" for h in seen)


class TestS3Adapter:
    async def test_round_trip_on_minio_compatible_endpoint(self) -> None:
        _, store, objects = await _emulator_store()
        try:
            meta = await store.put(
                "logs/run-6.log",
                b"adapter line one\n",
                content_type="text/plain",
                metadata={"producer": "worker-2"},
            )
            assert await store.exists("logs/run-6.log") is True
            assert await store.get("logs/run-6.log") == b"adapter line one\n"
            head = await store.head("logs/run-6.log")
            assert head is not None
            assert head.key == "logs/run-6.log"
            assert head.size == 17
            assert head.content_type == "text/plain"
            assert head.metadata == {"producer": "worker-2"}
            assert meta.size == 17
            assert "logs/run-6.log" in objects  # emulator actually stored it
        finally:
            await store.aclose()

    async def test_requests_are_sigv4_signed(self) -> None:
        seen: list[httpx.Request] = []

        def recorder(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(404, text="")

        config = ObjectStoreConfig(
            scheme="s3",
            url="http://minio.local:9000",
            bucket=BUCKET,
            access_key="emulator-ak",
            secret_key="emulator-sk",
            region="us-east-1",
            secure=False,
        )
        store = S3ObjectStore(config, transport=httpx.MockTransport(recorder))
        try:
            with pytest.raises(ObjectStoreError):
                await store.get("logs/signed.log")
            assert seen, "no request was captured"
            authorization = seen[0].headers["Authorization"]
            assert authorization.startswith("AWS4-HMAC-SHA256 ")
            assert "Credential=emulator-ak/" in authorization
            assert "SignedHeaders=" in authorization
            assert "Signature=" in authorization
            assert seen[0].headers["X-Amz-Date"]
        finally:
            await store.aclose()

    async def test_missing_object_semantics(self) -> None:
        _, store, _ = await _emulator_store()
        try:
            assert await store.exists("logs/absent.log") is False
            assert await store.head("logs/absent.log") is None
            with pytest.raises(ObjectNotFoundError):
                await store.get("logs/absent.log")
            assert await store.delete("logs/absent.log") is False
        finally:
            await store.aclose()

    async def test_delete_removes_object(self) -> None:
        _, store, objects = await _emulator_store()
        try:
            await store.put("logs/run-7.log", b"to-be-deleted")
            assert "logs/run-7.log" in objects
            assert await store.delete("logs/run-7.log") is True
            assert "logs/run-7.log" not in objects
            assert await store.delete("logs/run-7.log") is False
        finally:
            await store.aclose()

    async def test_error_statuses_surface_honestly(self) -> None:
        config = ObjectStoreConfig(
            scheme="s3",
            url="http://minio.local:9000",
            bucket=BUCKET,
            access_key="emulator-ak",
            secret_key="emulator-sk",
            region="us-east-1",
            secure=False,
        )

        def deny(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403, text="<Error><Code>AccessDenied</Code></Error>"
            )

        store = S3ObjectStore(config, transport=httpx.MockTransport(deny))
        try:
            with pytest.raises(ObjectStoreError, match="AccessDenied"):
                await store.get("logs/denied.log")
        finally:
            await store.aclose()


def test_env_var_names_are_deterministic() -> None:
    """Configuration names documented for the run report."""
    assert ENV_OBJECT_STORE_URL == "NERVECENTER_OBJECT_STORE_URL"
    assert ENV_OBJECT_STORE_BUCKET == "NERVECENTER_OBJECT_STORE_BUCKET"
    assert ENV_OBJECT_STORE_ACCESS_KEY == "NERVECENTER_OBJECT_STORE_ACCESS_KEY"
    assert ENV_OBJECT_STORE_SECRET_KEY == "NERVECENTER_OBJECT_STORE_SECRET_KEY"
    assert ENV_OBJECT_STORE_REGION == "NERVECENTER_OBJECT_STORE_REGION"
    assert ENV_OBJECT_STORE_SECURE == "NERVECENTER_OBJECT_STORE_SECURE"
