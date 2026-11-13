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
a safe trace id (a one-way hash of the job id) only -- never the account scope,
a message id or a body fragment (Task 2.6).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from backend.mail_security.acquisition import (
    RAW_SEAM_MODES,
    effective_acquisition_mode,
    fidelity_for_mode,
    is_body_only,
    missing_parts_for_mode,
    scan_ref_for,
)
from backend.mail_security.attachment_wiring import (
    AttachmentAssessment,
    collect_attachment_results,
)
from backend.mail_security.auth_evidence import (
    SenderAddresses,
    TrustedReceiverBoundary,
    build_auth_evidence,
    is_freemail_address,
)
from backend.mail_security.identifiers import assessment_id as derive_assessment_id
from backend.mail_security.ingestion_seam import mail_security_enabled
from backend.mail_security.inspections import inspect_message, sender_aligned_link_refs
from backend.mail_security.intel_wiring import collect_intel_findings
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
from backend.mail_security.queue import (
    CODE_LEASE_EXPIRED,
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    JOB_STATE_DEAD_LETTER,
    JOB_STATE_DONE,
    JOB_STATE_ERROR_RETRYABLE,
    JOB_STATE_PENDING,
    JOB_STATE_RUNNING,
    QueuePolicy,
    claim_next_job,
    lease_expired,
)
from backend.mail_security.telemetry import LatencyStage, safe_trace_id

if TYPE_CHECKING:
    from backend.persistence.repositories.mail_security_repo import (
        VerdictWriteOutcome,
    )

log = structlog.get_logger(__name__)

