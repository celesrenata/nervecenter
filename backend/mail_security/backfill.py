"""NerveShield recovery / backfill sources provider (Defect 2).

Makes the documented reconcilable-scanner guarantee (``ingestion_seam``) operative:
a message whose best-effort job insert was lost in the commit-to-schedule window,
and every message committed before the flag was turned on, is reconstructed from
the already-committed ``communications`` row and handed to
:func:`~backend.mail_security.queue.sweep_once`, which upserts the missing
``PENDING`` job.

Key parity (I-11, T-17): the reconstructed source uses the ingest worker's OWN
builders (``security_account_scope``, ``security_source_identity``,
``_content_key``), so the outbox job key is identical to the one the live seam
produces; a later real ingest of the same message resolves to the backfilled job.

Strict ref grammar: ``content://mail/<uuid>/<digest>`` exactly, where ``<uuid>`` is
a lowercase canonical UUID (the connection id) and ``<digest>`` is the 24
lowercase-hex object-key digest the worker derives as
``sha256("<connection-id>:<mail-id>")[:24]``. The digest is one-way, so the
mail id is recovered from the stored object's put-time ``message_id`` metadata
(HEAD only, never the body) and is accepted only if it re-hashes to the ref's
digest; anything else is skipped with a counted reason.

Privacy (T-14, R-023): reads identifiers and boolean flags only (never subject,
sender or body; never ``ObjectStore.get``), only under the scope's own connection
prefix; logs carry counts by fixed code only. Backfilled jobs carry
``LEGACY_BODY_ONLY`` (NS-ADR-024): stored objects are decoded body text.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog

from backend.mail_security.acquisition import ACQUISITION_META_KEY
from backend.mail_security.models import AcquisitionMode

log = structlog.get_logger(__name__)

#: Default per-runner-pass bound on reconstructed sources (small on purpose).
DEFAULT_BATCH_SIZE = 50
#: Hard ceiling for the configurable batch.
MAX_BATCH_SIZE = 500
#: Bound on communications rows inspected per call (keeps a pass cheap).
DEFAULT_MAX_SCAN = 2000
_PAGE_SIZE = 200
ENV_BACKFILL_BATCH = "NERVE_MAIL_SECURITY_BACKFILL_BATCH"

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_REF_RE = re.compile(rf"content://mail/(?P<conn>{_UUID})/(?P<digest>[0-9a-f]{{24}})")
_SCOPE_RE = re.compile(rf"conn:(?P<conn>{_UUID})")
_MAX_REF_LEN = 128
#: Mail ids are printable, whitespace-free ASCII (gmail:<hex>, RFC Message-ID,
#: imap-<mailbox>-<uid>); bounded before hashing.
_MAIL_ID_RE = re.compile(r"[\x21-\x7e]{1,512}")


class BackfillSkip(StrEnum):
    """Closed set of counted skip reasons (never free text)."""

    NON_CONNECTION_SCOPE = "NON_CONNECTION_SCOPE"
    CONNECTION_UNKNOWN = "CONNECTION_UNKNOWN"
    CONNECTION_DISABLED = "CONNECTION_DISABLED"
    UNSUPPORTED_KIND = "UNSUPPORTED_KIND"
    REF_REJECTED = "REF_REJECTED"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    ALREADY_SCHEDULED = "ALREADY_SCHEDULED"
    OBJECT_MISSING = "OBJECT_MISSING"
    IDENTITY_UNAVAILABLE = "IDENTITY_UNAVAILABLE"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    LOOKUP_FAILED = "LOOKUP_FAILED"


@dataclass(frozen=True)
class ParsedRef:
    connection_id: str
    object_digest: str

    @property
    def object_key(self) -> str:
        return f"mail/{self.connection_id}/{self.object_digest}"


def parse_content_ref(ref: object) -> ParsedRef | None:
    """Strictly parse ``content://mail/<uuid>/<24-hex>``; ``None`` otherwise."""
    if not isinstance(ref, str) or len(ref) > _MAX_REF_LEN:
        return None
    m = _REF_RE.fullmatch(ref)
    if m is None:
        return None
    return ParsedRef(connection_id=m["conn"], object_digest=m["digest"])


