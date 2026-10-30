"""Environment configuration for the NerveCenter object store (spec §14).

The configured production adapter must *fail honestly* when configuration is
absent or incomplete:

- No ``NERVECENTER_OBJECT_STORE_URL`` at all →
  :func:`create_object_store_from_env` raises
  :class:`~backend.storage.errors.ObjectStoreNotConfigured` (a subclass of
  :class:`~backend.storage.errors.ObjectStoreConfigError`).  It **never**
  silently substitutes an in-memory store — call sites that need a store must
  either catch this error and report "not configured" or construct
  :class:`~backend.storage.memory.InMemoryObjectStore` explicitly.
- An ``s3://``/``https://`` endpoint without bucket or credentials →
  :class:`~backend.storage.errors.ObjectStoreConfigError` naming every
  missing ``NERVECENTER_OBJECT_STORE_*`` variable in the ``missing`` list.

Environment contract
--------------------

- ``NERVECENTER_OBJECT_STORE_URL`` — the store URL.  Supported schemes:
  ``s3://`` (S3/MinIO API over HTTPS), ``http://``/``https://`` (S3/MinIO
  API with a forced scheme), and ``memory://`` (the explicit in-memory
  store; useful for local runs where the operator *asked* for memory).
- ``NERVECENTER_OBJECT_STORE_BUCKET`` — bucket name (required for S3/MinIO).
- ``NERVECENTER_OBJECT_STORE_ACCESS_KEY`` — access key ID.
- ``NERVECENTER_OBJECT_STORE_SECRET_KEY`` — secret access key.
- ``NERVECENTER_OBJECT_STORE_REGION`` — optional region (default
  ``us-east-1``); MinIO commonly serves ``us-east-1``.
- ``NERVECENTER_OBJECT_STORE_SECURE`` — set to ``0`` to downgrade an
  ``http://`` endpoint to plain HTTP (HTTPS is the default).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .errors import ObjectStoreConfigError, ObjectStoreNotConfigured

#: Environment variable holding the object store URL.
ENV_OBJECT_STORE_URL = "NERVECENTER_OBJECT_STORE_URL"

#: Environment variable holding the S3/MinIO bucket name.
ENV_OBJECT_STORE_BUCKET = "NERVECENTER_OBJECT_STORE_BUCKET"

#: Environment variable holding the S3/MinIO access key id.
ENV_OBJECT_STORE_ACCESS_KEY = "NERVECENTER_OBJECT_STORE_ACCESS_KEY"

#: Environment variable holding the S3/MinIO secret access key.
ENV_OBJECT_STORE_SECRET_KEY = "NERVECENTER_OBJECT_STORE_SECRET_KEY"

#: Environment variable holding the optional region override.
ENV_OBJECT_STORE_REGION = "NERVECENTER_OBJECT_STORE_REGION"

#: Environment variable forcing plain HTTP for ``http://`` endpoints.
ENV_OBJECT_STORE_SECURE = "NERVECENTER_OBJECT_STORE_SECURE"

#: Every ``NERVECENTER_OBJECT_STORE_*`` variable this module reads.
ENV_OBJECT_STORE_VARS = (
    ENV_OBJECT_STORE_URL,
    ENV_OBJECT_STORE_BUCKET,
    ENV_OBJECT_STORE_ACCESS_KEY,
    ENV_OBJECT_STORE_SECRET_KEY,
    ENV_OBJECT_STORE_REGION,
    ENV_OBJECT_STORE_SECURE,
)

#: Default S3 signature region when none is configured.
DEFAULT_REGION = "us-east-1"

#: URL schemes accepted as S3/MinIO-compatible endpoints.
_S3_SCHEMES = ("s3", "http", "https")


@dataclass(frozen=True, slots=True)
class ObjectStoreConfig:
    """Typed, frozen container of object-store settings.

    Attributes
    ----------
    scheme:
        Normalised scheme: ``"memory"`` or ``"s3"``.
    url:
        The configured URL (used as the S3/MinIO API base for ``s3``).
    bucket:
        Bucket name (required for S3).
    access_key / secret_key:
        SigV4 credentials (required for S3).
    region:
        SigV4 region (default :data:`DEFAULT_REGION`).
    secure:
        Whether the S3/MinIO API base uses HTTPS.
    """

    scheme: str
    url: str
    bucket: str | None
    access_key: str | None
    secret_key: str | None
    region: str
    secure: bool

    def __post_init__(self) -> None:  # pragma: no cover - validation only
        if self.scheme not in ("memory", "s3"):
            raise ValueError(
                "object store scheme must be 'memory' or 's3'; got "
                f"{self.scheme!r}"
            )


def config_from_env(env: dict[str, str] | None = None) -> ObjectStoreConfig:
    """Build an :class:`ObjectStoreConfig` from the process environment.

    Uses a supplied mapping when given (tests), otherwise ``os.environ``.
    Raises :class:`~backend.storage.errors.ObjectStoreNotConfigured` when
    ``ENV_OBJECT_STORE_URL`` is absent, and
    :class:`~backend.storage.errors.ObjectStoreConfigError` when an
    S3/MinIO endpoint is named without its bucket or credentials.
    """

    source = dict(env) if env is not None else dict(os.environ)

    url = source.get(ENV_OBJECT_STORE_URL, "").strip()
    if not url:
        raise ObjectStoreNotConfigured(
            f"{ENV_OBJECT_STORE_URL} is not set; no object store is configured "
            "(NerveCenter §14 requires an explicit object store; an in-memory "
            "store must be constructed explicitly, never substituted here)"
        )

    scheme = url.split("://", 1)[0].lower()
    if scheme == "memory":
        return ObjectStoreConfig(
            scheme="memory",
            url=url,
            bucket=None,
            access_key=None,
            secret_key=None,
            region=DEFAULT_REGION,
            secure=True,
        )
    if scheme not in _S3_SCHEMES:
        raise ObjectStoreConfigError(
            f"{ENV_OBJECT_STORE_URL} has unsupported scheme {scheme!r}; "
            "expected s3://, http://, https://, or memory://",
        )

    missing: list[str] = []
    bucket = source.get(ENV_OBJECT_STORE_BUCKET, "").strip()
    if not bucket:
        missing.append(ENV_OBJECT_STORE_BUCKET)
    access_key = source.get(ENV_OBJECT_STORE_ACCESS_KEY, "").strip()
    if not access_key:
        missing.append(ENV_OBJECT_STORE_ACCESS_KEY)
    secret_key = source.get(ENV_OBJECT_STORE_SECRET_KEY, "").strip()
    if not secret_key:
        missing.append(ENV_OBJECT_STORE_SECRET_KEY)
    if missing:
        raise ObjectStoreConfigError(
            f"{ENV_OBJECT_STORE_URL} names an S3/MinIO endpoint but required "
            f"configuration is missing: {', '.join(missing)}",
            missing=tuple(missing),
        )

    region = source.get(ENV_OBJECT_STORE_REGION, "").strip() or DEFAULT_REGION
    # Transport security follows the URL scheme by default: an in-cluster
    # ``http://`` MinIO endpoint is dialed in plain text, while ``s3://`` and
    # ``https://`` use TLS. ``NERVECENTER_OBJECT_STORE_SECURE`` is an explicit
    # ``0``/``1`` override (e.g. to TLS an ``http://`` URL that fronts a
    # reverse proxy). Any other value falls through to the scheme default, so
    # a plain-HTTP endpoint is never silently upgraded to TLS.
    secure = scheme != "http"
    secure_flag = source.get(ENV_OBJECT_STORE_SECURE, "").strip()
    if secure_flag in ("0", "1"):
        secure = secure_flag == "1"
    return ObjectStoreConfig(
        scheme="s3",
        url=url,
        bucket=bucket,
        access_key=access_key,
        secret_key=secret_key,
        region=region,
        secure=secure,
    )


#: Required ``NERVECENTER_OBJECT_STORE_*`` variables for an S3/MinIO endpoint
#: (bucket + credentials).  The region and secure-flag variables are
#: optional and therefore excluded.
_OBJECT_STORE_REQUIRED_S3 = (
    ENV_OBJECT_STORE_BUCKET,
    ENV_OBJECT_STORE_ACCESS_KEY,
    ENV_OBJECT_STORE_SECRET_KEY,
)


def object_store_status(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Non-raising readiness probe for the object store configuration.

    Reports configuration *status* without constructing a backend and
    without ever echoing configured values.  Suitable for a ``/api/health``
    readiness section (spec P1-A: "surface *why* ingest is gated").

    Returns a dict with:

    - ``status``: ``"not_configured"`` (no
      :data:`ENV_OBJECT_STORE_URL`), ``"incomplete"`` (a supported endpoint
      is named but required variables are absent), ``"configured"`` (a
      supported endpoint is fully configured), or ``"memory"`` (explicit
      ``memory://`` request).
    - ``scheme``: the URL scheme (e.g. ``"s3"``) or ``None``.
    - ``unsupported_scheme``: ``True`` when a URL is set but its scheme is
      not supported.
    - ``missing``: every required :data:`ENV_OBJECT_STORE_VARS` name that is
      absent (names only, never values).
    """
    source = dict(env) if env is not None else dict(os.environ)
    url = source.get(ENV_OBJECT_STORE_URL, "").strip()
    if not url:
        return {
            "status": "not_configured",
            "scheme": None,
            "unsupported_scheme": False,
            "missing": (ENV_OBJECT_STORE_URL,),
        }
    scheme = url.split(":", 1)[0].lower()
    if scheme == "memory":
        return {
            "status": "memory",
            "scheme": "memory",
            "unsupported_scheme": False,
            "missing": (),
        }
    if scheme not in _S3_SCHEMES:
        return {
            "status": "incomplete",
            "scheme": scheme,
            "unsupported_scheme": True,
            "missing": (
                ENV_OBJECT_STORE_BUCKET,
                ENV_OBJECT_STORE_ACCESS_KEY,
                ENV_OBJECT_STORE_SECRET_KEY,
            ),
        }
    missing = tuple(v for v in _OBJECT_STORE_REQUIRED_S3
                    if not source.get(v, "").strip())
    return {
        "status": "configured" if not missing else "incomplete",
        "scheme": "s3",
        "unsupported_scheme": False,
        "missing": missing,
    }


def create_object_store_from_env(env: dict[str, str] | None = None):
    """Build the configured object store backend from the environment.

    Returns a :class:`~backend.storage.memory.InMemoryObjectStore` for an
    explicitly requested ``memory://`` URL and an
    :class:`~backend.storage.s3.S3ObjectStore` for S3/MinIO endpoints.  Every
    missing/incomplete configuration raises
    :class:`~backend.storage.errors.ObjectStoreConfigError` (or the
    :class:`~backend.storage.errors.ObjectStoreNotConfigured` subclass) —
    never a silently substituted memory backend.
    """

    config = config_from_env(env)
    if config.scheme == "memory":
        # Explicit operator request only; see config_from_env's error paths.
        from .memory import InMemoryObjectStore

        return InMemoryObjectStore()
    from .s3 import S3ObjectStore

    return S3ObjectStore(config)
