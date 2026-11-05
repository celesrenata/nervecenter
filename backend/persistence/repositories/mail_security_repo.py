"""NerveShield mail-security repository (Phase 1, Task 1.7).

Durable, **account-scoped** access to the four minimum security tables
(``mail_security_jobs``, ``mail_evidence``, ``mail_security_assessments``,
``mail_security_findings``). Async and session-scoped like the rest of the
durable plane (see :mod:`backend.persistence.repositories.calendar_repo` /
``relay_seen_repo``): every method opens a session inside a transaction,
commits on success and returns ORM rows.

Isolation contract (R-023.4, T-15) — the whole reason this repository exists:

- **Every** read and write takes an ``account_scope`` and filters on it. There
  is no un-scoped getter. A ``get_*`` handed a *different* ``account_scope``
  than the row's returns ``None`` — never another account's row — even if the
  caller supplies account A's exact deterministic id, source id or content
  hash. Isolation is structural (the scope is a column + index), never the
  secrecy of a hash (design §9).

Idempotency contract (R-018.2, T-17):

- Writes are get-or-create keyed on the deterministic Task 1.2 ids
  (``outbox_job_key`` / ``(source_id, revision)`` / ``assessment_id``). A
  replay after a crash resolves to the existing row rather than inserting a
  duplicate.

Privacy contract (steering, T-14):

- The repository persists only the metadata-only
  :class:`~backend.mail_security.models.SecurityAssessment` /
  :class:`~backend.mail_security.models.MessageEvidenceManifest` payloads and
  the safe scalar columns derived from them. The Pydantic models already
  forbid raw body/subject/addresses (opaque refs + capped codes only), so the
  persisted JSON is metadata-only by construction. No raw content is written
  here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from backend.mail_security.models import (
    MessageEvidenceManifest,
    SecurityAssessment,
)
from backend.persistence.models.mail_security import (
    MailEvidenceRow,
    MailSecurityAssessmentRow,
    MailSecurityFindingRow,
    MailSecurityJobRow,
)


class MailSecurityRepository:
    """Account-scoped durable access to the NerveShield security tables."""

    def __init__(self, session_factory: Any) -> None:
        self._sessions = session_factory

    # -- jobs ---------------------------------------------------------------
    async def upsert_job(
        self,
        *,
        account_scope: str,
        outbox_job_key: str,
        source_key: str,
        analysis_profile_version: str,
        state: str = "DISCOVERED",
        meta: dict[str, Any] | None = None,
    ) -> MailSecurityJobRow:
        """Get-or-create a job by ``(account_scope, outbox_job_key)``.

        Idempotent (R-018.2, T-17): a replay with the same deterministic
        outbox job key resolves to the existing row rather than scheduling a
        duplicate. Only the row's mutable metadata is left untouched on replay;
        the logical identity columns are never rewritten.
        """
        async with self._sessions() as session:
            stmt = select(MailSecurityJobRow).where(
                MailSecurityJobRow.account_scope == account_scope,
                MailSecurityJobRow.outbox_job_key == outbox_job_key,
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is not None:
                return row
            row = MailSecurityJobRow(
                account_scope=account_scope,
                outbox_job_key=outbox_job_key,
                source_key=source_key,
                analysis_profile_version=analysis_profile_version,
                state=state,
                attempts=0,
                meta=meta or {},
            )
            session.add(row)
            await session.commit()
            return row

    async def get_job(
        self, account_scope: str, job_id: str
    ) -> MailSecurityJobRow | None:
        """Fetch a job by row id, enforcing the account scope.

        Returns ``None`` if the row belongs to a different account, even given
        its exact id (R-023.4, T-15).
        """
        async with self._sessions() as session:
            row = await session.get(MailSecurityJobRow, job_id)
        if row is None or row.account_scope != account_scope:
            return None
        return row

    async def get_job_by_key(
        self, account_scope: str, outbox_job_key: str
    ) -> MailSecurityJobRow | None:
        """Fetch a job by its deterministic outbox key within an account."""
        async with self._sessions() as session:
            stmt = select(MailSecurityJobRow).where(
                MailSecurityJobRow.account_scope == account_scope,
                MailSecurityJobRow.outbox_job_key == outbox_job_key,
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def mark_job_state(
        self,
        account_scope: str,
        job_id: str,
        *,
        state: str,
        attempts: int | None = None,
        lease_owner: str | None = None,
        lease_expires_at: datetime | None = None,
        last_error_code: str | None = None,
    ) -> MailSecurityJobRow | None:
        """Patch a job's processing state, scoped to the account.

        Returns ``None`` (no write) if the job belongs to another account.
        """
        async with self._sessions() as session:
            row = await session.get(MailSecurityJobRow, job_id)
            if row is None or row.account_scope != account_scope:
                return None
            row.state = state
            if attempts is not None:
                row.attempts = attempts
            row.lease_owner = lease_owner
            row.lease_expires_at = lease_expires_at
            row.last_error_code = last_error_code
            await session.commit()
            return row

    async def list_jobs(
        self, account_scope: str, *, state: str | None = None
    ) -> list[MailSecurityJobRow]:
        """Every job for one account (optionally filtered by state)."""
        async with self._sessions() as session:
            stmt = select(MailSecurityJobRow).where(
                MailSecurityJobRow.account_scope == account_scope
            )
            if state is not None:
                stmt = stmt.where(MailSecurityJobRow.state == state)
            stmt = stmt.order_by(MailSecurityJobRow.created_at)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    # -- evidence manifest --------------------------------------------------
    async def upsert_evidence(
        self, manifest: MessageEvidenceManifest
    ) -> MailEvidenceRow:
        """Get-or-create an evidence manifest row from a Pydantic manifest.

        Keyed on ``(account_scope, source_id, revision)`` (design §9). The
        manifest carries only metadata + opaque content refs, so the persisted
        row is metadata-only by construction (no raw bytes, T-14). Idempotent
        on replay for a fixed revision.
        """
        async with self._sessions() as session:
            stmt = select(MailEvidenceRow).where(
                MailEvidenceRow.account_scope == manifest.account_scope,
                MailEvidenceRow.source_id == manifest.source_id,
                MailEvidenceRow.revision == manifest.source_revision,
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is not None:
                return row
            row = MailEvidenceRow(
                account_scope=manifest.account_scope,
                source_id=manifest.source_id,
                revision=manifest.source_revision,
                provider=manifest.provider,
                acquisition_mode=str(manifest.acquisition_mode.value),
                evidence_fidelity=str(manifest.evidence_fidelity.value),
                raw_content_ref=manifest.raw_content_ref,
                normalized_content_ref=manifest.normalized_content_ref,
                sha256=manifest.sha256,
                missing_parts=[str(code) for code in manifest.missing_parts],
                meta={},
            )
            session.add(row)
            await session.commit()
            return row

    async def get_evidence(
        self, account_scope: str, source_id: str, revision: int
    ) -> MailEvidenceRow | None:
        """Fetch an evidence manifest, enforcing the account scope.

        Returns ``None`` for another account's manifest even given its exact
        ``source_id`` / ``revision`` (R-023.4, T-15).
        """
        async with self._sessions() as session:
            stmt = select(MailEvidenceRow).where(
                MailEvidenceRow.account_scope == account_scope,
                MailEvidenceRow.source_id == source_id,
                MailEvidenceRow.revision == revision,
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    # -- assessments + findings --------------------------------------------
    async def upsert_assessment(
        self, assessment: SecurityAssessment
    ) -> MailSecurityAssessmentRow:
        """Get-or-create an assessment (and its findings) from a Pydantic model.

        Keyed on ``(account_scope, assessment_id)`` — the deterministic Task
        1.2 id (design §9). Idempotent (R-018.2, I-11): replaying the same
        assessment id yields the single existing row and does not duplicate
        findings. The persisted ``assessment`` JSON is the metadata-only
        ``SecurityAssessment`` payload (opaque refs + safe codes only, no raw
        content — T-14).
        """
        async with self._sessions() as session:
            stmt = select(MailSecurityAssessmentRow).where(
                MailSecurityAssessmentRow.account_scope == assessment.account_scope,
                MailSecurityAssessmentRow.assessment_id == assessment.assessment_id,
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is not None:
                return row

            # Resolve the model version (if any) from the last model run — a
            # safe metadata id, never model output text.
            model_version = (
                assessment.model_runs[-1].model_id
                if assessment.model_runs
                else None
            )
            row = MailSecurityAssessmentRow(
                assessment_id=assessment.assessment_id,
                account_scope=assessment.account_scope,
                source_id=assessment.source_ref,
                revision=assessment.source_revision,
                analysis_profile_version=assessment.analysis_profile_version,
                model_version=model_version,
                policy_version=assessment.policy.policy_version,
                processing_state=str(assessment.processing_state.value),
                evidence_fidelity=str(assessment.evidence_fidelity.value),
                mode=str(assessment.policy.mode.value),
                disposition=str(assessment.policy.disposition.value),
                assessment=assessment.model_dump(mode="json"),
            )
            session.add(row)

            for finding in assessment.findings:
                confidence_semantics: str | None = None
                confidence_value: dict[str, Any] = {"value": None}
                session.add(
                    MailSecurityFindingRow(
                        account_scope=assessment.account_scope,
                        assessment_id=assessment.assessment_id,
                        finding_key=finding.finding_key,
                        rule_id=finding.rule_id,
                        category=str(finding.category.value),
                        severity=str(finding.severity.value),
                        status=str(finding.status.value),
                        confidence_semantics=confidence_semantics,
                        confidence_value=confidence_value,
                        evidence_ref=(
                            finding.evidence_refs[0]
                            if finding.evidence_refs
                            else None
                        ),
                        explanation_code=finding.explanation_code,
                        meta={"evidence_refs": list(finding.evidence_refs)},
                    )
                )
            await session.commit()
            return row

    async def get_assessment(
        self, account_scope: str, assessment_id: str
    ) -> MailSecurityAssessmentRow | None:
        """Fetch an assessment by its deterministic id, enforcing the scope.

        Returns ``None`` for another account's assessment even given its exact
        ``assessment_id`` (R-023.4, T-15).
        """
        async with self._sessions() as session:
            stmt = select(MailSecurityAssessmentRow).where(
                MailSecurityAssessmentRow.account_scope == account_scope,
                MailSecurityAssessmentRow.assessment_id == assessment_id,
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def list_assessments(
        self, account_scope: str, *, disposition: str | None = None
    ) -> list[MailSecurityAssessmentRow]:
        """Every assessment for one account (optionally filtered)."""
        async with self._sessions() as session:
            stmt = select(MailSecurityAssessmentRow).where(
                MailSecurityAssessmentRow.account_scope == account_scope
            )
            if disposition is not None:
                stmt = stmt.where(
                    MailSecurityAssessmentRow.disposition == disposition
                )
            stmt = stmt.order_by(MailSecurityAssessmentRow.created_at)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_findings(
        self, account_scope: str, assessment_id: str
    ) -> list[MailSecurityFindingRow]:
        """Every finding for one assessment, enforcing the account scope.

        Returns an empty list for another account's assessment id (R-023.4).
        """
        async with self._sessions() as session:
            stmt = (
                select(MailSecurityFindingRow)
                .where(
                    MailSecurityFindingRow.account_scope == account_scope,
                    MailSecurityFindingRow.assessment_id == assessment_id,
                )
                .order_by(MailSecurityFindingRow.finding_key)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())


__all__ = ["MailSecurityRepository"]
