#!/usr/bin/env python3
"""Object-store round-trip verifier (spec §14 — MinIO / S3).

Verifies the live object store end-to-end through the SAME factory the app
uses (:func:`backend.storage.config.create_object_store_from_env`), so a green
run proves the app's put/get/head/delete path works against the configured
endpoint — no network faking, no in-memory substitution.

Environment (read from the process environment ONLY — never from files, never
logged) — the same ``NERVECENTER_OBJECT_STORE_*`` names the app and the
``nervecenter-env`` Secret use (see ``deployments/kubernetes/base/secrets.yaml.example``):

  NERVECENTER_OBJECT_STORE_URL         http(s)://host:port   (s3:// is accepted by
                                                  the factory but NOT usable by `mc`)
  NERVECENTER_OBJECT_STORE_BUCKET      <bucket>
  NERVECENTER_OBJECT_STORE_ACCESS_KEY <access-key-id>
  NERVECENTER_OBJECT_STORE_SECRET_KEY <secret-access-key>
  NERVECENTER_OBJECT_STORE_REGION      <region>            (optional, default us-east-1)
  NERVECENTER_OBJECT_STORE_SECURE      "0" | "1"             (optional; else follows scheme)

CLI overrides (win over env, in this order: env -> args):

  --url --bucket --access-key --secret-key --region --secure
  --key         probe key (default: a unique ``__nervecenter_verify/<uuid>``)
  --timeout     per-request timeout in seconds (default 10)

Safety contract
---------------
* The secret key is NEVER printed: it only goes on the wire. Every string that
  reaches stdout is run through :func:`redact`, which strips the exact secret
  value and generic credential patterns.
* Every step degrades to a classified ``PASS``/``FAIL`` record; a failing step
  prints the error *class* and a redacted message (no raw stack trace), then
  exits ``1``.
* The probe object is cleaned up (deleted) when the run finishes, so the run
  leaves the bucket empty of probe keys.

Example (live cluster, in-cluster endpoint + tenant creds from minio-secrets):

  NERVECENTER_OBJECT_STORE_URL=http://minio-crawler-hl.minio-service.svc:9000 \
  NERVECENTER_OBJECT_STORE_BUCKET=nervcenter-artifacts \
  NERVECENTER_OBJECT_STORE_ACCESS_KEY=<from minio-service/minio-secrets> \
  NERVECENTER_OBJECT_STORE_SECRET_KEY=<from minio-service/minio-secrets> \
  python tools/verify_object_store.py

Example (local docker-compose parity):

  NERVECENTER_OBJECT_STORE_URL=http://localhost:9000 \
  NERVECENTER_OBJECT_STORE_BUCKET=nervcenter-artifacts \
  NERVECENTER_OBJECT_STORE_ACCESS_KEY=minioadmin \
  NERVECENTER_OBJECT_STORE_SECRET_KEY=minioadmin \
  python tools/verify_object_store.py
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
import uuid

# Make the in-tree backend importable regardless of cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.storage import (  # noqa: E402
    ENV_OBJECT_STORE_ACCESS_KEY,
    ENV_OBJECT_STORE_BUCKET,
    ENV_OBJECT_STORE_REGION,
    ENV_OBJECT_STORE_SECRET_KEY,
    ENV_OBJECT_STORE_SECURE,
    ENV_OBJECT_STORE_URL,
    ObjectStoreConfigError,
    ObjectStoreError,
    create_object_store_from_env,
    object_store_status,
)


def redact(text: object, secret: str | None) -> str:
    """Strip the exact secret value (and a generic credential shape) from output."""
    out = str(text)
    if secret:
        out = out.replace(secret, "***")
    # Generic AWS/MinIO-style credential echo (defence in depth; never needed in
    # practice but guards against an error body echoing a signed request).
    import re

    out = re.sub(r"(?i)(signature=)[^\s&]+", r"\1***", out)
    return out


def build_env(args: argparse.Namespace) -> dict[str, str]:
    """Compose the NERVECENTER_OBJECT_STORE_* env, with CLI overrides winning."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k.startswith("NERVECENTER_OBJECT_STORE_")
    }
    if args.url:
        env[ENV_OBJECT_STORE_URL] = args.url
    if args.bucket:
        env[ENV_OBJECT_STORE_BUCKET] = args.bucket
    if args.access_key:
        env[ENV_OBJECT_STORE_ACCESS_KEY] = args.access_key
    if args.secret_key:
        env[ENV_OBJECT_STORE_SECRET_KEY] = args.secret_key
    if args.region:
        env[ENV_OBJECT_STORE_REGION] = args.region
    if args.secure in ("0", "1"):
        env[ENV_OBJECT_STORE_SECURE] = args.secure
    return env


