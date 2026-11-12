"""NerveShield opt-in ingestion seam (Phase 1, Task 1.8).

An **opt-in, flag-gated** seam that *schedules* idempotent security work from
the existing worker / API ingest paths. It does **not** reimplement fetch,
leak secrets, or change cursor advancement (R-001, R-018). In SHADOW (and
especially with the flag OFF) the seam is a provably-inert no-op: the existing
ingestion + attention path behaves exactly as it does today.

Scope (Task 1.8 only — scheduling, never consuming):

- When :func:`mail_security_enabled` is ``True`` the seam derives the Task 1.2
  deterministic identity keys (``logical_source_key`` → ``outbox_job_key``) and
  upserts a single ``PENDING`` ``mail_security_jobs`` row via the Task 1.7
  :class:`~backend.persistence.repositories.mail_security_repo.
  MailSecurityRepository`. Consuming / processing the job is Task 1.9.
- With the flag OFF the seam returns immediately without touching the DB,
  the detector, the attention path, or the cursor.

Durability approach — **reconcilable scanner** (design §4.4 fallback, chosen
and documented honestly):

The worker's durable communication write
(``persist_communication_durably``) opens and commits its *own* session, and
the P0-D commit gate (``flush_durable``) governs the pipeline mirrors — neither
is a single open transaction a caller can enlist an extra write into. Joining
the security-job insert into that exact unit is therefore not cleanly
achievable without reaching into cursor-adjacent machinery, which Phase 1
forbids ("not license to change current cursor semantics"). So instead:

1. The job row is scheduled **best-effort, beside** the existing durable write,
   keyed on the deterministic Task 1.2 ids. A scheduling failure is swallowed
   (a safe code is logged, never a body fragment) so it can **never** fail the
   ingest, block the cursor advance, or mark the message safe (R-001.3,
   I-07) — the message is simply left without a (recoverable) job row.
2. Because the job key is a pure function of already-committed facts
   (account scope + source identity + analysis version), the row is fully
   **reconstructable** from the committed communication. :func:`scan_missing_jobs`
   is the reconciling sweeper that (re)creates any missing ``PENDING`` job for a
   set of already-committed sources. It is a pure, callable function (no
   background task wired at import); with the flag ON the pipeline runner
   drives it each pass through the bounded
   :class:`~backend.mail_security.backfill.CommittedCommunicationSources`
   provider (Defect 2), so lost jobs are actually recovered.

Durability limitation (documented, design §4.4 requires honesty): a crash in
the narrow window between the committed communication and a *best-effort* job
insert leaves no job row until the sweeper runs. This is strictly weaker than a
true co-committed transactional outbox, but it preserves the Phase-1 invariant
that matters most — the cursor and the existing attention behavior are never
perturbed by security scheduling — and the lost job is always recoverable from
committed state.

Privacy (steering, T-14): the scheduled job carries only the account scope,
the opaque Task 1.2 source/outbox keys, the analysis version, and bounded safe
metadata (the ``content_ref`` opaque pointer + boolean mail flags). It never
carries tokens, credentials, raw subject/body text, or email addresses, and no
log line emits raw content.
"""

from __future__ import annotations

import os
from typing import Any

import structlog

from backend.mail_security.acquisition import (
    ACQUISITION_META_KEY,
    RAW_REF_META_KEY,
    RAW_SEAM_MODES,
    is_raw_ref,
    trusted_mode_for_write,
)
from backend.mail_security.identifiers import (
    logical_source_key,
    normalize_analysis_version,
    outbox_job_key,
)

log = structlog.get_logger(__name__)

#: Environment toggle mirroring the ingest plane's ``NERVE_INGEST_ENABLED``
#: convention (:func:`backend.ingest.mail_worker.ingest_enabled`): the flag is
#: OFF unless the env var is set to something other than ``""`` / ``"0"``.
#: Defaults OFF per the NerveShield steering contract (SHADOW, opt-in).
ENV_MAIL_SECURITY_ENABLED = "NERVE_MAIL_SECURITY_ENABLED"

#: Opt-in full-email capture (NS-ADR-064, G8 item 1). When ON *and* the
#: security seam is ON, the Gmail transport also fetches ``format=raw`` and the
#: mail worker stores the unmodified RFC822 bytes as a separate encrypted
#: object for the security plane. Defaults OFF; OFF is byte-identical to the
#: body-only behavior.
ENV_MAIL_SECURITY_FULL_CAPTURE = "NERVE_MAIL_SECURITY_FULL_CAPTURE"

#: Upper bound on one stored raw message (decoded bytes). Larger messages are
#: not raw-captured; they keep the body-only path and its honest fidelity.
RAW_CAPTURE_MAX_BYTES = 32 * 1024 * 1024

#: The analysis-profile/version token that, with the source key, fixes a job's
#: logical identity (Task 1.2 ``outbox_job_key``). SHADOW profile for Phase 1.
DEFAULT_ANALYSIS_PROFILE_VERSION = "shadow-v1"

