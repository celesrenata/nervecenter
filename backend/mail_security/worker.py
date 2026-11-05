"""NerveShield local-only security worker / consumer (Phase 1, Task 1.9).

The **consumer** half of the NerveShield spine. Task 1.8 only *scheduled*
``PENDING`` ``mail_security_jobs`` rows beside the existing durable ingest; this
module is the worker that picks one up and runs the deterministic plane end to
end:

    claim job  ->  resolve content (LOCAL_ONLY, least-privilege)
               ->  parse (Task 1.3)  ->  auth evidence (Task 1.4)
               ->  deterministic inspections (Task 1.5)
               ->  conservative SHADOW policy (Task 1.6)
               ->  persist evidence + assessment + findings (Task 1.7)
               ->  mark the job DONE  (verdict committed BEFORE the job ack)

Scope and non-goals (owner-approved, do not exceed):

- **LOCAL ONLY, no GPU, no LLM, no network.** This worker calls none of those.
  It is pure deterministic scanning over locally-resolved content. It imports
  no network / model client, opens no socket, and makes no outbound request.
- **No agent action, no provider write, SHADOW only (T-26, T-29).** Writing the
  assessment (metadata + opaque refs) is the *only* effect. There is no
  attention mint, no PDP action, no provider mutation. The verdict is metadata
  that the read-only Task 1.10 API will later expose; this task does not expose
  it.
- **Default flag OFF.** The worker runs only when
  :data:`~backend.mail_security.ingestion_seam.NERVE_MAIL_SECURITY_ENABLED` is
  on. It is *not* started in app startup and wires no always-on background
  consumer. It is an invokable unit (:meth:`MailSecurityWorker.process_one` /
  :meth:`process_available`) that tests and operators drive directly.

Account isolation (R-017 least-privilege, T-15) — scope reconciliation:

  A job's ``account_scope`` is the **canonical** boundary the consumer honors.
  The two Task 1.8 producers that actually write job rows use two *distinct,
  non-colliding* account-scope namespaces: the worker producer
  (``backend.ingest.mail_worker._schedule_security_job``) scopes a job as
  ``conn:<connection_id>`` (keyed on the durable mail connection), while the
  API producer (``backend.api.endpoints.communications._schedule_security_job``)
  scopes as ``src:<source>`` (keyed on the sanitized ingest source). Because
  the two prefixes never overlap, an API-ingested message and a connection-
  ingested message can never share an account scope by accident. This consumer
  does not reconcile or rewrite either namespace: **the content resolver is
  handed the exact ``account_scope`` recorded on the claimed job, and every
  repository read/write uses that same scope.** The worker never reads content
  or writes a verdict under any other scope, so a job scheduled under account A
  can only resolve A's content and only write A's assessment (the Task 1.7
  repository access-checks the scope on every call, so even a forged id cannot
  cross).

Job lifecycle (R-018, design §3 state machine / §4.4 step 5):

  ``PENDING`` --claim--> ``RUNNING`` --success--> ``DONE``
                                     --retryable failure (attempts < ceiling)-->
                                       ``ERROR_RETRYABLE``
                                     --failure (attempts >= ceiling)-->
                                       ``DEAD_LETTER``

  The claim is a lease (``lease_owner`` + ``lease_expires_at`` on the Task 1.7
  row) taken by a compare-and-set update, so two workers never double-process a
  job and a crashed worker's lease can lapse and be re-claimed (T-17). The
  verdict (assessment + findings) is committed to the durable store **before**
  the job is marked ``DONE`` (design §4.4 step 5: "outbox ack only after
  committed verdict"), so a crash after the verdict commit but before the ack
  leaves a re-claimable job whose idempotent assessment id yields exactly one
  assessment on replay (T-16/T-17).

Never clean on failure (I-02, T-18, the tasks "never mark complete" list):

  A resolution failure, a parse that hit a cap/was malformed, or a simulated
  scan timeout can **never** produce a clean / ``SAFE_TO_PROCESS`` assessment.
  Such a job is driven to ``ERROR_RETRYABLE`` / ``DEAD_LETTER`` *and* an
  honest ``INCOMPLETE`` / restrictive assessment is still written (visible
  uncertainty, never a silent drop). The policy itself fails closed, and the
  worker only ever hands the policy ``inspection_complete=False`` on any
  partial/failed path.

Privacy (T-14, I-05): the worker may hold decoded content transiently in
memory to scan it, but nothing raw is persisted or logged — only the Task 1.7
metadata-only models (opaque refs + safe codes). Log lines carry safe codes and
the account scope / job id only, never a body fragment.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import structlog

from backend.mail_security.auth_evidence import (
    SenderAddresses,
    TrustedReceiverBoundary,
    build_auth_evidence,
)
from backend.mail_security.identifiers import assessment_id as derive_assessment_id
from backend.mail_security.ingestion_seam import mail_security_enabled
from backend.mail_security.inspections import inspect_message
from backend.mail_security.models import (
    AcquisitionMode,
    Authentication,
    AuthObservation,
    AuthProvenance,
    AuthResult,
    Completeness,
    EvidenceFidelity,
    MessageEvidenceManifest,
    PolicyDisposition,
    ProcessingState,
    SecurityAssessment,
    SecurityFinding,
    TrustAxes,
)
from backend.mail_security.parser import (
    DEFAULT_LIMITS,
    ParsedEvidence,
    ParserLimits,
    evidence_fidelity_for,
    parse_mime_evidence,
)
from backend.mail_security.policy import PolicyEvidence, decide_policy

log = structlog.get_logger(__name__)

#: Version token stamped on this worker's logs / behavior. Bump on a change to
#: the consumer orchestration (not the pure sub-stages, which carry their own).
WORKER_VERSION = "mail-security-worker@1"

# -- job lifecycle states (design §3 / §4.4; stored as capped strings) --------

#: A freshly scheduled job (Task 1.8). The only state this worker claims from.
JOB_STATE_PENDING = "PENDING"
#: A job currently leased by a worker. Transient; cleared on terminal states.
JOB_STATE_RUNNING = "RUNNING"
#: A job whose verdict is committed and whose work is done (the ack). Terminal.
JOB_STATE_DONE = "DONE"
#: A job that failed transiently and may be retried within the ceiling.
JOB_STATE_ERROR_RETRYABLE = "ERROR_RETRYABLE"
#: A job whose retries are exhausted. Terminal until a manual replay.
JOB_STATE_DEAD_LETTER = "DEAD_LETTER"

#: Default bounded retry ceiling (R-018). After this many *attempts* a failing
#: job is dead-lettered rather than retried forever.
DEFAULT_MAX_ATTEMPTS = 5

#: Default lease duration. A crashed worker's lease lapses after this so the
#: job becomes re-claimable (T-17). Content scanning is bounded and fast, so a
#: short lease is safe.
DEFAULT_LEASE_SECONDS = 300

# -- safe failure codes (upper-snake; never raw text) -------------------------

_CODE_CONTENT_UNRESOLVED = "CONTENT_UNRESOLVED"
_CODE_RESOLUTION_FAILED = "RESOLUTION_FAILED"
_CODE_SCAN_TIMEOUT = "SCAN_TIMEOUT"
_CODE_SCAN_FAILED = "SCAN_FAILED"
_CODE_NO_CONTENT_REF = "NO_CONTENT_REF"


class SecurityScanTimeout(Exception):
    """Raised by a deadline guard when deterministic scanning overruns.

    A timeout is a *failure*, never a clean result: the worker maps it to a
    typed retryable/dead-letter job state and an INCOMPLETE assessment, never
    SAFE_TO_PROCESS (I-02, T-18).
    """


# =============================================================================
# content resolution (dedicated, typed, LOCAL-ONLY, least-privilege)
# =============================================================================


class ContentResolver(Protocol):
    """A dedicated typed evidence resolver for the security consumer.

    This is **not** the generic ``backend.communications.detection`` content
    resolver: per design §1 the security plane uses its own typed resolution so
    it does not make generic content suddenly accessible to every agent. An
    implementation resolves the opaque ``content_ref`` to the raw MIME bytes
    **only within the supplied account scope**, performing LOCAL-ONLY I/O (an
    object-store read), never a network/model call.

    Returning ``None`` means the content is absent/unresolved (which must become
    an INCOMPLETE assessment, never clean — I-02).
    """

    async def resolve(self, *, account_scope: str, content_ref: str) -> bytes | None:
        ...


@dataclass(frozen=True)
class ObjectStoreContentResolver:
    """Resolve ``content://<key>`` opaque refs to bytes from an object store.

    Mirrors the Task 1.8 ingest convention: the worker stores each message's
    raw bytes in the object store under a secret-free key and records a
    ``content://<key>`` pointer in the job meta. This resolver reverses that
    mapping with a single LOCAL object-store ``get`` — no network, no model,
    least-privilege (it can only read the one referenced object).

    ``account_scope`` is accepted on :meth:`resolve` so the resolver honors the
    canonical isolation boundary; the object-store key itself is derived from
    ``(connection_id, mail_id)`` upstream so it is already account-partitioned,
    and the consumer never passes a ref from a different-scoped job.
    """

    object_store: Any
    _scheme: str = "content://"

    async def resolve(self, *, account_scope: str, content_ref: str) -> bytes | None:
        if not content_ref or not content_ref.startswith(self._scheme):
            return None
        key = content_ref[len(self._scheme) :]
        if not key:
            return None
        try:
            return await self.object_store.get(key)
        except Exception:  # noqa: BLE001 - absent/unreadable content is INCOMPLETE
            return None


# =============================================================================
# worker
# =============================================================================


class MailSecurityWorker:
    """Local-only deterministic security consumer (Task 1.9).

    Construct with the Task 1.7 repository, a :class:`ContentResolver`, and the
    account scope to drain. :meth:`process_one` claims and processes a single
    ``PENDING`` job; :meth:`process_available` drains the ready backlog. Both
    are no-ops when the feature flag is OFF.
    """

    def __init__(
        self,
        *,
        repository: Any,
        content_resolver: ContentResolver,
        account_scope: str,
        analysis_profile_version: str,
        boundary: TrustedReceiverBoundary | None = None,
        limits: ParserLimits = DEFAULT_LIMITS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        worker_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        scan_deadline_seconds: float | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._repo = repository
        self._resolver = content_resolver
        self._account_scope = account_scope
        self._analysis_profile_version = analysis_profile_version
        # An empty trusted-receiver boundary is the safe default: no origin can
        # be attested, so every Authentication-Results header is UNTRUSTED.
        self._boundary = boundary if boundary is not None else TrustedReceiverBoundary()
        self._limits = limits
        self._max_attempts = max(1, int(max_attempts))
        self._lease_seconds = int(lease_seconds)
        self._worker_id = worker_id or f"worker:{uuid.uuid4().hex[:16]}"
        self._clock = clock if clock is not None else lambda: datetime.now(tz=UTC)
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self._scan_deadline_seconds = scan_deadline_seconds
        self._env = dict(env) if env is not None else None

    @property
    def enabled(self) -> bool:
        """Whether the worker will do anything (the flag is ON) — else inert."""
        return mail_security_enabled(self._env)

    # -- public entry points -----------------------------------------------

    async def process_available(self, *, limit: int | None = None) -> list[Any]:
        """Drain claimable ``PENDING`` jobs for this account (flag-gated).

        Returns the list of job rows processed this pass (each already moved to
        a terminal/retryable state). A no-op returning ``[]`` when the flag is
        OFF or the repository is unbound.
        """
        if not self.enabled or self._repo is None:
            return []
        processed: list[Any] = []
        while limit is None or len(processed) < limit:
            row = await self.process_one()
            if row is None:
                break
            processed.append(row)
        return processed

    async def process_one(self) -> Any | None:
        """Claim and process exactly one claimable ``PENDING`` job.

        Returns the updated job row, or ``None`` when the flag is OFF, the
        repository is unbound, or there is no claimable job. The whole pipeline
        runs for the claimed job; the verdict is committed before the job is
        acked ``DONE`` (design §4.4 step 5).
        """
        if not self.enabled or self._repo is None:
            return None
        job = await self._claim_next()
        if job is None:
            return None
        return await self._process_job(job)

    # -- claim (lease / compare-and-set) -----------------------------------

    async def _claim_next(self) -> Any | None:
        """Lease the next claimable ``PENDING`` job for this account.

        A job is claimable when it is ``PENDING`` with no live lease. The claim
        is a compare-and-set via :meth:`MailSecurityRepository.mark_job_state`
        keyed on the row id within this account scope, so two concurrent workers
        cannot both win the same job (the second's CAS sees the state already
        moved and returns ``None``). The lease carries this worker's id and an
        expiry so a crashed worker's job lapses and is re-claimable (T-17).
        """
        now = self._clock()
        pending = await self._repo.list_jobs(
            self._account_scope, state=JOB_STATE_PENDING
        )
        for candidate in pending:
            if self._lease_is_live(candidate, now):
                continue
            claimed = await self._repo.mark_job_state(
                self._account_scope,
                candidate.id,
                state=JOB_STATE_RUNNING,
                attempts=int(getattr(candidate, "attempts", 0) or 0) + 1,
                lease_owner=self._worker_id,
                lease_expires_at=now + timedelta(seconds=self._lease_seconds),
                last_error_code=None,
            )
            if claimed is not None and claimed.lease_owner == self._worker_id:
                return claimed
        return None

    @staticmethod
    def _lease_is_live(job: Any, now: datetime) -> bool:
        """``True`` if the job currently holds an unexpired lease."""
        owner = getattr(job, "lease_owner", None)
        expires = getattr(job, "lease_expires_at", None)
        if not owner or expires is None:
            return False
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return expires > now

    # -- process one claimed job -------------------------------------------

    async def _process_job(self, job: Any) -> Any:
        """Run the deterministic plane for one claimed job and persist the
        verdict, then ack the job — committing the verdict first."""
        account_scope = job.account_scope
        source_key = job.source_key
        content_ref = str((job.meta or {}).get("content_ref") or "") or None
        flags = dict((job.meta or {}).get("flags") or {})
        attempts = int(getattr(job, "attempts", 0) or 0)

        try:
            content = await self._resolve_content(account_scope, content_ref)
        except Exception:  # noqa: BLE001 - resolution failure is INCOMPLETE, never clean
            return await self._fail_job(
                job, attempts, code=_CODE_RESOLUTION_FAILED,
                account_scope=account_scope, source_key=source_key,
                content_ref=content_ref, flags=flags,
            )

        if content is None:
            # Absent / unresolved content can never be clean (I-02). Persist an
            # honest INCOMPLETE verdict and fail the job with a typed code.
            code = _CODE_NO_CONTENT_REF if not content_ref else _CODE_CONTENT_UNRESOLVED
            return await self._fail_job(
                job, attempts, code=code,
                account_scope=account_scope, source_key=source_key,
                content_ref=content_ref, flags=flags,
            )

        try:
            evidence = self._scan(content)
        except SecurityScanTimeout:
            return await self._fail_job(
                job, attempts, code=_CODE_SCAN_TIMEOUT,
                account_scope=account_scope, source_key=source_key,
                content_ref=content_ref, flags=flags,
            )
        except Exception:  # noqa: BLE001 - any scan failure is INCOMPLETE, never clean
            return await self._fail_job(
                job, attempts, code=_CODE_SCAN_FAILED,
                account_scope=account_scope, source_key=source_key,
                content_ref=content_ref, flags=flags,
            )

        assessment = self._assess(
            account_scope=account_scope,
            source_key=source_key,
            content_ref=content_ref,
            evidence=evidence,
            flags=flags,
        )

        # --- verdict BEFORE ack (design §4.4 step 5, T-16/T-17) -------------
        # Persist evidence + assessment + findings and commit, THEN mark the
        # job DONE. A crash between the two leaves a re-claimable job whose
        # idempotent assessment id yields exactly one assessment on replay.
        await self._persist_verdict(
            account_scope=account_scope,
            source_key=source_key,
            content_ref=content_ref,
            evidence=evidence,
            assessment=assessment,
        )
        done = await self._repo.mark_job_state(
            account_scope,
            job.id,
            state=JOB_STATE_DONE,
            attempts=attempts,
            lease_owner=None,
            lease_expires_at=None,
            last_error_code=None,
        )
        log.info(
            "mail_security.job_done",
            account_scope=account_scope,
            job_id=job.id,
            disposition=assessment.policy.disposition.value,
            processing_state=assessment.processing_state.value,
        )
        return done if done is not None else job

    # -- content resolution (LOCAL-ONLY, scoped) ---------------------------

    async def _resolve_content(
        self, account_scope: str, content_ref: str | None
    ) -> bytes | None:
        """Resolve the opaque content ref to raw MIME bytes, scoped.

        Least-privilege: the resolver is handed the job's own account scope and
        a single opaque ref; it performs one LOCAL object-store read and never
        crosses scope or touches the network (R-017, T-15).
        """
        if not content_ref:
            return None
        return await self._resolver.resolve(
            account_scope=account_scope, content_ref=content_ref
        )

    # -- deterministic scan (pure, offline) --------------------------------

    def _scan(self, content: bytes) -> ParsedEvidence:
        """Parse + guard the deterministic scan against a bounded deadline.

        Pure/offline: :func:`parse_mime_evidence` performs no network/subprocess
        I/O. The optional ``scan_deadline_seconds`` is a wall-clock budget: if
        parsing overruns it, we raise :class:`SecurityScanTimeout` so the job
        fails typed and the assessment is INCOMPLETE, never clean (T-18).
        """
        start = self._monotonic()
        evidence = parse_mime_evidence(content, limits=self._limits)
        if (
            self._scan_deadline_seconds is not None
            and (self._monotonic() - start) > self._scan_deadline_seconds
        ):
            raise SecurityScanTimeout(_CODE_SCAN_TIMEOUT)
        return evidence

    # -- assessment assembly -----------------------------------------------

    def _assess(
        self,
        *,
        account_scope: str,
        source_key: str,
        content_ref: str | None,
        evidence: ParsedEvidence,
        flags: dict[str, Any],
    ) -> SecurityAssessment:
        """Run auth -> inspections -> policy and build the metadata-only
        assessment for a fully-resolved, scanned message."""
        addresses = _sender_addresses(evidence)
        auth = build_auth_evidence(
            evidence.headers, addresses=addresses, boundary=self._boundary
        )

        findings: list[SecurityFinding] = []
        findings.extend(evidence.to_findings())  # INSPECTION_INCOMPLETE notes
        findings.extend(auth.findings)
        findings.extend(inspect_message(evidence, addresses=addresses))

        policy_evidence = PolicyEvidence(
            authentication=auth.authentication,
            findings=findings,
            inspection_complete=evidence.inspection_complete,
            incomplete_reason_codes=list(evidence.completeness_codes()),
            bulk_mail=bool(flags.get("bulk") or flags.get("no_reply")),
        )
        result = decide_policy(policy_evidence, clock=self._clock)

        processing_state = (
            ProcessingState.ASSESSED
            if evidence.inspection_complete
            else ProcessingState.INCOMPLETE
        )
        return self._build_assessment(
            account_scope=account_scope,
            source_key=source_key,
            processing_state=processing_state,
            evidence_fidelity=evidence_fidelity_for(evidence),
            completeness=Completeness(
                available=[],
                missing=[],
                checks_not_run=list(evidence.completeness_codes()),
            ),
            authentication=auth.authentication,
            findings=findings,
            axes=result.axes,
            policy=result.policy,
            sha256=evidence.source_sha256,
        )

    def _assess_incomplete(
        self,
        *,
        account_scope: str,
        source_key: str,
        content_ref: str | None,
        evidence: ParsedEvidence | None,
        code: str,
    ) -> SecurityAssessment:
        """Build an honest INCOMPLETE assessment for a failed/timed-out job.

        Never clean (I-02, T-18): the policy is handed ``inspection_complete=
        False`` with the failure code, so it fails closed to INCOMPLETE/REVIEW,
        and the processing state is INCOMPLETE. The SecurityAssessment model
        itself rejects a SAFE_TO_PROCESS disposition in a non-clean state, so a
        clean verdict here is impossible by construction.
        """
        unknown = AuthObservation(
            result=AuthResult.UNKNOWN,
            provenance=AuthProvenance.UNKNOWN,
            evidence_refs=[],
        )
        authentication = Authentication(
            spf=unknown, dkim=unknown, dmarc=unknown, arc=unknown
        )
        findings: list[SecurityFinding] = []
        if evidence is not None:
            findings.extend(evidence.to_findings())
            findings.extend(inspect_message(evidence, addresses=_sender_addresses(evidence)))
        policy_evidence = PolicyEvidence(
            authentication=authentication,
            findings=findings,
            inspection_complete=False,
            incomplete_reason_codes=[_safe_code(code)],
        )
        result = decide_policy(policy_evidence, clock=self._clock)
        fidelity = (
            evidence_fidelity_for(evidence)
            if evidence is not None
            else EvidenceFidelity.UNKNOWN
        )
        return self._build_assessment(
            account_scope=account_scope,
            source_key=source_key,
            processing_state=ProcessingState.INCOMPLETE,
            evidence_fidelity=fidelity,
            completeness=Completeness(
                available=[], missing=[], checks_not_run=[_safe_code(code)]
            ),
            authentication=authentication,
            findings=findings,
            axes=result.axes,
            policy=result.policy,
            sha256=evidence.source_sha256 if evidence is not None else None,
        )

    def _build_assessment(
        self,
        *,
        account_scope: str,
        source_key: str,
        processing_state: ProcessingState,
        evidence_fidelity: EvidenceFidelity,
        completeness: Completeness,
        authentication: Authentication,
        findings: list[SecurityFinding],
        axes: TrustAxes,
        policy: PolicyDisposition,
        sha256: str | None,
    ) -> SecurityAssessment:
        """Assemble the deterministic, metadata-only SecurityAssessment.

        ``assessment_id`` is the deterministic Task 1.2 id over
        ``(account_scope, source_key, revision, analysis_version)`` so a replay
        of the same job produces the SAME id and the Task 1.7 upsert is
        idempotent (one assessment revision, no duplicate — T-16/T-17).
        """
        aid = derive_assessment_id(
            account_scope,
            source_key,
            self._source_revision,
            self._analysis_profile_version,
        )
        return SecurityAssessment(
            assessment_id=aid,
            account_scope=account_scope,
            source_ref=source_key,
            source_revision=self._source_revision,
            analysis_profile_version=self._analysis_profile_version,
            created_at=self._clock(),
            processing_state=processing_state,
            evidence_fidelity=evidence_fidelity,
            completeness=completeness,
            authentication=authentication,
            findings=findings,
            axes=axes,
            model_runs=[],  # LOCAL-ONLY deterministic plane: no model run (T-26)
            policy=policy,
        )

    #: Phase 1 pins a single immutable source revision per message (a rescan is
    #: a new revision in a later phase); the assessment id keys on it.
    _source_revision: int = 1

    # -- persistence (verdict) ---------------------------------------------

    async def _persist_verdict(
        self,
        *,
        account_scope: str,
        source_key: str,
        content_ref: str | None,
        evidence: ParsedEvidence | None,
        assessment: SecurityAssessment,
    ) -> None:
        """Commit the evidence manifest + assessment + findings (metadata only).

        Opaque refs only (T-14): the manifest carries the ``content_ref``
        pointer and the content hash, never raw bytes; the assessment carries
        safe codes and opaque refs. The Task 1.7 upserts are idempotent on the
        deterministic ids, so a replay commits the same single rows.
        """
        manifest = self._build_manifest(
            account_scope=account_scope,
            source_key=source_key,
            content_ref=content_ref,
            evidence=evidence,
            evidence_fidelity=assessment.evidence_fidelity,
        )
        await self._repo.upsert_evidence(manifest)
        await self._repo.upsert_assessment(assessment)

    def _build_manifest(
        self,
        *,
        account_scope: str,
        source_key: str,
        content_ref: str | None,
        evidence: ParsedEvidence | None,
        evidence_fidelity: EvidenceFidelity,
    ) -> MessageEvidenceManifest:
        """Build the immutable per-revision evidence manifest (design §4.1)."""
        sha = evidence.source_sha256 if evidence is not None else None
        missing = list(evidence.completeness_codes()) if evidence is not None else []
        return MessageEvidenceManifest(
            source_id=source_key,
            account_scope=account_scope,
            source_revision=self._source_revision,
            provider=_provider_from_source_key(source_key),
            provider_uid=source_key,
            acquisition_mode=AcquisitionMode.IMAP_RFC822,
            acquired_at=self._clock(),
            raw_content_ref=_opaque_ref(content_ref),
            normalized_content_ref=None,
            sha256=sha,
            evidence_fidelity=evidence_fidelity,
            missing_parts=missing,
        )

    # -- failure path (typed job state + INCOMPLETE verdict) ---------------

    async def _fail_job(
        self,
        job: Any,
        attempts: int,
        *,
        code: str,
        account_scope: str,
        source_key: str,
        content_ref: str | None,
        flags: dict[str, Any],
    ) -> Any:
        """Drive a failed job to a typed state and persist an INCOMPLETE verdict.

        Verdict before ack still holds: the INCOMPLETE assessment is committed
        first, then the job is moved to ERROR_RETRYABLE (within the ceiling) or
        DEAD_LETTER (retries exhausted). The message is NEVER marked clean
        (I-02, T-18): the assessment carries an INCOMPLETE processing state and
        a restrictive disposition by construction.
        """
        safe = _safe_code(code)
        assessment = self._assess_incomplete(
            account_scope=account_scope,
            source_key=source_key,
            content_ref=content_ref,
            evidence=None,
            code=safe,
        )
        await self._persist_verdict(
            account_scope=account_scope,
            source_key=source_key,
            content_ref=content_ref,
            evidence=None,
            assessment=assessment,
        )

        terminal = (
            JOB_STATE_DEAD_LETTER
            if attempts >= self._max_attempts
            else JOB_STATE_ERROR_RETRYABLE
        )
        updated = await self._repo.mark_job_state(
            account_scope,
            job.id,
            state=terminal,
            attempts=attempts,
            lease_owner=None,
            lease_expires_at=None,
            last_error_code=safe,
        )
        log.warning(
            "mail_security.job_failed",
            account_scope=account_scope,
            job_id=job.id,
            state=terminal,
            attempts=attempts,
            error_code=safe,
        )
        return updated if updated is not None else job


# =============================================================================
# small pure helpers
# =============================================================================


def _sender_addresses(evidence: ParsedEvidence) -> SenderAddresses:
    """Extract the From / Reply-To / Return-Path header values for provenance.

    Reads only the already-parsed, bounded headers (no network). The returned
    raw header strings stay in process memory for the offline provenance
    checks; they are never persisted (T-14).
    """
    by_name: dict[str, str] = {}
    for name, value in evidence.headers:
        key = name.strip().lower()
        if key not in by_name:
            by_name[key] = value
    return SenderAddresses(
        from_address=by_name.get("from"),
        reply_to=by_name.get("reply-to"),
        return_path=by_name.get("return-path"),
    )


def _provider_from_source_key(source_key: str) -> str:
    """Return a bounded, safe provider token for the manifest.

    The source key is an opaque Task 1.2 key, not a provider name, so we record
    a stable ``unknown`` provider token rather than guessing. This is metadata
    only; it never affects the disposition.
    """
    return "unknown"


def _opaque_ref(content_ref: str | None) -> str | None:
    """Return a content ref coerced to the OpaqueRef alphabet, or ``None``.

    The ingest convention records ``content://<key>``; the ``:`` and ``/``
    characters are not in the OpaqueRef pattern (``[A-Za-z0-9:_-]``), so we
    canonicalize to a safe opaque pointer by replacing the scheme separator.
    The result is still an opaque pointer, never raw content (T-14).
    """
    if not content_ref:
        return None
    safe = content_ref.replace("://", ":").replace("/", "_")
    out = "".join(ch for ch in safe if ch.isalnum() or ch in ":_-")
    return out[:200] or None


def _safe_code(code: str) -> str:
    """Coerce a failure string to a safe upper-snake Code (never raw text)."""
    upper = "".join(
        ch if (ch.isalnum() or ch in "_.-") else "_" for ch in str(code).upper()
    )
    upper = upper.lstrip("0123456789_.-") or "SCAN_FAILED"
    return upper[:80]


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "JOB_STATE_DEAD_LETTER",
    "JOB_STATE_DONE",
    "JOB_STATE_ERROR_RETRYABLE",
    "JOB_STATE_PENDING",
    "JOB_STATE_RUNNING",
    "WORKER_VERSION",
    "ContentResolver",
    "MailSecurityWorker",
    "ObjectStoreContentResolver",
    "SecurityScanTimeout",
]
