"""NerveShield read-only mail-security assessments API (Phase 1, Task 1.10).

The *read surface* over the SHADOW verdicts that the Task 1.9 worker commits to
the durable plane. It exposes two GET endpoints and nothing else:

- ``GET /api/mail-security/assessments`` — a paginated, account-scoped,
  disposition-filterable list of redacted verdicts with an opaque continuation
  cursor.
- ``GET /api/mail-security/assessments/{assessment_id}`` — one redacted
  assessment plus its findings and the checks that were *not* performed
  (R-020.1 "unavailable checks"), account-scoped.

Hard contract (requirements/design/threat-model — stricter behavior wins):

- **Authorization + account isolation (R-023, T-15, design §9).** Every query
  is constrained to the caller's authenticated account scope via the Task 1.7
  account-scoped repository. The caller's authorized scopes come from the
  verified access token (the ``account_scopes`` claim), never from a
  client-supplied value. The optional ``?account_id=`` filter is *intersected*
  with the caller's authorized scopes — it can only ever narrow, never widen,
  access. A detail request for an assessment id that belongs to a different
  account returns ``404`` (never the row, never a 403 that would confirm the
  id exists in another account), even when the caller supplies the exact id
  (R-023.4, T-15).

  Opt-in ``NERVE_MAIL_SECURITY_API_SCOPE_MODE=single_tenant`` (NS-ADR-025)
  additionally resolves ``conn:<id>`` server-side for enabled mail
  connections for human access tokens only (claim, if present, can only
  narrow). Default / any other value is ``claim_only``. ``src:`` scopes are
  never server-resolved.

- **Read-only, no side effects (R-019, T-26, steering).** This module mounts
  only GET routes. It performs no writes, schedules no jobs, mints no
  attention, calls no model/network and mutates no provider state. Reviewing a
  verdict here is not authorizing anything (T-26).

- **Redaction + safe errors (R-017.2, T-14, R-022.4).** Responses carry only
  the metadata / opaque-ref fields from the SecurityAssessment / SecurityFinding
  contracts — disposition, reason/explanation codes, severities, confidence
  *semantics* (type/calibration status, never a raw probability presented as
  verified), provenance, opaque evidence refs and not-evaluated statuses. The
  stored payload is metadata-only by construction (the Task 1.1 models forbid
  raw body/subject/addresses), but the projection here is an *explicit
  allow-list* so the API never widens what the storage layer happens to hold.
  Error bodies are safe-shaped: a stable ``error_code``, no stack traces, no
  raw content and no identifier that would confirm another account's row.

- **Bounded pagination (rate control).** The repo exposes no app-level
  rate-limiting mechanism, so the enforced control here is a bounded maximum
  page size (:data:`MAX_PAGE_SIZE`): a caller cannot request an unbounded page.
  The cursor is an opaque base64 offset token following the repo's
  ``_meta(total/page/per_page)`` + envelope convention.

``GET /api/mail-security/assessments/for-communication/{communication_id}``
(Task 7.1) returns the same redacted detail for the newest assessment of one
committed mail communication, under the same scope rules and safe ``404``.

A further read-only route, ``GET /api/mail-security/health`` (Task 2.6), returns
the per-stage health summary computed by :mod:`backend.mail_security.
observability` for the caller's authorized account scopes ONLY. A caller with no
scopes gets the same safe ``404`` as the detail route and never sees a global
count. The response is an allow-listed, identifier-free projection (no account,
message or source ids, no free text).

The router is always registered (like the other read routers): with no durable
registry or no stored assessments it returns an empty list / ``404``, exposing
nothing when the feature has produced no data.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from backend.api.auth import current_access
from backend.api.models import ApiResponseMeta, ListResponse
from backend.mail_security.review_feedback import verification_advice

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/mail-security", tags=["mail-security"])

#: Hard upper bound on a single page (the enforced rate/abuse control — see the
#: module docstring: the repo has no app-level rate limiter, so a bounded page
#: size is the control). A request for more is clamped to this ceiling.
MAX_PAGE_SIZE = 100

#: Default page size when the caller does not specify one.
DEFAULT_PAGE_SIZE = 50


# --------------------------------------------------------------------------- #
# metadata / envelope helpers (mirror attention.py / calendar.py conventions)
# --------------------------------------------------------------------------- #


def _meta(**pagination: int | None) -> ApiResponseMeta:
    """Build the shared response envelope metadata.

    ``pagination`` carries the ``total`` / ``page`` / ``per_page`` ints exactly
    like the other list endpoints (attention.py / calendar.py). The opaque
    ``next_cursor`` continuation token is returned as a *sibling* of ``data`` /
    ``meta`` (not inside ``pagination``, which the shared model types as ints)
    so clients page without ever seeing an internal offset.
    """
    return ApiResponseMeta(
        version="1.0.0",
        request_id=f"req-{uuid4().hex[:12]}",
        timestamp=datetime.now(UTC),
        pagination={k: v for k, v in pagination.items() if v is not None} or None,
    )


def _safe_error(status_code: int, error_code: str) -> HTTPException:
    """A safe-shaped error (stable ``error_code`` only — never raw content).

    No message body text, stack trace, internal id or other-account existence
    signal is ever included (T-14, R-022.4). The same opaque ``not_found`` code
    is used whether an assessment is absent or belongs to another account, so a
    cross-account probe cannot distinguish "does not exist" from "exists but is
    not yours" (R-023.4, T-15).
    """
    return HTTPException(
        status_code=status_code, detail={"error_code": error_code}
    )


# --------------------------------------------------------------------------- #
# cursor (opaque base64 offset continuation token)
# --------------------------------------------------------------------------- #


def _encode_cursor(offset: int) -> str:
    """Encode a non-negative offset into an opaque continuation token."""
    return base64.urlsafe_b64encode(f"o:{offset}".encode()).decode("ascii")


def _decode_cursor(cursor: str | None) -> int:
    """Decode a continuation token into an offset (422 on a malformed token).

    The token is opaque to clients; a tampered / non-numeric token is a safe
    ``invalid_cursor`` 422, never a 500 and never a leak of internal structure.
    """
    if cursor is None:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii")
        if not raw.startswith("o:"):
            raise ValueError("bad prefix")
        offset = int(raw[2:])
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise _safe_error(422, "invalid_cursor") from exc
    if offset < 0:
        raise _safe_error(422, "invalid_cursor")
    return offset


# --------------------------------------------------------------------------- #
# caller account scope resolution (authorization boundary)
# --------------------------------------------------------------------------- #


def _authorized_scopes(claims: dict[str, Any]) -> list[str]:
    """Resolve the caller's authorized account scopes from the verified token.

    The scopes come *only* from the ``account_scopes`` claim on the verified
    access token (a list of ``account_scope`` strings). There is no scope
    registry to enumerate, so the default is deny-by-default: a token with no
    ``account_scopes`` claim authorizes no scope and therefore sees nothing.
    This is the strict R-017 least-privilege posture — access is never widened
    by anything the client sends in the request.
    """
    raw = claims.get("account_scopes")
    if not isinstance(raw, list):
        return []
    return [s for s in raw if isinstance(s, str) and s]


#: Environment variable selecting the scope mode (NS-ADR-025).
SCOPE_MODE_ENV = "NERVE_MAIL_SECURITY_API_SCOPE_MODE"
MODE_CLAIM_ONLY = "claim_only"
MODE_SINGLE_TENANT = "single_tenant"


def scope_mode() -> str:
    """The configured scope mode; ANY value but exactly ``single_tenant`` is
    ``claim_only`` (unset, empty, unknown or garbled never fails open)."""
    raw = os.environ.get(SCOPE_MODE_ENV)
    if isinstance(raw, str) and raw.strip().lower() == MODE_SINGLE_TENANT:
        return MODE_SINGLE_TENANT
    return MODE_CLAIM_ONLY


def _eligible_for_server_scopes(claims: dict[str, Any]) -> bool:
    """Humans only: no device credential and a non-empty ``sub`` (NS-ADR-025)."""
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        return False
    if sub.startswith("device:") or claims.get("typ") == "device":
        return False
    roles = claims.get("roles")
    if isinstance(roles, (list, tuple, set)) and "device" in roles:
        return False
    return claims.get("typ") == "access"


async def _server_resolved_scopes(request: Request) -> list[str]:
    """``conn:<id>`` for each ENABLED mail-kind connection (ids only).

    Mail kinds and the scope shape are the ingest supervisor's / backfill's own
    definitions. Resolved per request (no cache). Any failure or an absent
    durable plane yields NO scopes (fail closed); only a safe code is logged.
    """
    from backend.mail_security.backfill import _SCOPE_RE, _supported_kinds

    registry = getattr(request.app.state, "repository_registry", None)
    if registry is None:
        log.warning("mail_security.api.scope_resolve_failed", code="no_plane")
        return []
    try:
        ids = await registry.connections.list_enabled_ids_for_kinds(
            set(_supported_kinds())
        )
    except Exception:  # noqa: BLE001 - fail closed, log a safe code only
        log.warning("mail_security.api.scope_resolve_failed", code="lookup_error")
        return []
    out = [f"conn:{i}" for i in ids if _SCOPE_RE.fullmatch(f"conn:{i}")]
    log.debug("mail_security.api.scopes_resolved", mode=MODE_SINGLE_TENANT, count=len(out))
    return out


async def _caller_scopes(request: Request, claims: dict[str, Any]) -> list[str]:
    """The caller's effective scopes under the configured mode.

    ``claim_only``: the ``account_scopes`` claim alone (unchanged).
    ``single_tenant``: the server-resolved set for eligible principals,
    INTERSECTED with the claim when the token carries one (claim only narrows).
    Ineligible principals get claim-only semantics.
    """
    claimed = _authorized_scopes(claims)
    if scope_mode() != MODE_SINGLE_TENANT or not _eligible_for_server_scopes(claims):
        return claimed
    resolved = await _server_resolved_scopes(request)
    if "account_scopes" in claims:
        allowed = set(claimed)
        return [s for s in resolved if s in allowed]
    return resolved


async def _resolve_query_scopes(
    request: Request, claims: dict[str, Any], account_id: str | None
) -> list[str]:
    """Intersect an optional client ``account_id`` with the authorized scopes.

    The design's ``?account_id=`` filter can only ever *narrow* access: it is
    intersected with the caller's authorized scopes, never trusted to widen
    them (R-023, design §9). An ``account_id`` the caller is not authorized for
    yields an empty set (the caller simply sees nothing for it — no 403 that
    would confirm the scope exists).
    """
    authorized = await _caller_scopes(request, claims)
    if account_id is None:
        return authorized
    return [s for s in authorized if s == account_id]


# --------------------------------------------------------------------------- #
# redaction projections (explicit allow-list — never echo the stored blob)
# --------------------------------------------------------------------------- #


class AssessmentSummary(BaseModel):
    """Redacted list-row view of a stored assessment (safe metadata only)."""

    assessment_id: str
    account_scope: str
    disposition: str
    mode: str
    processing_state: str
    evidence_fidelity: str
    policy_version: str
    analysis_profile_version: str
    created_at: str | None


def _summary(row: Any) -> dict[str, Any]:
    """Project a stored assessment row to the redacted list row.

    Only the safe scalar metadata columns are emitted; the full ``assessment``
    JSON blob is deliberately *not* returned here (T-14/R-017.2).
    """
    return AssessmentSummary(
        assessment_id=row.assessment_id,
        account_scope=row.account_scope,
        disposition=row.disposition,
        mode=row.mode,
        processing_state=row.processing_state,
        evidence_fidelity=row.evidence_fidelity,
        policy_version=row.policy_version,
        analysis_profile_version=row.analysis_profile_version,
        created_at=row.created_at.isoformat() if row.created_at else None,
    ).model_dump(mode="json")


def _redact_finding(row: Any) -> dict[str, Any]:
    """Project a stored finding row to a redacted view (opaque refs + codes)."""
    confidence = row.confidence_value or {}
    return {
        "finding_key": row.finding_key,
        "rule_id": row.rule_id,
        "category": row.category,
        "severity": row.severity,
        "status": row.status,
        # Confidence *semantics* only (what the value means), never a raw model
        # probability presented as verified truth (steering: estimates are
        # unverified until calibrated).
        "confidence_semantics": row.confidence_semantics,
        "confidence_type": (
            "ABSENT" if confidence.get("value") is None else "ESTIMATE"
        ),
        "explanation_code": row.explanation_code,
        "evidence_refs": list((row.meta or {}).get("evidence_refs") or []),
    }


def _axis_view(axis: dict[str, Any] | None) -> dict[str, Any]:
    """Redact one trust axis to its semantics + calibration status.

    A raw/uncalibrated ``value`` is never surfaced as a verified probability;
    only the ``semantics`` (e.g. ``NOT_EVALUATED`` / ``RAW_SCORE`` /
    ``CALIBRATED_PROBABILITY``) and whether a calibration id is present are
    exposed (R-020.1 confidence/calibration).
    """
    axis = axis or {}
    semantics = axis.get("semantics")
    calibrated = (
        bool(axis.get("calibration_id"))
        and semantics == "CALIBRATED_PROBABILITY"
    )
    return {
        "semantics": semantics,
        "calibrated": calibrated,
        "value": axis.get("value") if calibrated else None,
    }


def _redact_detail(row: Any, findings: list[Any]) -> dict[str, Any]:
    """Project a stored assessment + findings to the full redacted detail view.

    Reads the metadata-only ``assessment`` JSON payload for the provenance /
    reason-code / axis / completeness fields but re-projects through an explicit
    allow-list; it never returns the raw stored blob (R-017.2, T-14). The
    ``checks_not_run`` list is R-020.1's "unavailable checks".
    """
    payload = row.assessment or {}
    policy = payload.get("policy") or {}
    authentication = payload.get("authentication") or {}
    completeness = payload.get("completeness") or {}
    axes = payload.get("axes") or {}

    auth_view = {
        name: {
            "result": (authentication.get(name) or {}).get("result"),
            "provenance": (authentication.get(name) or {}).get("provenance"),
        }
        for name in ("spf", "dkim", "dmarc", "arc")
    }

    return {
        **_summary(row),
        "reason_codes": list(policy.get("reason_codes") or []),
        "human_approval_required": bool(policy.get("human_approval_required")),
        # SHADOW grants nothing; surface the (empty) capability codes honestly.
        "action_capabilities": list(policy.get("action_capabilities") or []),
        "authentication": auth_view,
        "axes": {name: _axis_view(axes.get(name)) for name in axes},
        "completeness": {
            "available": list(completeness.get("available") or []),
            "missing": list(completeness.get("missing") or []),
            # R-020.1 "unavailable checks": checks that were NOT performed.
            "checks_not_run": list(completeness.get("checks_not_run") or []),
        },
        "findings": [_redact_finding(f) for f in findings],
        # Task 7.2 (R-013.4): independent-verification advice codes derived
        # from findings + reason codes only; shown even when auth passes.
        "verification_advice": verification_advice(
            ((f.category, f.status) for f in findings),
            policy.get("reason_codes") or [],
        ),
    }


# --------------------------------------------------------------------------- #
# repository access
# --------------------------------------------------------------------------- #


def _repo(request: Request) -> Any | None:
    """The account-scoped Task 1.7 mail-security repository, or ``None``.

    Resolved from the single durable ``repository_registry`` on ``app.state``.
    ``None`` when no durable plane is wired — the endpoints then degrade to an
    empty list / ``404`` (never a fake success, never a 500).
    """
    registry = getattr(request.app.state, "repository_registry", None)
    if registry is None:
        return None
    try:
        return registry.mail_security
    except Exception:  # noqa: BLE001 - absent optional plane degrades to empty
        return None


# --------------------------------------------------------------------------- #
# routes (GET only — read-only surface)
# --------------------------------------------------------------------------- #


@router.get("/health")
async def get_health(
    request: Request,
    claims: Annotated[dict[str, Any], Depends(current_access)],
) -> dict[str, Any]:
    """Per-stage health for the caller's authorized account scopes only.

    Deny-by-default (R-023, T-15): no ``account_scopes`` claim, or no wired
    durable plane, is a safe ``404`` -- the caller never sees global counts.
    With the feature flag OFF every stage reports ``DISABLED`` and no
    repository read happens. Read-only; the body carries enums, counts and
    numbers only (R-017.2, R-021.4).
    """
    from backend.mail_security import observability as obs

    repo = _repo(request)
    scopes = await _caller_scopes(request, claims)
    if repo is None or not scopes:
        raise _safe_error(404, "not_found")

    now = datetime.now(UTC)
    feed_rows = None
    if obs.mail_security_enabled():
        registry = getattr(request.app.state, "repository_registry", None)
        factory = getattr(registry, "session_factory", None)
        if factory is not None:
            feed_rows = await obs.collect_feed_rows(factory)
    snapshot = await obs.collect_snapshot(
        repo, scopes, now=now, feed_rows=feed_rows
    )
    return {"data": obs.health_view(snapshot)}


@router.get("/assessments")
async def list_assessments(
    request: Request,
    claims: Annotated[dict[str, Any], Depends(current_access)],
    account_id: Annotated[str | None, Query()] = None,
    disposition: Annotated[str | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> dict[str, Any]:
    """List redacted SHADOW verdicts for the caller's authorized account(s).

    Account-scoped (R-023): results are restricted to the caller's authorized
    scopes; an optional ``account_id`` can only narrow that set. Paginated with
    an opaque ``cursor`` and a bounded ``limit`` (clamped to
    :data:`MAX_PAGE_SIZE`). Returns an empty list — never a 401/leak — when the
    caller has no authorized scopes or the durable plane is not wired.
    """
    # Defense in depth: FastAPI already enforces ``le=MAX_PAGE_SIZE`` (422 on
    # overflow), and this clamp guarantees the bound even if the validator is
    # ever relaxed.
    per_page = min(max(1, limit), MAX_PAGE_SIZE)
    offset = _decode_cursor(cursor)

    scopes = await _resolve_query_scopes(request, claims, account_id)
    repo = _repo(request)

    rows: list[Any] = []
    if repo is not None and scopes:
        for scope in scopes:
            rows.extend(
                await repo.list_assessments(scope, disposition=disposition)
            )
    # Stable ordering across scopes so the offset cursor is deterministic.
    rows.sort(key=lambda r: (r.created_at or datetime.min, r.assessment_id))

    total = len(rows)
    page_rows = rows[offset : offset + per_page]
    next_offset = offset + per_page
    next_cursor = _encode_cursor(next_offset) if next_offset < total else None

    envelope = ListResponse(
        data=[_summary(r) for r in page_rows],
        meta=_meta(
            total=total,
            page=offset // per_page + 1,
            per_page=per_page,
        ),
    )
    return {**envelope.model_dump(mode="json"), "next_cursor": next_cursor}


#: Communication ids the mail worker mints: ``comm-mail-<24 lowercase hex>``.
_COMMUNICATION_ID_RE = re.compile(r"comm-mail-(?P<digest>[0-9a-f]{24})")


@router.get("/assessments/for-communication/{communication_id}")
async def get_assessment_for_communication(
    request: Request,
    communication_id: str,
    claims: Annotated[dict[str, Any], Depends(current_access)],
) -> dict[str, Any]:
    """The newest redacted assessment for one committed mail communication.

    Resolution (Task 7.1): communication -> its opaque ``content_ref`` ->
    owning ``conn:<id>`` scope (must be one of the caller's scopes) -> the job
    scheduled for that exact ref -> its ``source_key`` -> the newest assessment
    for that source. The ref's digest must equal the communication id's digest
    (both are ``sha256(conn:mail_id)[:24]``), binding the row to its id. Every
    miss, malformed id or other-account row is the same safe ``404``.
    """
    from backend.mail_security.backfill import parse_content_ref

    match = _COMMUNICATION_ID_RE.fullmatch(communication_id)
    repo = _repo(request)
    registry = getattr(request.app.state, "repository_registry", None)
    if match is None or repo is None or registry is None:
        raise _safe_error(404, "not_found")
    scopes = set(await _caller_scopes(request, claims))
    if not scopes:
        raise _safe_error(404, "not_found")
    try:
        rows = await registry.communications.get_communications_by_ids(
            [communication_id]
        )
    except Exception:  # noqa: BLE001 - absent plane / lookup error is a 404
        raise _safe_error(404, "not_found") from None
    ref = getattr(rows[0], "content_ref", None) if rows else None
    parsed = parse_content_ref(ref)
    if parsed is None or parsed.object_digest != match["digest"]:
        raise _safe_error(404, "not_found")
    scope = f"conn:{parsed.connection_id}"
    if scope not in scopes:
        raise _safe_error(404, "not_found")
    source_key = await repo.source_key_for_content_ref(scope, str(ref))
    if source_key is None:
        raise _safe_error(404, "not_found")
    row = await repo.latest_assessment_for_source(scope, source_key)
    if row is None:
        raise _safe_error(404, "not_found")
    findings = await repo.get_findings(scope, row.assessment_id)
    return _redact_detail(row, findings)


@router.get("/assessments/{assessment_id}")
async def get_assessment(
    request: Request,
    assessment_id: str,
    claims: Annotated[dict[str, Any], Depends(current_access)],
) -> dict[str, Any]:
    """Return one redacted assessment + findings for the caller's account.

    Account isolation (R-023.4, T-15): the assessment is looked up under each
    of the caller's authorized scopes only. An id that belongs to a different
    account is indistinguishable from one that does not exist — both are a safe
    ``404`` ``not_found`` — even when the caller supplies the exact id. The
    detail includes the "checks not run" list (R-020.1 unavailable checks).
    """
    repo = _repo(request)
    scopes = await _caller_scopes(request, claims)
    if repo is None or not scopes:
        raise _safe_error(404, "not_found")

    for scope in scopes:
        row = await repo.get_assessment(scope, assessment_id)
        if row is not None:
            findings = await repo.get_findings(scope, assessment_id)
            return _redact_detail(row, findings)

    # Not in any authorized scope: deny without confirming existence elsewhere.
    raise _safe_error(404, "not_found")