#: The state a freshly scheduled (not-yet-consumed) job carries. Task 1.8 only
#: schedules; Task 1.9 moves it forward. PENDING is the honest "requested, not
#: evaluated" state — never a "safe" claim (I-02).
JOB_STATE_PENDING = "PENDING"


#: Process-wide durable mail-security repository bound at startup
#: (:func:`set_mail_security_repository`), mirroring
#: :func:`backend.ingest.mail_worker.set_communications_store`. ``None`` (the
#: default / no durable engine, or the flag OFF path that never binds it) keeps
#: the seam a no-op at both ingest call sites — the historical behavior.
_mail_security_repository: Any = None


def set_mail_security_repository(repository: Any) -> None:
    """App seam: bind (or unbind) the durable mail-security repository.

    Startup wiring calls this once with the
    ``RepositoryRegistry.mail_security`` repository **only when the feature
    flag is ON** (opt-in); passing ``None`` on teardown (or when the flag is
    OFF) restores the inert no-op default. Both the worker's ``_process_batch``
    and the ``POST /api/communications/ingest`` endpoint read the bound
    repository through :func:`get_mail_security_repository`.
    """
    global _mail_security_repository
    _mail_security_repository = repository


def get_mail_security_repository() -> Any:
    """Return the durable mail-security repository bound by
    :func:`set_mail_security_repository` (``None`` when unbound)."""
    return _mail_security_repository


def mail_security_enabled(env: dict[str, str] | None = None) -> bool:
    """Return ``True`` when the NerveShield ingestion seam is opted in.

    Mirrors :func:`backend.ingest.mail_worker.ingest_enabled`: reads
    ``NERVE_MAIL_SECURITY_ENABLED`` from the supplied mapping (or the process
    environment) and treats ``""`` / ``"0"`` as OFF. Defaults OFF so the seam
    is inert unless a deployment explicitly opts in.
    """
    source = dict(env) if env is not None else dict(os.environ)
    return source.get(ENV_MAIL_SECURITY_ENABLED, "").strip() not in ("", "0")


def full_capture_enabled(env: dict[str, str] | None = None) -> bool:
    """``True`` only when BOTH the security seam and full capture are opted in.

    Raw bytes are only worth fetching and storing when a security job will be
    scheduled to read them, so full capture can never run on its own.
    """
    source = dict(env) if env is not None else dict(os.environ)
    if not mail_security_enabled(source):
        return False
    return source.get(ENV_MAIL_SECURITY_FULL_CAPTURE, "").strip() not in ("", "0")


