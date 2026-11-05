"""Authentication-evidence representation for NerveShield (Task 1.4).

This module turns *supplied* mail headers (plus an offline view of the From /
Reply-To / Return-Path addresses) into the typed, honest authentication
evidence the rest of the pipeline consumes. It is **additive and pure**: no
wiring, no API, no migration, no event subject, and crucially **no network and
no DNS**. It never performs a live SPF lookup, a DKIM DNS key fetch, or a DMARC
record resolution — it only *classifies evidence that is already present* in
the headers it is handed.

Design anchors (`.kiro/specs/nerve-shield/design.md` §5.1 "Auth and
provenance"; requirements R-002.4/.5, R-004.1–.5; threats T-01/T-02/T-03/T-28):

- **State and provenance are independent.** For each of SPF/DKIM/DMARC/ARC we
  emit a result state in {PASS, FAIL, NEUTRAL, UNKNOWN} (the
  :class:`~backend.mail_security.models.AuthResult` contract) *and* a separate
  provenance in {INDEPENDENT, PROVIDER_REPORTED, UNTRUSTED_HEADER, UNKNOWN}
  (:class:`~backend.mail_security.models.AuthProvenance`). The two never
  collapse into a single "trusted" flag.

- **INDEPENDENT is unreachable here, by design.** True SPF validation needs the
  original SMTP client IP + envelope; direct historical DKIM verification needs
  the complete unmodified signed MIME plus DNS keys. We have neither from
  headers alone, so header classification can only ever produce
  ``PROVIDER_REPORTED`` or ``UNTRUSTED_HEADER`` (or ``UNKNOWN``). ``INDEPENDENT``
  is reserved for a future task that carries real transport/crypto evidence;
  the production path to it here is deliberately unreachable (T-01, design
  §5.1, tasks.md "SPF declared independently verified without necessary
  transport evidence").

- **Trusted-receiver boundary decides provider-reported vs forged.** An
  ``Authentication-Results`` header is ``PROVIDER_REPORTED`` **only** when its
  ``authserv-id`` is inside the configured :class:`TrustedReceiverBoundary`. An
  ``Authentication-Results`` whose origin is not attested by a trusted boundary
  is ``UNTRUSTED_HEADER`` — never verified (R-004.2, T-01 "Forged trusted relay
  results" red-team template).

- **Legacy body-only is UNKNOWN.** When no auth headers are supplied — the
  common case today, where transports capture BODY_ONLY — every axis is
  ``UNKNOWN`` with provenance ``UNKNOWN`` and a ``NO_AUTH_EVIDENCE_CAPTURED``
  completeness code. We never default to ``PASS`` or any verified state
  (T-28, invariant I-02).

- **Auth is not a trust flag (I-06 / R-004.4 / T-02).** The result object
  carries observations and findings only. There is no ``allow`` / ``trusted`` /
  ``safe`` field; a DKIM=pass can never short-circuit content/intent evidence.

- **Sender provenance is offline (R-004.3, T-03).** From vs Reply-To vs
  Return-Path mismatches and Unicode/IDNA confusable-domain signals are emitted
  as typed findings, computed with no network. Full registrable-domain /
  canonicalization overlaps with Task 1.5's URL inspections; the parser does
  not yet expose domain canonicalization, so the confusable check here is kept
  intentionally minimal and offline, and the richer overlap is deferred to 1.5.

- **Reputation unavailable => UNKNOWN (R-004.5).** This module never asserts a
  reputation; absence is represented as UNKNOWN, not as safe or malicious.

Emitted types reuse the existing contract models
(:mod:`backend.mail_security.models`) — :class:`AuthObservation`,
:class:`Authentication`, :class:`AuthResult`, :class:`AuthProvenance`,
:class:`Completeness`, :class:`SecurityFinding` — without duplicating or
extending them. No model change is required.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field

from backend.mail_security.models import (
    Authentication,
    AuthObservation,
    AuthProvenance,
    AuthResult,
    Code,
    Completeness,
    FindingCategory,
    FindingSeverity,
    FindingStatus,
    SecurityFinding,
)

# -- detector identity -------------------------------------------------------

#: Stable version recorded on every emitted finding so deterministic auth
#: evidence is reproducible/auditable (R-002, R-005.5).
AUTH_EVIDENCE_VERSION = "auth-evidence@1"

#: Completeness code recorded when no authentication headers were supplied —
#: the current real BODY_ONLY case. Surfaced in ``checks_not_run`` so a reader
#: sees auth was *not evaluated*, never silently clean (T-28, I-02).
NO_AUTH_EVIDENCE_CAPTURED: Code = "NO_AUTH_EVIDENCE_CAPTURED"

#: Rule ids (stable grouping keys for dashboards).
_RULE_AUTH_PROVENANCE = "auth.results.provenance"
_RULE_SENDER_MISMATCH = "auth.sender.mismatch"
_RULE_CONFUSABLE = "auth.sender.confusable"

#: The four authentication axes, in schema order.
_AXES: tuple[str, ...] = ("spf", "dkim", "dmarc", "arc")

#: Raw auth-result tokens that map cleanly onto the bounded
#: :class:`AuthResult` contract. Anything else (softfail, temperror,
#: permerror, policy, unknown tokens) degrades to ``UNKNOWN`` rather than being
#: forced into a PASS/FAIL it does not justify (design §5.1: do not invent
#: states; I-02: never upgrade an ambiguous token to clean).
_RESULT_TOKEN_MAP: dict[str, AuthResult] = {
    "pass": AuthResult.PASS,
    "fail": AuthResult.FAIL,
    "neutral": AuthResult.NEUTRAL,
    "none": AuthResult.NEUTRAL,
}


# =============================================================================
# configuration: trusted receiver boundary
# =============================================================================


@dataclass(frozen=True)
class TrustedReceiverBoundary:
    """Configured set of ``authserv-id`` values we attest actually added an
    ``Authentication-Results`` header (design §5.1, R-004.2).

    Only a header whose ``authserv-id`` is inside this boundary may be treated
    as ``PROVIDER_REPORTED``; every other ``Authentication-Results`` is a
    forgeable ``UNTRUSTED_HEADER``. The boundary is *not* a trust/allow switch:
    it only distinguishes "our receiver said this" from "someone wrote this".

    ``authserv_ids`` is normalized to a lowercase frozenset on construction so
    matching is case-insensitive and the config is immutable (``frozen=True``).
    An empty boundary means no origin can be attested, so every header becomes
    ``UNTRUSTED_HEADER`` — the safe default.
    """

    authserv_ids: frozenset[str] = field(default_factory=frozenset)

    def __init__(self, authserv_ids: Iterable[str] | None = None) -> None:
        normalized: set[str] = set()
        if authserv_ids is not None:
            if isinstance(authserv_ids, str):
                raise TypeError(
                    "authserv_ids must be an iterable of strings, not a single str"
                )
            for item in authserv_ids:
                if not isinstance(item, str):
                    raise TypeError("each authserv-id must be a string")
                token = item.strip().lower()
                if token:
                    normalized.add(token)
        # frozen dataclass: set the field via object.__setattr__.
        object.__setattr__(self, "authserv_ids", frozenset(normalized))

    def attests(self, authserv_id: str | None) -> bool:
        """Return ``True`` iff this boundary attests the given authserv-id."""
        if not authserv_id:
            return False
        return authserv_id.strip().lower() in self.authserv_ids


# =============================================================================
# offline sender address view
# =============================================================================


@dataclass(frozen=True)
class SenderAddresses:
    """Offline view of the addresses needed for sender-provenance checks.

    All fields are optional raw header values (already extracted upstream). This
    module performs no parsing beyond a bounded local address/domain split and
    never resolves anything over the network (R-004.3).
    """

    from_address: str | None = None
    reply_to: str | None = None
    return_path: str | None = None


# =============================================================================
# typed result
# =============================================================================


@dataclass(frozen=True)
class AuthEvidenceResult:
    """Typed authentication evidence for one message.

    Deliberately carries **no** allow/trust/safe flag (I-06, R-004.4): it is an
    evidence bundle, not a decision. Downstream policy (Task 1.6) combines this
    with content/intent evidence; a DKIM=pass here can never short-circuit that.
    """

    authentication: Authentication
    completeness: Completeness
    findings: list[SecurityFinding] = field(default_factory=list)


# =============================================================================
# Authentication-Results parsing (bounded, offline)
# =============================================================================


@dataclass(frozen=True)
class _ParsedAuthResults:
    """One parsed ``Authentication-Results`` header: authserv-id + methods."""

    authserv_id: str | None
    methods: dict[str, AuthResult]


def _parse_authentication_results(value: str) -> _ParsedAuthResults:
    """Parse a single ``Authentication-Results`` value into authserv-id +
    method→state, bounded and never raising.

    Grammar (RFC 8601, simplified for offline evidence): the value starts with
    the ``authserv-id`` token, then ``;``-separated ``method=result`` clauses
    with optional ``property=value`` ptype tokens we ignore. We only extract the
    method keyword and its result token; anything we cannot map becomes
    ``UNKNOWN`` (we never guess a PASS).
    """
    text = (value or "").strip()
    parts = [segment.strip() for segment in text.split(";")]

    authserv_id: str | None = None
    if parts:
        # The authserv-id is the first token of the first clause (it may carry
        # an optional version number, e.g. "mx.google.com 1"). An empty leading
        # clause means no resolvable authserv-id.
        head = parts[0].split()
        if head:
            authserv_id = head[0].strip() or None

    methods: dict[str, AuthResult] = {}
    for clause in parts[1:]:
        if "=" not in clause:
            continue
        # The method token is the first "method=result" pair of the clause.
        first_token = clause.split()[0] if clause.split() else clause
        if "=" not in first_token:
            continue
        method_name, _, result_token = first_token.partition("=")
        method = method_name.strip().lower()
        if method not in _AXES:
            continue
        result = _RESULT_TOKEN_MAP.get(result_token.strip().lower(), AuthResult.UNKNOWN)
        # If the same method appears twice, keep the first seen (stable).
        methods.setdefault(method, result)

    return _ParsedAuthResults(authserv_id=authserv_id, methods=methods)


def _collect_header_values(headers: object) -> list[str]:
    """Extract all ``Authentication-Results`` values from a header sequence.

    Accepts the ``list[tuple[name, value]]`` shape used by
    :class:`~backend.mail_security.parser.ParsedEvidence.headers`. Matching is
    case-insensitive on the header name. Never raises on odd input.
    """
    values: list[str] = []
    if not headers:
        return values
    try:
        for entry in headers:  # type: ignore[union-attr]
            if (
                isinstance(entry, tuple)
                and len(entry) == 2
                and isinstance(entry[0], str)
                and isinstance(entry[1], str)
                and entry[0].strip().lower() == "authentication-results"
            ):
                values.append(entry[1])
    except TypeError:
        return values
    return values


# =============================================================================
# public: classify authentication axes
# =============================================================================


def classify_authentication(
    headers: object,
    *,
    boundary: TrustedReceiverBoundary,
) -> AuthEvidenceResult:
    """Classify SPF/DKIM/DMARC/ARC evidence present in ``headers``.

    ``headers`` is a sequence of ``(name, value)`` pairs (e.g.
    ``ParsedEvidence.headers``). For each axis we emit an
    :class:`AuthObservation` whose ``result`` is the parsed state and whose
    ``provenance`` is decided by the trusted-receiver boundary:

    - an axis asserted by an ``Authentication-Results`` header whose authserv-id
      is inside ``boundary`` => ``PROVIDER_REPORTED``;
    - an axis asserted only by headers outside the boundary => ``UNTRUSTED_HEADER``;
    - no auth header at all => ``UNKNOWN`` (+ ``NO_AUTH_EVIDENCE_CAPTURED``).

    ``INDEPENDENT`` is never produced here (we lack transport/crypto evidence).
    Never raises on malformed input: unparseable headers leave axes ``UNKNOWN``.
    """
    if not isinstance(boundary, TrustedReceiverBoundary):
        raise TypeError("boundary must be a TrustedReceiverBoundary")

    raw_values = _collect_header_values(headers)

    # Per axis, remember the best evidence we found and whether it came from a
    # trusted (provider-reported) or untrusted origin. Trusted evidence wins
    # over untrusted for the same axis so a forged duplicate cannot downgrade a
    # genuine provider assertion — but it can never *upgrade* to independent.
    trusted_results: dict[str, AuthResult] = {}
    untrusted_results: dict[str, AuthResult] = {}
    saw_untrusted_origin = False
    saw_malformed = False

    for value in raw_values:
        parsed = _parse_authentication_results(value)
        if not parsed.methods and parsed.authserv_id is None:
            saw_malformed = True
        is_trusted = boundary.attests(parsed.authserv_id)
        if not is_trusted and (parsed.methods or parsed.authserv_id is None):
            # A header that asserts something but is not attested is forgeable.
            if parsed.methods:
                saw_untrusted_origin = True
        for method, result in parsed.methods.items():
            if is_trusted:
                trusted_results.setdefault(method, result)
            else:
                untrusted_results.setdefault(method, result)

    observations: dict[str, AuthObservation] = {}
    for axis in _AXES:
        if axis in trusted_results:
            observations[axis] = AuthObservation(
                result=trusted_results[axis],
                provenance=AuthProvenance.PROVIDER_REPORTED,
                evidence_refs=[],
            )
        elif axis in untrusted_results:
            observations[axis] = AuthObservation(
                result=untrusted_results[axis],
                provenance=AuthProvenance.UNTRUSTED_HEADER,
                evidence_refs=[],
            )
        else:
            observations[axis] = AuthObservation(
                result=AuthResult.UNKNOWN,
                provenance=AuthProvenance.UNKNOWN,
                evidence_refs=[],
            )

    authentication = Authentication(
        spf=observations["spf"],
        dkim=observations["dkim"],
        dmarc=observations["dmarc"],
        arc=observations["arc"],
    )

    findings: list[SecurityFinding] = []
    checks_not_run: list[Code] = []
    available: list[Code] = []

    if not raw_values:
        # Legacy body-only: nothing to evaluate. Mark not-run, stay UNKNOWN.
        checks_not_run.append(NO_AUTH_EVIDENCE_CAPTURED)
    else:
        available.append("AUTH_RESULTS_PRESENT")

    if saw_untrusted_origin:
        findings.append(
            _finding(
                finding_key="auth-results-untrusted-origin",
                rule_id=_RULE_AUTH_PROVENANCE,
                category=FindingCategory.AUTH_PROVENANCE,
                severity=FindingSeverity.MEDIUM,
                status=FindingStatus.OBSERVED,
                explanation_code="AUTH_RESULTS_UNTRUSTED_ORIGIN",
            )
        )

    if saw_malformed:
        findings.append(
            _finding(
                finding_key="auth-results-malformed",
                rule_id=_RULE_AUTH_PROVENANCE,
                category=FindingCategory.AUTH_PROVENANCE,
                severity=FindingSeverity.UNKNOWN,
                status=FindingStatus.INCOMPLETE,
                explanation_code="AUTH_RESULTS_MALFORMED",
            )
        )

    completeness = Completeness(
        available=available,
        missing=[],
        checks_not_run=checks_not_run,
    )
    return AuthEvidenceResult(
        authentication=authentication,
        completeness=completeness,
        findings=findings,
    )


# =============================================================================
# public: offline sender provenance inspection
# =============================================================================


def inspect_sender_provenance(addresses: SenderAddresses) -> list[SecurityFinding]:
    """Emit offline sender-provenance findings (R-004.3, T-03).

    Checks, with no network/DNS:

    - From vs Reply-To domain mismatch (``REPLY_TO_MISMATCH``);
    - From vs Return-Path domain mismatch (``RETURN_PATH_MISMATCH``);
    - a Unicode/IDNA confusable From domain (``CONFUSABLE_DOMAIN``).

    These are observations, never trust decisions. Full registrable-domain
    canonicalization overlaps Task 1.5's URL inspections and is deferred there;
    the confusable heuristic here is intentionally minimal and offline.
    """
    if not isinstance(addresses, SenderAddresses):
        raise TypeError("addresses must be a SenderAddresses")

    findings: list[SecurityFinding] = []
    from_domain = _domain_of(addresses.from_address)

    if from_domain and addresses.reply_to is not None:
        reply_domain = _domain_of(addresses.reply_to)
        if reply_domain and reply_domain != from_domain:
            findings.append(
                _finding(
                    finding_key="sender-reply-to-mismatch",
                    rule_id=_RULE_SENDER_MISMATCH,
                    category=FindingCategory.SENDER_IMPERSONATION,
                    severity=FindingSeverity.MEDIUM,
                    status=FindingStatus.OBSERVED,
                    explanation_code="REPLY_TO_MISMATCH",
                )
            )

    if from_domain and addresses.return_path is not None:
        return_domain = _domain_of(addresses.return_path)
        if return_domain and return_domain != from_domain:
            findings.append(
                _finding(
                    finding_key="sender-return-path-mismatch",
                    rule_id=_RULE_SENDER_MISMATCH,
                    category=FindingCategory.SENDER_IMPERSONATION,
                    severity=FindingSeverity.LOW,
                    status=FindingStatus.OBSERVED,
                    explanation_code="RETURN_PATH_MISMATCH",
                )
            )

    if from_domain and _is_confusable_domain(from_domain):
        findings.append(
            _finding(
                finding_key="sender-confusable-domain",
                rule_id=_RULE_CONFUSABLE,
                category=FindingCategory.SENDER_IMPERSONATION,
                severity=FindingSeverity.MEDIUM,
                status=FindingStatus.SUSPECTED,
                explanation_code="CONFUSABLE_DOMAIN",
            )
        )

    return findings


# =============================================================================
# public: combined builder
# =============================================================================


def build_auth_evidence(
    headers: object,
    *,
    addresses: SenderAddresses | None = None,
    boundary: TrustedReceiverBoundary,
) -> AuthEvidenceResult:
    """Classify auth axes and merge offline sender-provenance findings.

    Convenience composition of :func:`classify_authentication` and
    :func:`inspect_sender_provenance`. The sender findings are independent of
    the auth result: a provider-reported DKIM=pass plus a Reply-To mismatch
    yields *both* the pass observation and the mismatch finding, and the result
    still exposes no allow/trust flag (I-06, R-004.4, T-02).
    """
    result = classify_authentication(headers, boundary=boundary)
    merged = list(result.findings)
    if addresses is not None:
        merged.extend(inspect_sender_provenance(addresses))
    return AuthEvidenceResult(
        authentication=result.authentication,
        completeness=result.completeness,
        findings=merged,
    )


# =============================================================================
# small pure helpers
# =============================================================================


def _finding(
    *,
    finding_key: str,
    rule_id: str,
    category: FindingCategory,
    severity: FindingSeverity,
    status: FindingStatus,
    explanation_code: Code,
) -> SecurityFinding:
    """Build a metadata-only typed finding (no raw body text, I-05)."""
    return SecurityFinding(
        finding_key=finding_key,
        rule_id=rule_id,
        category=category,
        severity=severity,
        status=status,
        detector_version=AUTH_EVIDENCE_VERSION,
        evidence_refs=[],
        explanation_code=explanation_code,
    )


def _domain_of(address: str | None) -> str | None:
    """Return the lowercased domain of an email address, offline and bounded.

    Handles an optional display name and angle-addr (``Name <a@b>``) and bare
    ``a@b``. Returns ``None`` when no ``@domain`` is present. Performs no DNS.
    """
    if not isinstance(address, str):
        return None
    text = address.strip()
    if "<" in text and ">" in text:
        start = text.rfind("<")
        end = text.find(">", start)
        if end > start:
            text = text[start + 1 : end].strip()
    if "@" not in text:
        return None
    domain = text.rsplit("@", 1)[-1].strip().strip("<>").rstrip(".")
    # Strip a trailing route/port or whitespace fragment defensively.
    domain = domain.split()[0] if domain.split() else ""
    return domain.lower() or None


def _is_confusable_domain(domain: str) -> bool:
    """Offline heuristic: does the domain look like a homoglyph/IDNA lookalike?

    Minimal and network-free (T-03). Flags a domain when either:

    - it is already in punycode/IDNA ``xn--`` form (an encoded non-ASCII label
      that could be a lookalike of an ASCII brand), or
    - it contains any non-ASCII character that is not a plain lowercase letter
      — i.e. a Unicode confusable such as Cyrillic ``а`` (U+0430) mixed into an
      otherwise ASCII-looking name.

    Richer registrable-domain / skeleton-similarity scoring overlaps Task 1.5's
    URL inspections and is deferred there; this keeps a conservative offline
    signal so a confusable sender is never silently trusted.
    """
    if not domain:
        return False
    lowered = domain.lower()
    # Punycode / IDNA encoded labels.
    if "xn--" in lowered:
        return True
    # Any non-ASCII code point is a confusable candidate.
    for char in domain:
        if ord(char) > 0x7F:
            return True
        # Mixed-script detection for already-decoded homoglyphs: a letter whose
        # Unicode name says it is not from the Latin block is suspicious.
        if char.isalpha() and not char.isascii():
            return True
    # Defensive: NFKC-fold and compare; if folding changes the string it had
    # compatibility/confusable characters.
    if unicodedata.normalize("NFKC", domain) != domain:
        return True
    return False


__all__ = [
    "AUTH_EVIDENCE_VERSION",
    "NO_AUTH_EVIDENCE_CAPTURED",
    "AuthEvidenceResult",
    "SenderAddresses",
    "TrustedReceiverBoundary",
    "build_auth_evidence",
    "classify_authentication",
    "inspect_sender_provenance",
]
