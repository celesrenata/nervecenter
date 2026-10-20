"""NerveCenter policy decision point (NC-SPEC-2026-010 Phase 2, C4 / ADR-0003).

This module is the single, framework-neutral PDP for all NerveCenter actions:

    principal + action + resource + context  ->  Decision(ALLOW|DENY, reason, policy_id)

Semantics (ADR-0003):

- **Default deny.** A request is allowed only while at least one applicable
  ``ALLOW`` policy matches and no applicable ``DENY`` policy does.
- **Explicit deny precedence.** Any matching ``DENY`` overrides every
  ``ALLOW``, regardless of scope or priority.
- **Namespace isolation.** The PDP denies any request whose resource
  namespace differs from the principal's namespace; no policy can cross
  that platform invariant.
- **No wildcard privilege expansion for agent/function principals.** A
  wildcard principal pattern (``*`` / ``kind:*``) never matches a
  ``kind:agent`` or ``kind:function`` principal; non-human machine
  principals are only reached by exact ``principal_id`` or explicit
  ``kind:<kind>`` clauses.
- **Fail closed.** Malformed requests, malformed contexts, malformed policy
  patterns, and unsupported condition expressions all produce ``DENY``
  (never an exception out of :meth:`PolicyDecisionPoint.evaluate`).
- **Deterministic ordering.** Applicable policies are ordered by
  ``priority`` desc, then ``version`` desc, then ``policy_id`` asc; the
  first applicable policy of each effect class names the decision.
- **No Cedar runtime.** This repository has no Cedar dependency (see
  ADR-0003, checked 2026-10-10). This module implements a *documented
  Cedar-style subset* (below); anything outside the subset yields an
  explicit ``unsupported_expression`` fail-closed result instead of a
  silent guess.
- **No side effects.** The PDP is stateless: it never mutates inputs,
  writes, or emits events. Audit emission is the caller's job.
  ``simulate=True`` only tags the returned :class:`Decision` for audit
  reporting; approvals are verified but never consumed.

Cedar-style condition subset
----------------------------

``NCPolicy.condition`` strings are evaluated by a small tokenizer /
recursive-descent parser supporting:

- literals: single- or double-quoted strings (no escapes), integers,
  floats, ``true`` / ``false`` / ``null``;
- identifiers: ``name`` or dotted ``a.b.c`` paths resolved against the
  request context;
- comparisons: ``==`` ``!=`` ``<`` ``<=`` ``>`` ``>=``;
- logic: ``&&`` ``||`` ``!``;
- membership: ``<operand> in [literal, ...]``;
- parentheses for grouping.

Deliberately unsupported (fail closed, ``unsupported_expression``):
function/method calls, string operators, ``has``, ranges, nested
subscripts, escape sequences, and any token the grammar above does not
cover. Unsupported *ALLOW* conditions make that policy inapplicable;
an unsupported *DENY* condition fails the whole evaluation to ``DENY``.

Policy match patterns
---------------------

- ``principal_match`` — comma-joined AND clauses: ``prn_<ulid>`` (exact
  id), ``kind:<kind>``, ``namespace:<ns>``; ``*`` / ``kind:*`` /
  ``namespace:*`` are wildcards (see the no-expansion rule above).
- ``action_match`` — exact action string, trailing-wildcard prefix
  (``resource.action:devices.*``), or ``*`` for any action.
- ``resource_match`` — an exact canonical ``nc://`` URI, a trailing
  wildcard URI prefix (``nc://personal/homeassistant/home/devices/*``),
  or ``*`` for any resource.

Approval binding
----------------

:func:`verify_approval_binding` is a pure helper verifying an
:class:`NCApproval` against the exact immutable payload (SHA-256 via
``schemas.base.content_hash``), exact target URI, exact proposed action,
expiry, and the ``APPROVED`` state. :meth:`PolicyDecisionPoint.evaluate`
accepts an optional ``approval`` + ``payload``: a verified binding can
elevate a *default-deny* result to ``ALLOW``; it can never override an
explicit policy ``DENY``.
"""

from __future__ import annotations

import operator
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Literal, Mapping, Sequence

from schemas.base import content_hash
from schemas.nc_platform import (
    NCApproval,
    NCPolicy,
    NCPrincipal,
    NCResource,
    NcUriError,
    validate_nc_uri,
)

from .sensitivity import ModelTier, SensitivityClass

#: PDP contract version stamped into decision notes for audit consumers.
PDP_CONTRACT_VERSION = "nervcenter-pdp/v1"

#: Principal kinds a wildcard pattern may never expand to (NC-CAP-007:
#: no wildcard issuance for machine principals).
NO_WILDCARD_KINDS = frozenset({"agent", "function"})

#: Approval state required for a binding to verify (NC-CAP-015).
APPROVED_STATE = "APPROVED"

Effect = Literal["ALLOW", "DENY"]

