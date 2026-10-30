"""Object storage for NerveCenter (spec Section 14 Persistence).

Section 14 of the NerveCenter specification pairs PostgreSQL with an object
store that keeps *logs, diffs, model transcripts, artifacts, and snapshots*.
This package is that store, source-only for T-H38 / D-H38:

- :mod:`~backend.storage.contracts` — the async contract
  (:class:`ObjectStore` with ``put``/``get``/``delete``/``exists``/``head``)
  and the shared :class:`ObjectMetadata` currency;
- :mod:`~backend.storage.memory` — the explicit in-memory backend for tests
  and ephemeral local runs;
- :mod:`~backend.storage.s3` — the S3/MinIO-compatible production adapter
  (AWS SigV4 over the already-available ``httpx`` dependency);
- :mod:`~backend.storage.config` — env configuration
  (``NERVECENTER_OBJECT_STORE_*``) and the honest factory
  :func:`create_object_store_from_env`, which raises
  :class:`~backend.storage.errors.ObjectStoreNotConfigured` when nothing is
  configured instead of silently substituting memory.

Typical wiring::

    from backend.storage import create_object_store_from_env

    try:
        store = create_object_store_from_env()
    except ObjectStoreNotConfigured:
        # Report "not configured" honestly; never fall back to memory.
        raise
    await store.put("logs/run-1.log", b"...")
"""

from .config import (
    DEFAULT_REGION,
    ENV_OBJECT_STORE_ACCESS_KEY,
    ENV_OBJECT_STORE_BUCKET,
    ENV_OBJECT_STORE_REGION,
    ENV_OBJECT_STORE_SECRET_KEY,
    ENV_OBJECT_STORE_SECURE,
    ENV_OBJECT_STORE_URL,
    ENV_OBJECT_STORE_VARS,
    ObjectStoreConfig,
    config_from_env,
    create_object_store_from_env,
    object_store_status,
)
from .contracts import MetadataMap, ObjectMetadata, ObjectStore, validate_key, validate_metadata
from .errors import (
    ObjectNotFoundError,
    ObjectStoreConfigError,
    ObjectStoreError,
    ObjectStoreNotConfigured,
)
from .memory import InMemoryObjectStore
from .s3 import S3ObjectStore

__all__ = [
    # config
    "DEFAULT_REGION",
    "ENV_OBJECT_STORE_ACCESS_KEY",
    "ENV_OBJECT_STORE_BUCKET",
    "ENV_OBJECT_STORE_REGION",
    "ENV_OBJECT_STORE_SECRET_KEY",
    "ENV_OBJECT_STORE_SECURE",
    "ENV_OBJECT_STORE_URL",
    "ENV_OBJECT_STORE_VARS",
    "ObjectStoreConfig",
    "config_from_env",
    "create_object_store_from_env",
    "object_store_status",
    # contracts
    "MetadataMap",
    "ObjectMetadata",
    "ObjectStore",
    "validate_key",
    "validate_metadata",
    # errors
    "ObjectNotFoundError",
    "ObjectStoreConfigError",
    "ObjectStoreError",
    "ObjectStoreNotConfigured",
    # backends
    "InMemoryObjectStore",
    "S3ObjectStore",
]