def batch_size_from_env(env: Mapping[str, str] | None = None) -> int:
    """Configured batch (``NERVE_MAIL_SECURITY_BACKFILL_BATCH``), clamped."""
    raw = (env if env is not None else os.environ).get(ENV_BACKFILL_BATCH, "")
    try:
        value = int(raw) if str(raw).strip() else DEFAULT_BATCH_SIZE
    except ValueError:
        value = DEFAULT_BATCH_SIZE
    return max(1, min(MAX_BATCH_SIZE, value))


def _supported_kinds() -> dict[str, str]:
    """Mail connection kind -> cursor kind, exactly as the supervisor spawns."""
    from backend.ingest.supervisor import _KIND_TO_CURSOR

    return {k: v[0] for k, v in _KIND_TO_CURSOR.items()}


class CommittedCommunicationSources:
    """Bounded, idempotent ``SourcesProvider`` over committed communications.

    ``begin_pass()`` (called by the runner) resets the per-pass budget;
    ``scopes()`` lists ``conn:<uuid>`` for enabled mail connections so scopes
    with no job yet are still visited; calling the instance with a scope
    returns at most the remaining budget of missing sources, newest first.
    """

    def __init__(
        self,
        *,
        connections: Any,
        communications: Any,
        object_store: Any,
        jobs: Any,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_scan: int = DEFAULT_MAX_SCAN,
    ) -> None:
        self._connections = connections
        self._communications = communications
        self._store = object_store
        self._jobs = jobs
        self._batch = max(1, min(MAX_BATCH_SIZE, int(batch_size)))
        self._max_scan = max(1, int(max_scan))
        self._remaining: int | None = None
        self._skips: dict[BackfillSkip, int] = dict.fromkeys(BackfillSkip, 0)

    @property
    def skip_counts(self) -> dict[BackfillSkip, int]:
        return dict(self._skips)

    def _skip(self, reason: BackfillSkip) -> None:
        self._skips[reason] += 1

    def begin_pass(self) -> None:
        self._remaining = self._batch

    async def scopes(self) -> list[str]:
        kinds = _supported_kinds()
        out: list[str] = []
        for conn in await self._connections.list_connections(enabled_only=True):
            cid = str(getattr(conn, "id", "") or "")
            kind = str(getattr(conn, "kind", "") or "").lower()
            if kind in kinds and _SCOPE_RE.fullmatch(f"conn:{cid}"):
                out.append(f"conn:{cid}")
        return out

    async def _connection_for(self, account_scope: str) -> tuple[Any, str] | None:
        m = _SCOPE_RE.fullmatch(account_scope) if isinstance(account_scope, str) else None
        if m is None:
            self._skip(BackfillSkip.NON_CONNECTION_SCOPE)
            return None
        conn = await self._connections.get_connection(m["conn"])
        if conn is None:
            self._skip(BackfillSkip.CONNECTION_UNKNOWN)
            return None
        if not bool(getattr(conn, "enabled", False)):
            self._skip(BackfillSkip.CONNECTION_DISABLED)
            return None
        cursor_kind = _supported_kinds().get(str(getattr(conn, "kind", "") or "").lower())
        if cursor_kind is None:
            self._skip(BackfillSkip.UNSUPPORTED_KIND)
            return None
        return conn, cursor_kind

    async def reconstruct(
        self, account_scope: str, content_ref: object, flags: object
    ) -> dict[str, Any] | None:
        """Rebuild one source (identifiers + flags only) or ``None`` (counted)."""
        resolved = await self._connection_for(account_scope)
        if resolved is None:
            return None
        return await self._reconstruct(resolved, account_scope, content_ref, flags)

    async def _reconstruct(
        self,
        resolved: tuple[Any, str],
        account_scope: str,
        content_ref: object,
        flags: object,
    ) -> dict[str, Any] | None:
        from backend.ingest.mail_worker import (
            _content_key,
            security_account_scope,
            security_source_identity,
        )

        conn, cursor_kind = resolved
        parsed = parse_content_ref(content_ref)
        if parsed is None:
            self._skip(BackfillSkip.REF_REJECTED)
            return None
        if security_account_scope(parsed.connection_id) != account_scope:
            self._skip(BackfillSkip.SCOPE_MISMATCH)
            return None
        try:
            head = await self._store.head(parsed.object_key)
        except Exception:  # noqa: BLE001 - counted, never raised or echoed
            self._skip(BackfillSkip.LOOKUP_FAILED)
            return None
        if head is None:
            self._skip(BackfillSkip.OBJECT_MISSING)
            return None
        metadata = getattr(head, "metadata", None) or {}
        mail_id = metadata.get("message_id") if isinstance(metadata, Mapping) else None
        if not isinstance(mail_id, str) or not _MAIL_ID_RE.fullmatch(mail_id):
            self._skip(BackfillSkip.IDENTITY_UNAVAILABLE)
            return None
        # Bind the recovered id to the ref: it must hash to the ref's digest.
        if _content_key(parsed.connection_id, mail_id) != parsed.object_key:
            self._skip(BackfillSkip.IDENTITY_MISMATCH)
            return None
        source: dict[str, Any] = dict(
            security_source_identity(conn, cursor_kind=cursor_kind, mail_id=mail_id)
        )
        source["content_ref"] = f"content://{parsed.object_key}"
        source["flags"] = (
            {str(k): v for k, v in flags.items() if isinstance(v, bool)}
            if isinstance(flags, Mapping)
            else {}
        )
        source[ACQUISITION_META_KEY] = AcquisitionMode.LEGACY_BODY_ONLY.value
        return source

    async def __call__(self, account_scope: str) -> list[dict[str, Any]]:
        budget = self._remaining if self._remaining is not None else self._batch
        if budget <= 0:
            return []
        resolved = await self._connection_for(account_scope)
        if resolved is None:
            return []
        conn_id = str(getattr(resolved[0], "id", ""))
        scheduled = {
            str((j.meta or {}).get("content_ref"))
            for j in await self._jobs.list_jobs(account_scope)
            if isinstance(getattr(j, "meta", None), Mapping)
        }
        out: list[dict[str, Any]] = []
        scanned = 0
        offset = 0
        while len(out) < budget and scanned < self._max_scan:
            page = await self._communications.list_ref_identifiers(
                ref_prefix=f"content://mail/{conn_id}/",
                limit=min(_PAGE_SIZE, self._max_scan - scanned),
                offset=offset,
            )
            if not page:
                break
            offset += len(page)
            for _cid, ref, flags in page:
                scanned += 1
                if ref in scheduled:
                    self._skip(BackfillSkip.ALREADY_SCHEDULED)
                    continue
                source = await self._reconstruct(resolved, account_scope, ref, flags)
                if source is None:
                    continue
                scheduled.add(source["content_ref"])
                out.append(source)
                if len(out) >= budget:
                    break
        if self._remaining is not None:
            self._remaining -= len(out)
        if out or scanned:
            log.info(
                "mail_security.backfill_scan",
                scanned=scanned,
                reconstructed=len(out),
                skips={k.value: v for k, v in self._skips.items() if v},
            )
        return out


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_MAX_SCAN",
    "ENV_BACKFILL_BATCH",
    "MAX_BATCH_SIZE",
    "BackfillSkip",
    "CommittedCommunicationSources",
    "ParsedRef",
    "batch_size_from_env",
    "parse_content_ref",
]