# ---------------------------------------------------------------------------
# Reason codes (machine-readable; one per Decision)
# ---------------------------------------------------------------------------

REASON_DEFAULT_DENY = "default_deny"
REASON_ALLOW = "allow"
REASON_EXPLICIT_DENY = "explicit_deny"
REASON_NAMESPACE_MISMATCH = "namespace_mismatch"
REASON_MALFORMED_REQUEST = "malformed_request"
REASON_MALFORMED_CONTEXT = "malformed_context"
REASON_MALFORMED_POLICY = "malformed_policy"
REASON_UNSUPPORTED_EXPRESSION = "unsupported_expression"
REASON_APPROVAL_GRANTED = "approval_granted"
REASON_APPROVAL_REJECTED = "approval_rejected"
REASON_APPROVAL_MALFORMED = "approval_malformed"


class _UnsupportedExpression(Exception):
    """Internal: condition text outside the documented Cedar-style subset."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class _CondEvaluationError(Exception):
    """Internal: a well-formed condition could not be evaluated (type
    mismatch, undefined key, unsupported value shape). Fails closed."""


# ---------------------------------------------------------------------------
# Decision / binding value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """An auditable PDP outcome.

    ``policy_id`` names the policy (``pol_*``) or approval (``app_*``)
    that decided the outcome, or ``None`` for a default deny. ``reason``
    is a short, secret-free human summary; ``reason_code`` is the stable
    machine tag. ``notes`` carries non-decisive diagnostics (malformed
    policies skipped, conditions that were false, failed approval
    binding details). Nothing on this object may contain tokens or
    credential material.
    """

    effect: Effect
    reason: str
    reason_code: str
    policy_id: str | None = None
    simulated: bool = False
    matched_policy_ids: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# INFER_ROUTE decision (R107 / §37–§39)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InferRouteDecision:
    """Outcome of an ``INFER_ROUTE`` decision (R107 / §37–§39).

    Carries the routing metadata the inference layer needs: ``model_tier``,
    ``remote_allowed``, ``policy_id``, and ``reason`` — plus the derived
    ``sensitivity_class`` for audit. A ``remote_allowed=False`` outcome is
    unconditionally ``LOCAL_ONLY`` regardless of the policy set: a remote
    tier is only ever named by an explicit matching ``ALLOW`` policy, and a
    confidential / ``LOCAL_ONLY`` input can never be routed remote.
    """

    model_tier: str
    remote_allowed: bool
    policy_id: str | None
    reason: str
    sensitivity_class: str
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApprovalBinding:
    """Result of :func:`verify_approval_binding` (pure, no side effects)."""

    verified: bool
    reason: str
    reason_code: str
    approval_id: str | None = None


# ---------------------------------------------------------------------------
# Condition subset: tokenizer
# ---------------------------------------------------------------------------

_TOKEN_SPEC = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<number>\d+(?:\.\d+)?)
    | (?P<string>'[^']*'|"[^"]*")
    | (?P<op>==|!=|<=|>=|&&|\|\||<|>|!|in|\(|\)|\[|\]|,)
    | (?P<ident>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)
    """,
    re.VERBOSE,
)