class MailSecurityIngestionSeam:
    """Schedules (never consumes) one idempotent security job per message.

    Construct with the Task 1.7 repository and the account scope for the
    connection / request. Both ingest paths call :meth:`schedule` after the
    existing durable communication write; the call is a no-op when the flag is
    OFF and is swallowed-on-failure when ON (see the module docstring).
    """

    def __init__(
        self,
        *,
        repository: Any,
        account_scope: str,
        analysis_profile_version: str = DEFAULT_ANALYSIS_PROFILE_VERSION,
        env: dict[str, str] | None = None,
    ) -> None:
        self._repo = repository
        self._account_scope = account_scope
        self._analysis_profile_version = normalize_analysis_version(
            analysis_profile_version
        )
        self._env = dict(env) if env is not None else None

    @property
    def enabled(self) -> bool:
        """Whether the seam will schedule (the flag is ON) — else inert."""
        return mail_security_enabled(self._env)

    def _source_key(
        self,
        *,
        provider: str,
        provider_uid: str | None,
        rfc_message_id: str | None,
        folder: str | None,
    ) -> str:
        """Derive the account-scoped Task 1.2 logical source key."""
        return logical_source_key(
            self._account_scope,
            provider=provider,
            provider_uid=provider_uid,
            rfc_message_id=rfc_message_id,
            folder=folder,
        )

    async def schedule(
        self,
        *,
        provider: str,
        provider_uid: str | None = None,
        rfc_message_id: str | None = None,
        folder: str | None = None,
        content_ref: str | None = None,
        flags: dict[str, bool] | None = None,
        acquisition_mode: str | None = None,
        raw_ref: str | None = None,
    ) -> Any | None:
        """Upsert one ``PENDING`` security job for a message (idempotent).

        A no-op returning ``None`` when the flag is OFF (the seam is inert) or
        when the repository is absent (no durable plane wired). When ON, the
        Task 1.2 outbox job key makes the upsert idempotent: a retry / restart /
        replay of the same ``(account_scope, source, analysis_version)``
        resolves to the single existing row rather than scheduling a duplicate
        (R-018.2, T-16).

        The job row carries only keys + the opaque ``content_ref`` pointer and
        boolean mail flags — never raw subject/body/addresses or credentials
        (T-14). A failure is caught and logged with a safe code only; it is
        **never** raised, so it cannot fail the ingest, block the cursor, or
        mark the message safe (R-001.3, I-07). The message is simply left
        without a job row, which :func:`scan_missing_jobs` can later
        reconstruct from the committed communication.
        """
        if not self.enabled or self._repo is None:
            return None
        try:
            source_key = self._source_key(
                provider=provider,
                provider_uid=provider_uid,
                rfc_message_id=rfc_message_id,
                folder=folder,
            )
            job_key = outbox_job_key(
                self._account_scope,
                source_key,
                self._analysis_profile_version,
            )
            return await self._repo.upsert_job(
                account_scope=self._account_scope,
                outbox_job_key=job_key,
                source_key=source_key,
                analysis_profile_version=self._analysis_profile_version,
                state=JOB_STATE_PENDING,
                meta=_safe_job_meta(
                    content_ref=content_ref,
                    flags=flags,
                    acquisition_mode=acquisition_mode,
                    raw_ref=raw_ref,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - scheduling must never fail ingest
            # Safe code only — never the exception text (it could echo a body
            # fragment). The message is recoverable via scan_missing_jobs.
            log.warning(
                "mail_security.schedule_failed",
                error_type=type(exc).__name__,
            )
            return None


def _safe_job_meta(
    *,
    content_ref: str | None,
    flags: dict[str, bool] | None,
    acquisition_mode: str | None = None,
    raw_ref: str | None = None,
) -> dict[str, Any]:
    """Build the bounded, safe ``meta`` payload for a scheduled job.

    Only an opaque ``content_ref`` pointer and boolean mail flags are carried
    (T-14) — no raw subject/body/addresses, no credentials. Non-boolean flag
    values are dropped defensively so a caller can never smuggle text through
    the flags channel.
    """
    meta: dict[str, Any] = {"requested": True}
    # Trusted acquisition provenance (NS-ADR-024): recorded by the ingest code
    # that stored the object. Missing/unknown/raw-without-a-raw-seam all
    # collapse to LEGACY_BODY_ONLY; fidelity is never inferred from content.
    mode = trusted_mode_for_write(acquisition_mode, raw_ref=raw_ref)
    meta[ACQUISITION_META_KEY] = mode.value
    if content_ref is not None:
        meta["content_ref"] = str(content_ref)
    # NS-ADR-064: the raw ref is recorded only when it backs a trusted raw
    # mode, so a demoted job never carries a ref the worker would scan.
    if mode in RAW_SEAM_MODES and is_raw_ref(raw_ref):
        meta[RAW_REF_META_KEY] = str(raw_ref)
    if flags:
        meta["flags"] = {
            str(k): bool(v) for k, v in flags.items() if isinstance(v, bool)
        }
    return meta


async def scan_missing_jobs(
    *,
    repository: Any,
    account_scope: str,
    sources: list[dict[str, Any]],
    analysis_profile_version: str = DEFAULT_ANALYSIS_PROFILE_VERSION,
) -> list[Any]:
    """Reconciling sweeper: (re)create any missing ``PENDING`` job for a set of
    already-committed sources (design §4.4 reconcilable-scanner fallback).

    Each entry in ``sources`` describes one committed message by its identity
    material (``provider`` + ``provider_uid`` / ``rfc_message_id`` / ``folder``)
    plus optional safe ``content_ref`` / ``flags`` metadata. For every source
    the sweeper upserts the deterministic ``PENDING`` job; because the upsert is
    idempotent on the Task 1.2 outbox key, sources that already have a job are
    left untouched and only genuinely missing jobs are created.

    This is a pure, callable function: it starts no background task and wires no
    consumer. It exists so an operator / later phase can recover jobs lost in
    the best-effort scheduling window without duplicating effects (R-018, T-17).
    Returns the list of upserted job rows (the scheduled + already-present set).
    """
    if repository is None:
        return []
    seam = MailSecurityIngestionSeam(
        repository=repository,
        account_scope=account_scope,
        analysis_profile_version=analysis_profile_version,
        # The sweeper schedules unconditionally (it is itself the opt-in call
        # site); bypass the env flag by forcing it ON for this scoped instance.
        env={ENV_MAIL_SECURITY_ENABLED: "1"},
    )
    created: list[Any] = []
    for source in sources:
        row = await seam.schedule(
            provider=str(source.get("provider") or ""),
            provider_uid=source.get("provider_uid"),
            rfc_message_id=source.get("rfc_message_id"),
            folder=source.get("folder"),
            content_ref=source.get("content_ref"),
            flags=source.get("flags"),
            acquisition_mode=source.get("acquisition_mode"),
        )
        if row is not None:
            created.append(row)
    return created


__all__ = [
    "DEFAULT_ANALYSIS_PROFILE_VERSION",
    "ENV_MAIL_SECURITY_ENABLED",
    "ENV_MAIL_SECURITY_FULL_CAPTURE",
    "JOB_STATE_PENDING",
    "RAW_CAPTURE_MAX_BYTES",
    "MailSecurityIngestionSeam",
    "full_capture_enabled",
    "get_mail_security_repository",
    "mail_security_enabled",
    "scan_missing_jobs",
    "set_mail_security_repository",
]