#: Version token stamped on this worker's logs / behavior. Bump on a change to
#: the consumer orchestration (not the pure sub-stages, which carry their own).
WORKER_VERSION = "mail-security-worker@1"

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
        latency_sink: Callable[[str, float], None] | None = None,
        latency_clock: Callable[[], float] | None = None,
        intel_store: Any | None = None,
        attachment_scanner: Any | None = None,
        static_inspector: Any | None = None,
    ) -> None:
        self._repo = repository
        # Optional static deep-inspection sidecar (NS-ADR-086). ``None``
        # (default): PDF/OOXML/ZIP attachments record
        # ATTACHMENT_DEEP_INSPECTION_NOT_RUN; nothing is sent anywhere.
        self._static_inspector = static_inspector
        # Optional local attachment scanner (NS-ADR-014). ``None`` (default)
        # means no scanner: attachments get static inspection only and an
        # explicit ATTACHMENT_SCAN_NOT_RUN note. No socket is ever opened.
        self._attachment_scanner = attachment_scanner
        # Optional global threat-intel store (read-only ``lookup``). ``None``
        # means no intel: nothing changes (UNKNOWN, never safe: R-004.5).
        self._intel_store = intel_store
        self._resolver = content_resolver
        self._account_scope = account_scope
        self._analysis_profile_version = analysis_profile_version
        # An empty trusted-receiver boundary is the safe default: no origin can
        # be attested, so every Authentication-Results header is UNTRUSTED.
        self._boundary = boundary if boundary is not None else TrustedReceiverBoundary()
        self._limits = limits
        self._max_attempts = max(1, int(max_attempts))
        self._lease_seconds = int(lease_seconds)
        self._policy = QueuePolicy(
            max_attempts=self._max_attempts, lease_seconds=self._lease_seconds
        )
        self._worker_id = worker_id or f"worker:{uuid.uuid4().hex[:16]}"
        self._clock = clock if clock is not None else lambda: datetime.now(tz=UTC)
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self._scan_deadline_seconds = scan_deadline_seconds
        self._env = dict(env) if env is not None else None
        # Task 2.6 stage-latency hook. ``None`` (default) means NO timing code
        # runs and the injected ``monotonic`` above is never touched. The sink
        # receives ``(stage_name, seconds)`` -- a fixed stage enum value and a
        # number, nothing else.
        self._latency_sink = latency_sink
        self._latency_clock = (
            latency_clock if latency_clock is not None else time.monotonic
        )

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
        """Lease the next claimable job for this account (Task 2.1 queue).

        Delegates to :func:`~backend.mail_security.queue.claim_next_job`: an
        atomic compare-and-set claim that bumps ``attempts`` (the fencing
        token with ``lease_owner``). ``PENDING``, due ``ERROR_RETRYABLE`` and
        lease-expired ``RUNNING`` jobs are claimable; a live lease is not.
        """
        return await claim_next_job(
            self._repo,
            self._account_scope,
            worker_id=self._worker_id,
            now=self._clock(),
            policy=self._policy,
        )

    def _note_lease_lost(self, job: Any) -> None:
        """Log a rejected fenced write (safe trace id only)."""
        log.warning("mail_security.lease_lost", trace_id=safe_trace_id(job.id))

    async def record_incomplete(self, job: Any, code: str) -> None:
        """Record an honest INCOMPLETE assessment for a job (sweeper hook).

        Used when a lease expired: no verdict was committed, and the message
        must never read as clean (I-02). Idempotent; never downgrades a
        complete assessment.
        """
        content_ref = str((job.meta or {}).get("content_ref") or "") or None
        assessment = self._assess_incomplete(
            account_scope=job.account_scope,
            source_key=job.source_key,
            content_ref=content_ref,
            evidence=None,
            code=_safe_code(code),
        )
        await self._persist_verdict(
            account_scope=job.account_scope,
            source_key=job.source_key,
            content_ref=content_ref,
            evidence=None,
            assessment=assessment,
            acquisition_mode=effective_acquisition_mode(job.meta),
        )

    # -- process one claimed job -------------------------------------------

    def _lap(self, stage: LatencyStage, started: float | None) -> float | None:
        """Report ``stage`` latency to the sink (numbers only) and restart the
        lap. Never raises into the pipeline; a no-op when no sink is set."""
        if self._latency_sink is None or started is None:
            return None
        now = self._latency_clock()
        try:
            self._latency_sink(stage.value, max(0.0, now - started))
        except Exception:  # noqa: BLE001 - telemetry must never affect a verdict
            pass
        return now

    async def _process_job(self, job: Any) -> Any:
        """Process one claimed job, recording total latency (Task 2.6)."""
        started = self._latency_clock() if self._latency_sink is not None else None
        # NS-ADR-071: a re-acquisition (e.g. the full-capture backfill) records
        # a new immutable source revision in the trusted job meta; verdicts are
        # never overwritten, the newer revision is added beside the old one.
        self._source_revision = source_revision_of(job.meta)
        try:
            return await self._process_job_inner(job, started)
        finally:
            self._source_revision = 1
            self._lap(LatencyStage.TOTAL, started)

    async def _process_job_inner(self, job: Any, lap: float | None) -> Any:
        """Run the deterministic plane for one claimed job and persist the
        verdict, then ack the job — committing the verdict first."""
        account_scope = job.account_scope
        source_key = job.source_key
        content_ref = str((job.meta or {}).get("content_ref") or "") or None
        flags = dict((job.meta or {}).get("flags") or {})
        # Fidelity comes from the TRUSTED acquisition mode recorded by the
        # ingest seam in the job meta, never from the content (NS-ADR-024).
        acquisition_mode = effective_acquisition_mode(job.meta)
        # NS-ADR-064: scan the raw RFC822 object when (and only when) the
        # trusted mode is a raw seam mode; ``content_ref`` stays the body ref
        # for the 7.1/7.4 joins. A missing raw object fails closed to
        # INCOMPLETE below, never silently falls back to the body.
        scan_ref = scan_ref_for(job.meta)
        attempts = int(getattr(job, "attempts", 0) or 0)

        try:
            content = await self._resolve_content(account_scope, scan_ref)
            lap = self._lap(LatencyStage.RESOLVE, lap)
        except Exception:  # noqa: BLE001 - resolution failure is INCOMPLETE, never clean
            return await self._fail_job(
                job, attempts, code=_CODE_RESOLUTION_FAILED,
                account_scope=account_scope, source_key=source_key,
                content_ref=content_ref, flags=flags,
            )

        if content is None:
            # Absent / unresolved content can never be clean (I-02). Persist an
            # honest INCOMPLETE verdict and fail the job with a typed code.
            code = _CODE_NO_CONTENT_REF if not scan_ref else _CODE_CONTENT_UNRESOLVED
            return await self._fail_job(
                job, attempts, code=code,
                account_scope=account_scope, source_key=source_key,
                content_ref=content_ref, flags=flags,
            )

        try:
            evidence = self._scan(content)
            lap = self._lap(LatencyStage.SCAN, lap)
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

        # Per-job timeout: a lease that lapsed mid-processing makes the result
        # untrustworthy for this attempt -> retryable + INCOMPLETE, never clean.
        if lease_expired(job, self._clock()):
            return await self._fail_job(
                job, attempts, code=CODE_LEASE_EXPIRED,
                account_scope=account_scope, source_key=source_key,
                content_ref=content_ref, flags=flags,
            )

        intel_findings = await collect_intel_findings(
            evidence,
            _sender_addresses(evidence),
            store=self._intel_store,
            now=self._clock(),
        )
        attachment_result = await collect_attachment_results(
            evidence, scanner=self._attachment_scanner, deep_inspector=self._static_inspector
        )
        assessment = self._assess(
            account_scope=account_scope,
            source_key=source_key,
            content_ref=content_ref,
            evidence=evidence,
            flags=flags,
            intel_findings=intel_findings,
            attachment_result=attachment_result,
            acquisition_mode=acquisition_mode,
        )
        lap = self._lap(LatencyStage.ASSESS, lap)

        # --- verdict + ack in ONE fenced transaction (design §4.4 step 5) ---
        # The fencing token (lease_owner, attempts) is compared inside the same
        # database transaction as the evidence/assessment/findings writes and
        # the DONE transition, so a stale worker is rejected atomically with
        # nothing written (T-16/T-17). The verdict is written before the ack
        # and both become visible together at commit; replay of a job whose
        # commit never happened yields exactly one assessment (idempotent id).
        outcome = await self._commit_verdict(
            job,
            content_ref=content_ref,
            evidence=evidence,
            assessment=assessment,
            state=JOB_STATE_DONE,
            lease_expires_at=None,
            last_error_code=None,
        )
        if not outcome.committed:
            return job  # stale worker: reclaimed by another; nothing written
        done = outcome.job
        self._lap(LatencyStage.PERSIST, lap)
        await self._record_campaign(job, content_ref=content_ref, evidence=evidence)
        await self._observe_semantics(job, evidence=evidence, assessment=assessment)
        log.info(
            "mail_security.job_done",
            trace_id=safe_trace_id(job.id),
            disposition=assessment.policy.disposition.value,
            processing_state=assessment.processing_state.value,
        )
        return done if done is not None else job

    async def _record_campaign(
        self, job: Any, *, content_ref: str | None, evidence: ParsedEvidence
    ) -> None:
        """SHADOW per-account content campaign membership (NS-ADR-069).

        Runs after the verdict is committed and can never change it. Inert
        without a bound repository or the key; any failure is logged as a
        safe code only.
        """
        from backend.mail_security.campaigns import (
            band_keys,
            campaign_secret_from_env,
            get_campaign_repository,
            signature,
            text_of,
        )

        repo = get_campaign_repository()
        if repo is None:
            return
        secret = campaign_secret_from_env()
        if secret is None:
            return
        try:
            sig = signature(secret, job.account_scope, text_of(evidence))
            if not sig:
                return
            await repo.record(
                job.account_scope,
                source_key=job.source_key,
                content_ref=content_ref,
                sig=sig,
                bands=band_keys(secret, job.account_scope, sig),
                now=self._clock(),
            )
        except Exception as exc:  # noqa: BLE001 - SHADOW signal must never fail a job
            log.warning("mail_security.campaign_record_failed", error_type=type(exc).__name__)

    async def _observe_semantics(
        self, job: Any, *, evidence: ParsedEvidence, assessment: SecurityAssessment
    ) -> None:
        """SHADOW semantic observer (NS-ADR-073), after the verdict commits.

        Inert unless an observer runtime is bound (mail security ON) and the
        account is opted in. It can never change the committed verdict; every
        failure is recorded as a typed status or logged as a safe code.
        """
        from backend.mail_security.observer_wiring import get_observer_runtime, sender_auth_of

        runtime = get_observer_runtime()
        if runtime is None:
            return
        try:
            await runtime.observe(
                job.account_scope,
                source_key=job.source_key,
                revision=self._source_revision,
                evidence=evidence,
                finding_codes=[str(f.explanation_code) for f in assessment.findings],
                sender_auth=sender_auth_of(assessment.authentication),
            )
        except Exception as exc:  # noqa: BLE001 - SHADOW signal must never fail a job
            log.warning("mail_security.observer_failed", error_type=type(exc).__name__)

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
        evidence = parse_mime_evidence(
            content,
            limits=self._limits,
            retain_attachment_payloads=(
                self._attachment_scanner is not None or self._static_inspector is not None
            ),
        )
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
        intel_findings: list[SecurityFinding] | None = None,
        attachment_result: AttachmentAssessment | None = None,
        acquisition_mode: AcquisitionMode = AcquisitionMode.LEGACY_BODY_ONLY,
    ) -> SecurityAssessment:
        """Run auth -> inspections -> intel evidence -> policy and build the metadata-only
        assessment for a fully-resolved, scanned message."""
        addresses = _sender_addresses(evidence)
        # Body-only capture: header-looking lines in the stored text are
        # attacker-controlled body content, not captured transport headers, so
        # they must never feed authentication evidence (T-01, T-28, I-02).
        auth_headers = [] if is_body_only(acquisition_mode) else evidence.headers
        auth = build_auth_evidence(
            auth_headers, addresses=addresses, boundary=self._boundary
        )

        findings: list[SecurityFinding] = []
        findings.extend(evidence.to_findings())  # INSPECTION_INCOMPLETE notes
        findings.extend(auth.findings)
        findings.extend(inspect_message(evidence, addresses=addresses))
        # Live threat-intel matches are evidence for the policy only (R-006.2).
        findings.extend(intel_findings or [])
        attachments = attachment_result or AttachmentAssessment()
        # Static attachment findings (and any scanner result) are ordinary
        # evidence for the policy (NS-ADR-014 a).
        findings.extend(attachments.findings)
        # Only a configured scanner/static failure makes inspection incomplete;
        # "no scanner configured" is a recorded not-run check only (b, c).
        inspection_complete = evidence.inspection_complete and not attachments.incomplete_codes
        reason_codes = list(
            dict.fromkeys([*evidence.completeness_codes(), *attachments.incomplete_codes])
        )

        policy_evidence = PolicyEvidence(
            authentication=auth.authentication,
            findings=findings,
            inspection_complete=inspection_complete,
            incomplete_reason_codes=reason_codes,
            bulk_mail=bool(flags.get("bulk") or flags.get("no_reply")),
            reply_to_freemail=is_freemail_address(addresses.reply_to),
            sender_aligned_link_refs=sender_aligned_link_refs(
                evidence, _address_domain(addresses.from_address)
            ),
        )
        result = decide_policy(policy_evidence, clock=self._clock)

        processing_state = (
            ProcessingState.ASSESSED
            if inspection_complete
            else ProcessingState.INCOMPLETE
        )
        return self._build_assessment(
            account_scope=account_scope,
            source_key=source_key,
            processing_state=processing_state,
            evidence_fidelity=evidence_fidelity_for(
                evidence, fidelity_for_mode(acquisition_mode)
            ),
            completeness=Completeness(
                available=[],
                missing=missing_parts_for_mode(acquisition_mode),
                checks_not_run=list(
                    dict.fromkeys(
                        [
                            *reason_codes,
                            *attachments.checks_not_run,
                        ]
                    )
                ),
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
        acquisition_mode: AcquisitionMode = AcquisitionMode.LEGACY_BODY_ONLY,
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
            acquisition_mode=acquisition_mode,
        )
        await self._repo.upsert_evidence(manifest)
        await self._repo.upsert_assessment(assessment)

    async def _commit_verdict(
        self,
        job: Any,
        *,
        content_ref: str | None,
        evidence: ParsedEvidence | None,
        assessment: SecurityAssessment,
        state: str,
        lease_expires_at: datetime | None,
        last_error_code: str | None,
    ) -> VerdictWriteOutcome:
        """Fenced verdict write + job transition in one transaction.

        Rejected (``STALE_FENCE``) when ``(lease_owner, attempts)`` no longer
        matches this claim; in that case nothing is written (R-018, T-17).
        """
        manifest = self._build_manifest(
            account_scope=job.account_scope,
            source_key=job.source_key,
            content_ref=content_ref,
            evidence=evidence,
            evidence_fidelity=assessment.evidence_fidelity,
            acquisition_mode=effective_acquisition_mode(job.meta),
            scan_ref=scan_ref_for(job.meta),
        )
        outcome = await self._repo.commit_verdict_fenced(
            job.account_scope,
            job.id,
            fence_owner=self._worker_id,
            fence_attempts=int(job.attempts or 0),
            manifest=manifest,
            assessment=assessment,
            state=state,
            lease_expires_at=lease_expires_at,
            last_error_code=last_error_code,
        )
        if not outcome.committed:
            self._note_lease_lost(job)
        return outcome

    def _build_manifest(
        self,
        *,
        account_scope: str,
        source_key: str,
        content_ref: str | None,
        evidence: ParsedEvidence | None,
        evidence_fidelity: EvidenceFidelity,
        acquisition_mode: AcquisitionMode = AcquisitionMode.LEGACY_BODY_ONLY,
        scan_ref: str | None = None,
    ) -> MessageEvidenceManifest:
        """Build the immutable per-revision evidence manifest (design §4.1).

        For a trusted raw capture (NS-ADR-064) the manifest points
        ``raw_content_ref`` at the raw RFC822 object and
        ``normalized_content_ref`` at the decoded body; otherwise the body ref
        is the only ref, as before.
        """
        raw_capture = acquisition_mode in RAW_SEAM_MODES and bool(scan_ref)
        raw_ref = scan_ref if raw_capture else content_ref
        normalized_ref = content_ref if raw_capture else None
        sha = evidence.source_sha256 if evidence is not None else None
        missing = list(evidence.completeness_codes()) if evidence is not None else []
        # R-002.3: a body-only capture explicitly marks the evidence it lacks.
        missing.extend(
            code for code in missing_parts_for_mode(acquisition_mode) if code not in missing
        )
        return MessageEvidenceManifest(
            source_id=source_key,
            account_scope=account_scope,
            source_revision=self._source_revision,
            provider=_provider_from_source_key(source_key),
            provider_uid=source_key,
            acquisition_mode=acquisition_mode,
            acquired_at=self._clock(),
            raw_content_ref=_opaque_ref(raw_ref),
            normalized_content_ref=_opaque_ref(normalized_ref),
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
        terminal = (
            JOB_STATE_DEAD_LETTER
            if attempts >= self._max_attempts
            else JOB_STATE_ERROR_RETRYABLE
        )
        outcome = await self._commit_verdict(
            job,
            content_ref=content_ref,
            evidence=None,
            assessment=assessment,
            state=terminal,
            # While RETRYABLE (no owner) this holds the backoff "not before".
            lease_expires_at=(
                None
                if terminal == JOB_STATE_DEAD_LETTER
                else self._policy.not_before(self._clock(), attempts)
            ),
            last_error_code=safe,
        )
        if not outcome.committed:
            return job  # stale worker: reclaimed by another; nothing written
        updated = outcome.job
        log.warning(
            "mail_security.job_failed",
            trace_id=safe_trace_id(job.id),
            state=terminal,
            attempts=attempts,
            error_code=safe,
        )
        return updated if updated is not None else job


# =============================================================================
# small pure helpers
# =============================================================================


def _address_domain(header_value: str | None) -> str | None:
    """Domain of a parsed From header value (offline), or ``None``."""
    import email.utils

    addr = email.utils.parseaddr(header_value or "")[1]
    return addr.rpartition("@")[2].strip().lower() or None if "@" in addr else None


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


#: Job meta key for the source revision (NS-ADR-071); bounded, default 1.
SOURCE_REVISION_META_KEY = "source_revision"
_MAX_SOURCE_REVISION = 100


def source_revision_of(meta: Any) -> int:
    """Revision from trusted job meta; anything unusable is revision 1."""
    value = meta.get(SOURCE_REVISION_META_KEY) if isinstance(meta, dict) else None
    if isinstance(value, bool) or not isinstance(value, int):
        return 1
    return value if 1 <= value <= _MAX_SOURCE_REVISION else 1


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