def _tokenize_condition(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN_SPEC.match(text, pos)
        if m is None:
            if text[pos:].strip() == "":
                break
            raise _UnsupportedExpression(f"unrecognized token at {text[pos:pos + 12]!r}")
        pos = m.end()
        kind = m.lastgroup
        assert kind is not None
        if kind == "ws":
            continue
        tokens.append((kind, m.group()))
    return tokens


# ---------------------------------------------------------------------------
# Condition subset: recursive-descent parser/evaluator
# ---------------------------------------------------------------------------

# Allowed scalar context value types.
_CONTEXT_SCALAR = (str, int, float, bool, type(None))


class _ConditionEvaluator:
    """Evaluates one condition expression against a request context.

    Raises :class:`_UnsupportedExpression` for out-of-subset syntax and
    :class:`_CondEvaluationError` for evaluable syntax that cannot be
    resolved (undefined key, type mismatch). Both fail closed by the
    caller.
    """

    def __init__(self, tokens: list[tuple[str, str]], context: Mapping[str, Any]) -> None:
        self._tokens = tokens
        self._i = 0
        self._context = context

    # -- token helpers -----------------------------------------------------

    def _peek(self) -> tuple[str, str] | None:
        if self._i < len(self._tokens):
            return self._tokens[self._i]
        return None

    def _next(self) -> tuple[str, str]:
        tok = self._peek()
        if tok is None:
            raise _UnsupportedExpression("unexpected end of condition")
        self._i += 1
        return tok

    def _expect_op(self, value: str) -> None:
        kind, tok = self._next()
        if kind != "op" or tok != value:
            raise _UnsupportedExpression(f"expected {value!r}, got {tok!r}")

    # -- grammar -----------------------------------------------------------

    def parse(self) -> Any:
        if not self._tokens:
            raise _UnsupportedExpression("empty condition")
        value = self._or_expr()
        if self._peek() is not None:
            raise _UnsupportedExpression(
                f"trailing tokens after expression: {self._tokens[self._i][1]!r}"
            )
        return value

    def _or_expr(self) -> Any:
        value = self._and_expr()
        while True:
            peek = self._peek()
            if peek is not None and peek == ("op", "||"):
                self._next()
                rhs = self._and_expr()
                value = self._as_bool(value, "left of ||") or self._as_bool(rhs, "right of ||")
            else:
                return value

    def _and_expr(self) -> Any:
        value = self._unary()
        while True:
            peek = self._peek()
            if peek is not None and peek == ("op", "&&"):
                self._next()
                rhs = self._unary()
                value = self._as_bool(value, "left of &&") and self._as_bool(rhs, "right of &&")
            else:
                return value

    def _unary(self) -> Any:
        peek = self._peek()
        if peek is not None and peek == ("op", "!"):
            self._next()
            return not self._as_bool(self._unary(), "operand of !")
        value = self._comparison()
        peek = self._peek()
        if peek is not None and peek == ("op", "in"):
            self._next()
            membership = self._list_literal()
            return value in membership
        return value

    def _comparison(self) -> Any:
        left = self._operand()
        peek = self._peek()
        if peek is None or peek[0] != "op" or peek[1] not in {"==", "!=", "<", "<=", ">", ">="}:
            return left
        kind, op = self._next()
        right = self._operand()
        return _compare(op, left, right)

    def _operand(self) -> Any:
        kind, tok = self._next()
        if kind == "number":
            return float(tok) if "." in tok else int(tok)
        if kind == "string":
            return tok[1:-1]
        if kind == "ident":
            if tok == "true":
                return True
            if tok == "false":
                return False
            if tok == "null":
                return None
            return self._resolve(tok)
        raise _UnsupportedExpression(f"unexpected token {tok!r} in operand position")

    def _list_literal(self) -> list[Any]:
        self._expect_op("[")
        items: list[Any] = []
        peek = self._peek()
        if peek is not None and peek == ("op", "]"):
            self._next()
            return items
        while True:
            items.append(self._operand())
            peek = self._peek()
            if peek is None:
                raise _UnsupportedExpression("unterminated list literal")
            if peek == ("op", ","):
                self._next()
                continue
            if peek == ("op", "]"):
                self._next()
                return items
            raise _UnsupportedExpression(f"expected ',' or ']' in list literal, got {peek[1]!r}")

    def _resolve(self, path: str) -> Any:
        parts = path.split(".")
        current: Any = self._context
        for part in parts:
            if isinstance(current, Mapping):
                if part not in current:
                    raise _CondEvaluationError(f"context key {path!r} is not present")
                current = current[part]
            else:
                raise _CondEvaluationError(f"context path {path!r} does not resolve to a mapping")
        if isinstance(current, (dict, list)) or not isinstance(current, _CONTEXT_SCALAR):
            raise _CondEvaluationError(f"context value for {path!r} has unsupported type")
        return current

    @staticmethod
    def _as_bool(value: Any, where: str) -> bool:
        if isinstance(value, bool):
            return value
        raise _CondEvaluationError(f"non-boolean value in {where}")


def _compare(op: str, left: Any, right: Any) -> Any:
    if op in ("==", "!="):
        result = left == right
        return result if op == "==" else not result
    order_ops = {
        "<": operator.lt,
        "<=": operator.le,
        ">": operator.gt,
        ">=": operator.ge,
    }
    if not (isinstance(left, (int, float)) and isinstance(right, (int, float))):
        if isinstance(left, str) and isinstance(right, str) and op in ("<", "<=", ">", ">="):
            pass  # lexicographic string comparison is defined
        else:
            raise _CondEvaluationError(f"ordering operator {op!r} requires numeric (or string) operands")
    return order_ops[op](left, right)


def _evaluate_condition(condition: str, context: Mapping[str, Any]) -> Any:
    """Evaluate a documented-subset condition.

    Returns the boolean result. Raises ``_UnsupportedExpression`` (out of
    subset) or ``_CondEvaluationError`` (defined but unresolvable).
    """
    if condition is None:
        return True
    text = condition.strip()
    if not text:
        raise _UnsupportedExpression("empty condition string")
    return bool(_ConditionEvaluator(_tokenize_condition(text), context).parse())


# ---------------------------------------------------------------------------
# Pattern matching
# ---------------------------------------------------------------------------

_NC_SEGMENT = re.compile(r"^[A-Za-z0-9\-._~]+$")
_PRINCIPAL_ID = re.compile(r"^prn_[0-9A-HJKMNP-TV-Z]{26}$")


def _principal_pattern_result(pattern: str, principal: NCPrincipal) -> bool | None:
    """Match a principal match pattern.

    Returns ``True`` (match), ``False`` (no match), or ``None``
    (malformed pattern — the calling PDP records it and continues;
    default deny still governs).
    """
    if not isinstance(pattern, str) or not pattern.strip():
        return None
    for clause in pattern.split(","):
        clause = clause.strip()
        if not clause:
            return None
        if clause == "*":
            if principal.kind in NO_WILDCARD_KINDS:
                return False  # no wildcard privilege expansion
            continue
        if clause.startswith("kind:"):
            value = clause[len("kind:"):]
            if not value:
                return None
            if value == "*":
                if principal.kind in NO_WILDCARD_KINDS:
                    return False
                continue
            if value != principal.kind:
                return False
            continue
        if clause.startswith("namespace:"):
            value = clause[len("namespace:"):]
            if not value:
                return None
            if value == "*":
                if principal.kind in NO_WILDCARD_KINDS:
                    return False
                continue
            if value != principal.namespace:
                return False
            continue
        if _PRINCIPAL_ID.match(clause):
            if clause != principal.principal_id:
                return False
            continue
        return None  # unrecognized clause form
    return True


def _action_match_result(pattern: str, action: str) -> bool | None:
    if not isinstance(pattern, str) or not pattern.strip():
        return None
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        prefix = pattern[:-1]
        return bool(prefix) and action.startswith(prefix)
    return action == pattern


def _resource_pattern_result(pattern: str, uri: str) -> bool | None:
    """Match a resource match pattern against a validated nc:// URI.

    Supported: exact URI, trailing-wildcard URI prefix, or bare ``*``.
    Returns ``None`` for a malformed pattern.
    """
    if not isinstance(pattern, str) or not pattern.strip():
        return None
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        prefix = pattern[:-1]
        if not prefix.startswith("nc://"):
            return None
        body = prefix[len("nc://"):]
        if not body.endswith("/"):
            return None  # wildcard must occupy a whole trailing segment
        if any(not _NC_SEGMENT.match(s) for s in body[:-1].split("/")):
            return None  # every fixed segment must be a valid nc segment
        return uri.startswith(prefix)
    # exact match; the requested URI is already validated by the PDP, and
    # the pattern must itself parse under the frozen grammar
    try:
        validate_nc_uri(pattern)
    except NcUriError:
        return None
    return uri == pattern


# ---------------------------------------------------------------------------
# Approval binding
# ---------------------------------------------------------------------------


def verify_approval_binding(
    approval: NCApproval,
    *,
    action: Mapping[str, Any],
    target: str,
    payload: Any,
    now: datetime | None = None,
) -> ApprovalBinding:
    """Verify that ``approval`` binds to the exact proposed operation.

    Checks, in order (first failure is reported):

    1. payload type / immutable-hash equality — ``payload_hash`` must be
       the SHA-256 (``schemas.base.content_hash``) of the immutable
       request payload presented now;
    2. exact target — the approval's canonical ``target`` URI equals the
       requested resource URI byte-for-byte;
    3. exact action — the approval's ``proposed_action`` dict equals the
       requested action mapping (deep equality; no re-interpretation);
    4. expiry — ``now`` (default: current UTC clock) must be strictly
       before ``expires_at``;
    5. state — the approval must be ``APPROVED``.

    Pure: no mutation, no I/O, no logging of payload or approval content.
    """
    approval_id = getattr(approval, "approval_id", None)
    stamp = f"approval {approval_id}: " if approval_id else "approval: "

    if not isinstance(approval, NCApproval):
        return ApprovalBinding(False, "approval record is not an NCApproval", REASON_APPROVAL_MALFORMED, None)
    if payload is None:
        return ApprovalBinding(
            False, stamp + "no payload presented; hash cannot be verified",
            REASON_APPROVAL_MALFORMED, approval.approval_id,
        )
    try:
        presented_hash = content_hash(payload)
    except (TypeError, ValueError, RecursionError):
        return ApprovalBinding(
            False, stamp + "payload is not JSON-serializable; hash cannot be computed",
            REASON_APPROVAL_MALFORMED, approval.approval_id,
        )
    if presented_hash != approval.payload_hash:
        return ApprovalBinding(
            False, stamp + "payload hash mismatch (payload was altered or is not the approved payload)",
            REASON_APPROVAL_REJECTED, approval.approval_id,
        )
    if target != approval.target:
        return ApprovalBinding(
            False, stamp + "target mismatch (approval is bound to a different resource URI)",
            REASON_APPROVAL_REJECTED, approval.approval_id,
        )
    if action != approval.proposed_action:
        return ApprovalBinding(
            False, stamp + "action mismatch (proposed action differs from the approval's exact action)",
            REASON_APPROVAL_REJECTED, approval.approval_id,
        )
    moment = now if now is not None else datetime.now(UTC)
    expires = approval.expires_at
    if moment.tzinfo is None or expires.tzinfo is None:
        return ApprovalBinding(
            False, stamp + "expiry comparison requires timezone-aware datetimes",
            REASON_APPROVAL_MALFORMED, approval.approval_id,
        )
    if moment >= expires:
        return ApprovalBinding(
            False, stamp + "expired (now is at or past expires_at)",
            REASON_APPROVAL_REJECTED, approval.approval_id,
        )
    if approval.state != APPROVED_STATE:
        return ApprovalBinding(
            False, f"approval state is {approval.state!r}, expected {APPROVED_STATE!r}",
            REASON_APPROVAL_REJECTED, approval.approval_id,
        )
    return ApprovalBinding(
        True,
        stamp + "verified (payload hash + exact target + exact action + within expiry + APPROVED)",
        REASON_APPROVAL_GRANTED, approval.approval_id,
    )


# ---------------------------------------------------------------------------
# The PDP
# ---------------------------------------------------------------------------


class PolicyDecisionPoint:
    """The single policy decision point for NerveCenter (ADR-0003).

    Framework-neutral and adapter-free: policies arrive as a sequence of
    frozen :class:`NCPolicy` records or as a callable returning such a
    sequence (invoked per evaluation, so an API layer can later swap in a
    versioned cache or persistence adapter without PDP changes). The PDP
    never mutates the records it receives.

    Usage::

        pdp = PolicyDecisionPoint(policies)
        decision = pdp.evaluate(
            principal=nc_principal,
            action="resource.write:devices.turn_on",
            resource="nc://personal/homeassistant/home/devices/bedroom-lamp",
            context={"classification": "HOME_PRIVATE"},
        )
    """

    def __init__(
        self,
        policies: Sequence[NCPolicy] | Callable[[], Sequence[NCPolicy]] | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if policies is not None and not callable(policies):
            policies = tuple(policies)
        self._policies: Sequence[NCPolicy] | Callable[[], Sequence[NCPolicy]] | None = policies
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))

    # -- policy source -----------------------------------------------------

    @property
    def policies(self) -> tuple[NCPolicy, ...]:
        """The policy set currently in force (snapshot, never mutated)."""
        if self._policies is None:
            return ()
        if callable(self._policies):
            return tuple(self._policies())
        return tuple(self._policies)

    # -- evaluation ---------------------------------------------------------

    def evaluate(
        self,
        principal: NCPrincipal,
        action: str,
        resource: NCResource | str,
        context: Mapping[str, Any] | None = None,
        *,
        approval: NCApproval | None = None,
        payload: Any = None,
        now: datetime | None = None,
        simulate: bool = False,
    ) -> Decision:
        """Decide one request.

        Never raises on bad inputs: malformed data yields a fail-closed
        ``DENY`` with a specific ``reason_code``. ``simulate=True`` tags
        the result for audit reporting; the PDP has no side effects in
        either mode (nothing is written, emitted, or consumed).
        """
        # 1. request shape (fail closed)
        if not isinstance(principal, NCPrincipal):
            return self._deny(
                REASON_MALFORMED_REQUEST, "principal is not an NCPrincipal record",
                simulated=simulate,
            )
        if not isinstance(action, str) or not action.strip():
            return self._deny(
                REASON_MALFORMED_REQUEST, "action must be a non-empty string",
                simulated=simulate,
            )
        uri, namespace = self._extract_resource(resource)
        if uri is None:
            return self._deny(
                REASON_MALFORMED_REQUEST, "resource must be an NCResource or a valid nc:// URI",
                simulated=simulate,
            )
        if context is not None and not isinstance(context, Mapping):
            return self._deny(
                REASON_MALFORMED_CONTEXT, "context must be a mapping of scalar values",
                simulated=simulate,
            )
        ctx: Mapping[str, Any] = context or {}

        # 2. namespace isolation (platform invariant, pre-policy)
        if principal.namespace != namespace:
            return self._deny(
                REASON_NAMESPACE_MISMATCH,
                f"namespace mismatch: principal scoped to {principal.namespace!r}, resource in {namespace!r}",
                simulated=simulate,
            )

        # 3. deterministic policy evaluation
        notes: list[str] = []
        applicable: list[NCPolicy] = []
        fail_closed: str | None = None
        for policy in self._ordered_policies():
            pnotes, ok, blocked = self._policy_applies(
                policy, principal, action, uri, ctx, notes
            )
            notes.extend(pnotes)
            if blocked is not None:
                fail_closed = blocked
                break
            if ok:
                applicable.append(policy)
        if fail_closed is not None:
            return self._deny(
                fail_closed,
                f"policy evaluation failed closed ({fail_closed})",
                simulated=simulate,
                matched_policy_ids=tuple(p.policy_id for p in applicable),
                notes=tuple(notes),
            )

        denies = [p for p in applicable if p.effect == "DENY"]
        allows = [p for p in applicable if p.effect == "ALLOW"]
        matched_ids = tuple(p.policy_id for p in applicable)

        if denies:
            top = denies[0]
            decision = Decision(
                effect="DENY",
                reason=f"explicit deny by policy {top.policy_id} (effect DENY, priority {top.priority}, version {top.version})",
                reason_code=REASON_EXPLICIT_DENY,
                policy_id=top.policy_id,
                simulated=simulate,
                matched_policy_ids=matched_ids,
                notes=tuple(notes),
            )
        elif allows:
            top = allows[0]
            decision = Decision(
                effect="ALLOW",
                reason=f"allowed by policy {top.policy_id} (priority {top.priority}, version {top.version})",
                reason_code=REASON_ALLOW,
                policy_id=top.policy_id,
                simulated=simulate,
                matched_policy_ids=matched_ids,
                notes=tuple(notes),
            )
        else:
            decision = Decision(
                effect="DENY",
                reason="no applicable ALLOW policy (default deny)",
                reason_code=REASON_DEFAULT_DENY,
                policy_id=None,
                simulated=simulate,
                matched_policy_ids=matched_ids,
                notes=tuple(notes),
            )

        # 4. approval binding (pure verification; can only elevate default
        #    deny, never override an explicit policy DENY)
        if approval is not None and decision.effect == "DENY" and decision.reason_code == REASON_DEFAULT_DENY:
            binding = verify_approval_binding(
                approval,
                action=_as_action_mapping(action),
                target=uri,
                payload=payload,
                now=now if now is not None else self._clock(),
            )
            extra = (binding.reason,)
            if binding.verified:
                return Decision(
                    effect="ALLOW",
                    reason=(
                        f"default deny overridden by verified approval binding "
                        f"{binding.approval_id} (exact payload hash, target, action; within expiry)"
                    ),
                    reason_code=REASON_APPROVAL_GRANTED,
                    policy_id=binding.approval_id,
                    simulated=simulate,
                    matched_policy_ids=matched_ids,
                    notes=tuple(notes) + extra,
                )
            return Decision(
                effect="DENY",
                reason=f"default deny stands; approval binding failed: {binding.reason}",
                reason_code=binding.reason_code,
                policy_id=None,
                simulated=simulate,
                matched_policy_ids=matched_ids,
                notes=tuple(notes) + extra,
            )
        return decision

    # -- INFER_ROUTE decision (R107 / §37–§39) --------------------------------

    def infer_route(
        self,
        principal,
        context: Mapping[str, Any] | None = None,
        *,
        action: str = "resource.infer:route",
        resource: NCResource | str | None = None,
        simulate: bool = False,
    ) -> InferRouteDecision:
        """Decide how an inference request may be routed (R107 / §37–§39).

        Reuses the same PDP engine as :meth:`evaluate` (no second policy
        engine): a remote-allowed result requires an explicit matching
        ``ALLOW`` policy, an explicit ``DENY`` wins, and default deny holds
        when nothing matches. The inference request carries data-policy
        metadata in ``context``:

        - ``sensitivity_class`` / ``privacy`` — the canonical class
          (``public`` / ``internal`` / ``confidential``);
        - ``contains_personal_communications`` — bool; if true the input is
          treated as ``confidential`` (§37: personal content never routes
          remote);
        - ``remote_provider_allowed`` — bool; a transport-level gate that
          must also be true for a remote tier to be named.

        Invariants (fail closed):

        - ``confidential`` / ``LOCAL_ONLY`` input and any input with
          ``contains_personal_communications`` are never routed remote —
          ``remote_allowed`` is ``False`` and the tier is local.
        - A missing or unresolvable sensitivity class fails closed to
          ``confidential`` (local).
        - A remote tier is named only when the class is remote-capable
          (``public`` / ``internal``), no explicit ``DENY`` matches, an
          ``ALLOW`` policy matches, and ``remote_provider_allowed`` is true.
        """
        # 1. principal shape (fail closed -> local-only)
        if not isinstance(principal, NCPrincipal):
            return InferRouteDecision(
                model_tier=ModelTier.SMALL_LOCAL.value,
                remote_allowed=False,
                policy_id=None,
                reason="principal is not an NCPrincipal; fail closed to local-only",
                sensitivity_class=SensitivityClass.CONFIDENTIAL.value,
                notes=("malformed_principal",),
            )
        if context is not None and not isinstance(context, Mapping):
            return InferRouteDecision(
                model_tier=ModelTier.SMALL_LOCAL.value,
                remote_allowed=False,
                policy_id=None,
                reason="context must be a mapping; fail closed to local-only",
                sensitivity_class=SensitivityClass.CONFIDENTIAL.value,
                notes=("malformed_context",),
            )
        ctx: Mapping[str, Any] = context or {}

        # 2. resolve the canonical sensitivity class (fail closed ->
        #    confidential). Personal-communication inputs are confidential.
        if bool(ctx.get("contains_personal_communications", False)):
            class_ = SensitivityClass.CONFIDENTIAL
            class_source = "contains_personal_communications"
        else:
            raw = ctx.get("sensitivity_class", ctx.get("privacy"))
            if isinstance(raw, SensitivityClass):
                class_ = raw
                class_source = "sensitivity_class"
            elif isinstance(raw, str):
                try:
                    class_ = SensitivityClass(raw)
                    class_source = "sensitivity_class"
                except ValueError:
                    class_ = SensitivityClass.CONFIDENTIAL
                    class_source = "unresolvable_sensitivity_class"
            else:
                class_ = SensitivityClass.CONFIDENTIAL
                class_source = "missing_sensitivity_class"

        notes = (f"sensitivity_class={class_.value} ({class_source})",)

        # 3. hard floor: confidential / LOCAL_ONLY is never routed remote.
        if class_ is SensitivityClass.CONFIDENTIAL:
            return InferRouteDecision(
                model_tier=ModelTier.SMALL_LOCAL.value,
                remote_allowed=False,
                policy_id=None,
                reason="confidential / LOCAL_ONLY content; remote tier is disallowed",
                sensitivity_class=class_.value,
                notes=notes,
            )

        # 4. remote-capable (public / internal): run the policy engine.
        if resource is None:
            resource = f"nc://{principal.namespace}/inference/routing/model/route"
        uri, namespace = self._extract_resource(resource)
        if uri is None:
            return InferRouteDecision(
                model_tier=ModelTier.MEDIUM_LOCAL.value,
                remote_allowed=False,
                policy_id=None,
                reason="route resource is not a valid nc:// URI; fail closed to local-only",
                sensitivity_class=class_.value,
                notes=notes + ("malformed_resource",),
            )
        if principal.namespace != namespace:
            return InferRouteDecision(
                model_tier=ModelTier.MEDIUM_LOCAL.value,
                remote_allowed=False,
                policy_id=None,
                reason=(
                    f"namespace mismatch: principal scoped to "
                    f"{principal.namespace!r}, route resource in {namespace!r}"
                ),
                sensitivity_class=class_.value,
                notes=notes + ("namespace_mismatch",),
            )

        eval_notes: list[str] = []
        applicable: list[NCPolicy] = []
        fail_closed: str | None = None
        for policy in self._ordered_policies():
            pnotes, ok, blocked = self._policy_applies(
                policy, principal, action, uri, ctx, eval_notes
            )
            eval_notes.extend(pnotes)
            if blocked is not None:
                fail_closed = blocked
                break
            if ok:
                applicable.append(policy)
        if fail_closed is not None:
            return InferRouteDecision(
                model_tier=ModelTier.MEDIUM_LOCAL.value,
                remote_allowed=False,
                policy_id=None,
                reason=f"policy evaluation failed closed ({fail_closed})",
                sensitivity_class=class_.value,
                notes=notes + tuple(eval_notes),
            )

        denies = [p for p in applicable if p.effect == "DENY"]
        allows = [p for p in applicable if p.effect == "ALLOW"]
        if denies:
            top = denies[0]
            return InferRouteDecision(
                model_tier=ModelTier.MEDIUM_LOCAL.value,
                remote_allowed=False,
                policy_id=top.policy_id,
                reason=f"explicit deny by policy {top.policy_id}",
                sensitivity_class=class_.value,
                notes=notes + tuple(eval_notes),
            )

        if not allows:
            return InferRouteDecision(
                model_tier=ModelTier.MEDIUM_LOCAL.value,
                remote_allowed=False,
                policy_id=None,
                reason="no applicable ALLOW policy (default deny)",
                sensitivity_class=class_.value,
                notes=notes + tuple(eval_notes),
            )

        top = allows[0]
        if not bool(ctx.get("remote_provider_allowed", False)):
            return InferRouteDecision(
                model_tier=ModelTier.MEDIUM_LOCAL.value,
                remote_allowed=False,
                policy_id=top.policy_id,
                reason="remote provider not permitted by transport metadata; stays local",
                sensitivity_class=class_.value,
                notes=notes + tuple(eval_notes) + ("remote_provider_allowed=false",),
            )

        return InferRouteDecision(
            model_tier=ModelTier.LARGE_REASONING.value,
            remote_allowed=True,
            policy_id=top.policy_id,
            reason=(
                f"remote allowed by policy {top.policy_id} "
                f"(priority {top.priority}, version {top.version})"
            ),
            sensitivity_class=class_.value,
            notes=notes + tuple(eval_notes),
        )

    # -- internals -----------------------------------------------------------

    def _ordered_policies(self) -> list[NCPolicy]:
        """Policies in deterministic order: priority desc, version desc, id asc."""
        return sorted(
            self.policies,
            key=lambda p: (-p.priority, -p.version, p.policy_id),
        )

    @staticmethod
    def _extract_resource(resource: Any) -> tuple[str | None, str | None]:
        """Return (canonical uri, namespace) or (None, None) if malformed."""
        if isinstance(resource, NCResource):
            return resource.uri, resource.namespace
        if isinstance(resource, str):
            try:
                parts = validate_nc_uri(resource)
            except NcUriError:
                return None, None
            return resource, parts.namespace
        return None, None

    @staticmethod
    def _policy_applies(
        policy: NCPolicy,
        principal: NCPrincipal,
        action: str,
        uri: str,
        context: Mapping[str, Any],
        shared_notes: list[str],
    ) -> tuple[list[str], bool, str | None]:
        """Check pattern + condition applicability for one policy.

        Returns ``(notes, applies, fail_closed)``. ``applies`` is True only
        when every pattern matches and the condition evaluates true.
        Malformed patterns are recorded and the policy is skipped (default
        deny still governs). ``fail_closed`` carries a reason code when a
        pattern-matching DENY policy has a condition we cannot evaluate
        (out of the documented subset, or unresolvable): an unreadable deny
        is treated as in force, so the whole request is denied with that
        code (fail closed — a deny we cannot prove false must stand).
        """
        notes: list[str] = []
        principal_ok = _principal_pattern_result(policy.principal_match, principal)
        if principal_ok is None:
            shared_notes.append(
                f"policy {policy.policy_id}: malformed principal_match {policy.principal_match!r}; skipped"
            )
            return notes, False, None
        if not principal_ok:
            return notes, False, None
        action_ok = _action_match_result(policy.action_match, action)
        if action_ok is None:
            shared_notes.append(
                f"policy {policy.policy_id}: malformed action_match {policy.action_match!r}; skipped"
            )
            return notes, False, None
        if not action_ok:
            return notes, False, None
        resource_ok = _resource_pattern_result(policy.resource_match, uri)
        if resource_ok is None:
            shared_notes.append(
                f"policy {policy.policy_id}: malformed resource_match {policy.resource_match!r}; skipped"
            )
            return notes, False, None
        if not resource_ok:
            return notes, False, None
        # condition (patterns already matched)
        try:
            condition_true = _evaluate_condition(policy.condition, context)
        except _UnsupportedExpression as exc:
            if policy.effect == "DENY":
                shared_notes.append(
                    f"policy {policy.policy_id}: unsupported condition ({exc.detail}); fail closed"
                )
                return notes, True, REASON_UNSUPPORTED_EXPRESSION
            notes.append(
                f"policy {policy.policy_id}: unsupported condition ({exc.detail}); allow policy not applied"
            )
            return notes, False, None
        except _CondEvaluationError as exc:
            if policy.effect == "DENY":
                shared_notes.append(
                    f"policy {policy.policy_id}: condition unresolvable ({exc}); fail closed"
                )
                return notes, True, REASON_MALFORMED_CONTEXT
            notes.append(
                f"policy {policy.policy_id}: condition unresolvable ({exc}); allow policy not applied"
            )
            return notes, False, None
        if not condition_true:
            notes.append(f"policy {policy.policy_id}: condition evaluated false; not applied")
            return notes, False, None
        return notes, True, None

    def _deny(
        self,
        reason_code: str,
        reason: str,
        *,
        simulated: bool,
        matched_policy_ids: tuple[str, ...] = (),
        notes: tuple[str, ...] = (),
    ) -> Decision:
        return Decision(
            effect="DENY",
            reason=reason,
            reason_code=reason_code,
            policy_id=None,
            simulated=simulated,
            matched_policy_ids=matched_policy_ids,
            notes=notes,
        )


def _as_action_mapping(action: str) -> Mapping[str, Any]:
    """Represent the requested action for approval-binding comparison.

    The approval contract binds to ``proposed_action`` as a mapping, so a
    bare action string is compared as ``{"action": <action>}``.
    """
    return {"action": action}


#: Distinct, not-yet-wired future capability for the personal plane (R107 /
#: §40–§41: response assistant / no impersonation). Exposing the name in the
#: PDP keeps the capability *addressable by policy* without adding a sending
#: endpoint or MCP tool. The capability is declared but no route is bound to
#: it yet; a future ``SEND`` action can gate on it via a policy.
SENDING_CAPABILITY = "personal.sending"

#: Action used by :meth:`PolicyDecisionPoint.infer_route` so routing policies
#: can match on it explicitly.
INFER_ROUTE_ACTION = "resource.infer:route"


__all__ = [
    "APPROVED_STATE",
    "ApprovalBinding",
    "Decision",
    "INFER_ROUTE_ACTION",
    "InferRouteDecision",
    "NO_WILDCARD_KINDS",
    "PDP_CONTRACT_VERSION",
    "PolicyDecisionPoint",
    "SENDING_CAPABILITY",
    "verify_approval_binding",
]