async def _run(env: dict[str, str], key: str, timeout: float) -> list[tuple[str, bool, str]]:
    """Execute the round-trip; return a list of (step, ok, note)."""
    results: list[tuple[str, bool, str]] = []

    # 0. Honest config status (same probe /api/health uses).
    status = object_store_status(env)
    if status.get("status") not in ("configured", "memory"):
        results.append(
            (
                "config",
                False,
                f"status={status.get('status')} missing={list(status.get('missing') or ())} "
                f"unsupported_scheme={status.get('unsupported_scheme')}",
            )
        )
        return results
    results.append(
        ("config", True, f"status={status.get('status')} scheme={status.get('scheme')}")
    )

    store = create_object_store_from_env(env)
    secret = env.get(ENV_OBJECT_STORE_SECRET_KEY, "")
    payload = f"nervecenter-object-store-verify {uuid.uuid4()}".encode()
    want = hashlib.sha256(payload).hexdigest()
    meta = {"probe": "verify", "n": "1"}

    try:
        # 1. put
        try:
            put_meta = await asyncio.wait_for(
                store.put(key, payload, content_type="text/plain", metadata=meta), timeout
            )
        except ObjectStoreError as exc:
            results.append(("put", False, f"{type(exc).__name__}: {redact(exc, secret)}"))
            return results
        results.append(("put", True, f"size={put_meta.size} etag={put_meta.etag}"))

        # 2. get + byte compare
        got = await asyncio.wait_for(store.get(key), timeout)
        ok = hashlib.sha256(got).hexdigest() == want
        results.append(
            ("get", ok, f"bytes={len(got)} sha256_match={ok}")
        )
        if not ok:
            return results

        # 3. head (metadata round-trip)
        head = await asyncio.wait_for(store.head(key), timeout)
        head_ok = head is not None and head.size == len(payload) and put_meta.etag == head.etag
        meta_ok = head is not None and head.metadata == meta
        results.append(
            (
                "head",
                head_ok and meta_ok,
                f"size={head.size if head else None} etag_match={put_meta.etag == head.etag if head else None} "
                f"metadata_match={meta_ok}",
            )
        )
        if not (head_ok and meta_ok):
            return results

        # 4. exists True
        exists_after_put = await asyncio.wait_for(store.exists(key), timeout)
        results.append(("exists_after_put", exists_after_put, f"exists={exists_after_put}"))
        if not exists_after_put:
            return results

        # 5. delete True
        deleted = await asyncio.wait_for(store.delete(key), timeout)
        results.append(("delete", deleted, f"deleted={deleted}"))
        if not deleted:
            return results

        # 6. exists False + idempotent delete
        exists_after_delete = await asyncio.wait_for(store.exists(key), timeout)
        again = await asyncio.wait_for(store.delete(key), timeout)
        idem_ok = (not exists_after_delete) and (again is False)
        results.append(
            (
                "idempotent_delete",
                idem_ok,
                f"exists={exists_after_delete} second_delete={again}",
            )
        )
    except ObjectStoreError as exc:
        results.append(("__error__", False, f"{type(exc).__name__}: {redact(exc, secret)}"))
    except ObjectStoreConfigError as exc:
        results.append(("__config_error__", False, f"{type(exc).__name__}: {redact(exc, secret)}"))
    except Exception as exc:  # noqa: BLE001 - classify, don't dump a stack
        results.append(("__error__", False, f"{type(exc).__name__}: {redact(exc, secret)}"))
    finally:
        try:
            await store.aclose()
        except Exception:  # noqa: BLE001 - closing is best-effort
            pass

    return results


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Object-store round-trip verifier (spec §14).")
    p.add_argument("--url", help="NERVECENTER_OBJECT_STORE_URL (overrides env)")
    p.add_argument("--bucket", help="NERVECENTER_OBJECT_STORE_BUCKET (overrides env)")
    p.add_argument("--access-key", help="NERVECENTER_OBJECT_STORE_ACCESS_KEY (overrides env)")
    p.add_argument("--secret-key", help="NERVECENTER_OBJECT_STORE_SECRET_KEY (overrides env)")
    p.add_argument("--region", help="NERVECENTER_OBJECT_STORE_REGION (overrides env)")
    p.add_argument("--secure", choices=["0", "1"], help="NERVECENTER_OBJECT_STORE_SECURE (overrides env)")
    p.add_argument("--key", help="probe key (default: a unique __nervecenter_verify/<uuid>)")
    p.add_argument("--timeout", type=float, default=10.0, help="per-request timeout seconds")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    env = build_env(args)
    key = args.key or f"__nervecenter_verify/{uuid.uuid4().hex}"
    secret = env.get(ENV_OBJECT_STORE_SECRET_KEY, "")

    url = env.get(ENV_OBJECT_STORE_URL, "")
    bucket = env.get(ENV_OBJECT_STORE_BUCKET, "")
    # Print config with the redacted endpoint (host is not a secret; creds never shown).
    print("=== NerveCenter object-store round-trip verify (spec §14) ===")
    print(f"endpoint: {redact(url, secret) or '<unset>'}")
    print(f"bucket:   {redact(bucket, secret) or '<unset>'}")
    print(f"key:      {redact(key, secret)}")
    print("-" * 52)

    results = asyncio.run(_run(env, key, args.timeout))

    all_ok = True
    for step, ok, note in results:
        label = "PASS" if ok else "FAIL"
        if not ok:
            all_ok = False
        print(f"[{label}] {step:<16} {redact(note, secret)}")
    print("-" * 52)
    print(f"RESULT: {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
