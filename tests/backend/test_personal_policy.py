"""R107 — Personal policy foundation (AC-R12 / AC-R13 / AC-R14).

Covers the three R107 capabilities in ``backend/policy`` +
``backend/context/compiler.py``:

- **AC-R12 (personal/engineering firewall, §35):** engineering
  compilation excludes personal communications by default; personal
  compilation excludes engineering by default; an explicit scoped grant
  admits *only the granted* source.
- **AC-R13 (sensitivity labels + deterministic classification, §36):**
  deterministic keyword classification (financial → FINANCIAL, etc.),
  the §36 label set, ``LOCAL_ONLY`` routing for confidential content,
  and explicit override.
- **AC-R14 (INFER_ROUTE decision, §37–§39):** PDP-style routing decision
  with ``model_tier`` / ``remote_allowed`` / ``policy_id`` / ``reason``.
  Confidential/``LOCAL_ONLY`` never routes remote; default-deny when no
  policy matches; explicit-DENY precedence; remote requires an explicit
  matching ``ALLOW`` policy.

The PDP matrix exercises at least six cases plus the firewall and the
sending-capability declaration.
"""

from __future__ import annotations

import pytest

from backend.communications.models import SensitivityLabel
from backend.context import ContextCompiler, ContextSource
from backend.policy.pdp import (
    INFER_ROUTE_ACTION,
    SENDING_CAPABILITY,
    InferRouteDecision,
    PolicyDecisionPoint,
)
from backend.policy.sensitivity import (
    ModelTier,
    RoutingDecision,
    SensitivityClass,
    classify_sensitivity,
)
from schemas.nc_platform import NCPolicy, NCPrincipal

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_ULID_BODY = "0123456789ABCDEFGHJKMNPTWX"
assert len(_ULID_BODY) == 26


def _id(prefix: str) -> str:
    return f"{prefix}_{_ULID_BODY}"


def _principal(kind: str = "human", namespace: str = "personal") -> NCPrincipal:
    return NCPrincipal(
        id=f"row-{kind}",
        principal_id=_id("prn"),
        kind=kind,
        display_name=f"{kind}-principal",
        namespace=namespace,
    )


def _route_policy(
    effect: str,
    principal_match: str = "*",
    action_match: str = INFER_ROUTE_ACTION,
    resource_match: str = "*",
    condition: str | None = None,
    priority: int = 0,
    version: int = 1,
    policy_id: str | None = None,
) -> NCPolicy:
    return NCPolicy(
        id=f"row-{policy_id or 'pol'}",
        policy_id=policy_id or _id("pol"),
        effect=effect,
        principal_match=principal_match,
        action_match=action_match,
        resource_match=resource_match,
        condition=condition,
        priority=priority,
        version=version,
    )


def _route(
    policies: list[NCPolicy],
    principal: NCPrincipal,
    context: dict,
):
    return PolicyDecisionPoint(policies).infer_route(principal, context)


# ---------------------------------------------------------------------------
# AC-R12 — personal/engineering partition firewall
# ---------------------------------------------------------------------------


class TestFirewall:
    def test_engineering_excludes_personal_by_default(self) -> None:
        """AC-R12: a seeded personal message never appears in an
        engineering compile result (default)."""
        compiler = ContextCompiler()
        eng = ContextSource(id="eng-src", kind="entities", partition="engineering")
        personal = ContextSource(id="per-msg", kind="notes", partition="personal")
        pkg = compiler.compile(purpose="inference", goal_id="g1", sources=[eng, personal])
        included_src = {s.metadata.get("source_id") for s in pkg.sections}
        assert "per-msg" not in included_src, "personal source leaked into engineering package"
        assert "eng-src" in included_src
        # firewall audit trail
        assert any(e["source_id"] == "per-msg" for e in pkg.metadata["firewall_excluded"])

    def test_explicit_grant_admits_only_that_source(self) -> None:
        """AC-R12: with an explicit link, only the granted communication
        appears (not the rest of the personal plane)."""
        compiler = ContextCompiler()
        eng = ContextSource(id="eng-src", kind="entities", partition="engineering")
        per_a = ContextSource(id="per-a", kind="notes", partition="personal")
        per_b = ContextSource(id="per-b", kind="notes", partition="personal")
        pkg = compiler.compile(
            purpose="inference", goal_id="g1", sources=[eng, per_a, per_b],
            granted_communication_ids=["per-a"],
        )
        notes_src = {s.metadata.get("source_id") for s in pkg.sections if s.kind == "notes"}
        assert notes_src == {"per-a"}, f"expected only per-a, got {notes_src}"
        assert any(e["source_id"] == "per-b" for e in pkg.metadata["firewall_excluded"])

    def test_personal_excludes_engineering_by_default(self) -> None:
        """AC-R12: personal compilation excludes engineering by default."""
        compiler = ContextCompiler()
        eng = ContextSource(id="eng-src", kind="entities", partition="engineering")
        personal = ContextSource(id="per-msg", kind="notes", partition="personal")
        pkg = compiler.compile(
            purpose="inference", goal_id="g1", sources=[eng, personal], personal=True
        )
        included_src = {s.metadata.get("source_id") for s in pkg.sections}
        assert "eng-src" not in included_src, "engineering source leaked into personal package"
        assert "per-msg" in included_src
        assert any(e["source_id"] == "eng-src" for e in pkg.metadata["firewall_excluded"])

    def test_engineering_grant_of_engineering_noop(self) -> None:
        """A grant naming an in-plane (engineering) source is not needed —
        it is included regardless; the grant only matters cross-plane."""
        compiler = ContextCompiler()
        eng = ContextSource(id="eng-src", kind="entities", partition="engineering")
        pkg = compiler.compile(
            purpose="inference", goal_id="g1", sources=[eng], granted_communication_ids=["eng-src"]
        )
        assert "eng-src" in {s.metadata.get("source_id") for s in pkg.sections}
        assert "firewall_excluded" not in pkg.metadata

    def test_default_partition_is_engineering(self) -> None:
        """Back-compat: a source with no partition defaults to engineering,
        so existing callers' behavior is preserved."""
        src = ContextSource(id="legacy", kind="entities")
        assert src.partition == "engineering"
        compiler = ContextCompiler()
        pkg = compiler.compile(purpose="inference", goal_id="g1", sources=[src])
        assert "legacy" in {s.metadata.get("source_id") for s in pkg.sections}


# ---------------------------------------------------------------------------
# AC-R13 — deterministic sensitivity classification + override
# ---------------------------------------------------------------------------


class TestSensitivity:
    def test_financial_keywords_to_financial(self) -> None:
        c = classify_sensitivity("here is my invoice and bank balance")
        assert c.sensitivity_class is SensitivityClass.CONFIDENTIAL
        assert c.label == SensitivityLabel.FINANCIAL.value
        assert c.routing is RoutingDecision.LOCAL_ONLY

    def test_health_keywords_to_personal(self) -> None:
        c = classify_sensitivity("I need a new prescription from my doctor")
        assert c.sensitivity_class is SensitivityClass.CONFIDENTIAL
        assert c.label == SensitivityLabel.PERSONAL.value
        assert c.routing is RoutingDecision.LOCAL_ONLY

    def test_default_is_public_low_sensitivity(self) -> None:
        c = classify_sensitivity("hi, how are you doing?")
        assert c.sensitivity_class is SensitivityClass.PUBLIC
        assert c.routing is RoutingDecision.REMOTE_ALLOWED
        assert not c.overridden

    def test_ingest_label_honored_when_no_keywords(self) -> None:
        c = classify_sensitivity("routine status update", label=SensitivityLabel.WORK)
        assert c.sensitivity_class is SensitivityClass.INTERNAL
        assert c.label == SensitivityLabel.WORK.value

    def test_explicit_override_honored(self) -> None:
        """AC-R13: an API override wins even when keywords say otherwise."""
        c = classify_sensitivity("my credit card invoice", override=SensitivityClass.PUBLIC)
        assert c.sensitivity_class is SensitivityClass.PUBLIC
        assert c.overridden is True
        assert c.routing is RoutingDecision.REMOTE_ALLOWED

    def test_override_to_confidential_is_local_only(self) -> None:
        c = classify_sensitivity("hi there", override=SensitivityClass.CONFIDENTIAL)
        assert c.sensitivity_class is SensitivityClass.CONFIDENTIAL
        assert c.routing is RoutingDecision.LOCAL_ONLY

    def test_unknown_label_raises(self) -> None:
        with pytest.raises(ValueError):
            classify_sensitivity("x", label="NOT_A_LABEL")

    def test_label_set_matches_section_36(self) -> None:
        """AC-R13: the §36 label set is the authoritative vocabulary."""
        vals = {m.value for m in SensitivityLabel}
        assert vals == {
            "PERSONAL", "PRIVATE", "FINANCIAL",
            "WORK", "PROJECT", "LOW-SENSITIVITY",
        }


# ---------------------------------------------------------------------------
# AC-R14 — INFER_ROUTE PDP decision matrix
# ---------------------------------------------------------------------------


class TestInferRoute:
    """The >=6-case PDP routing matrix (AC-R14)."""

    def test_1_confidential_local_only(self) -> None:
        """Confidential → local only, remote_allowed is False even with an
        explicit ALLOW policy present (the hard floor wins)."""
        allow = _route_policy("ALLOW")
        d = _route([allow], _principal(), {
            "sensitivity_class": "confidential",
            "remote_provider_allowed": True,
        })
        assert isinstance(d, InferRouteDecision)
        assert d.remote_allowed is False
        assert d.model_tier == ModelTier.SMALL_LOCAL.value
        assert d.policy_id is None
        assert d.sensitivity_class == SensitivityClass.CONFIDENTIAL.value

    def test_2_internal_no_policy_default_deny(self) -> None:
        """Internal + no policy → default deny, stays local."""
        d = _route([], _principal(), {
            "sensitivity_class": "internal",
            "remote_provider_allowed": True,
        })
        assert d.remote_allowed is False
        assert d.model_tier == ModelTier.MEDIUM_LOCAL.value
        assert d.policy_id is None
        assert "default deny" in d.reason

    def test_3_internal_explicit_allow(self) -> None:
        """Internal + explicit matching ALLOW + remote permitted → remote."""
        allow = _route_policy("ALLOW")
        d = _route([allow], _principal(), {
            "sensitivity_class": "internal",
            "remote_provider_allowed": True,
        })
        assert d.remote_allowed is True
        assert d.model_tier == ModelTier.LARGE_REASONING.value
        assert d.policy_id == allow.policy_id

    def test_4_public_explicit_allow(self) -> None:
        """Public + explicit matching ALLOW → remote allowed."""
        allow = _route_policy("ALLOW")
        d = _route([allow], _principal(), {
            "sensitivity_class": "public",
            "remote_provider_allowed": True,
        })
        assert d.remote_allowed is True
        assert d.model_tier == ModelTier.LARGE_REASONING.value
        assert d.policy_id == allow.policy_id

    def test_5_override_honored(self) -> None:
        """A classifier override to public + ALLOW → remote (override is
        honored end-to-end into the routing decision)."""
        c = classify_sensitivity("my credit card invoice", override=SensitivityClass.PUBLIC)
        assert c.overridden is True
        allow = _route_policy("ALLOW")
        d = _route([allow], _principal(), {
            "sensitivity_class": c.sensitivity_class.value,
            "remote_provider_allowed": True,
        })
        assert d.remote_allowed is True
        assert d.sensitivity_class == SensitivityClass.PUBLIC.value
        assert d.policy_id == allow.policy_id

    def test_6_personal_communications_exclusion(self) -> None:
        """Partition exclusion at the routing layer: an inference request
        that ``contains_personal_communications`` is never routed remote
        even when a wildcard ALLOW + remote permission exist."""
        allow = _route_policy("ALLOW")
        d = _route([allow], _principal(), {
            "contains_personal_communications": True,
            "remote_provider_allowed": True,
        })
        assert d.remote_allowed is False
        assert d.model_tier == ModelTier.SMALL_LOCAL.value
        assert d.sensitivity_class == SensitivityClass.CONFIDENTIAL.value

    # -- additional hardening (beyond the required six) ---------------------

    def test_explicit_deny_precedence(self) -> None:
        """An explicit DENY overrides any ALLOW, regardless of priority."""
        allow = _route_policy("ALLOW", priority=10)
        deny = _route_policy("DENY", priority=1)
        d = _route([allow, deny], _principal(), {
            "sensitivity_class": "public",
            "remote_provider_allowed": True,
        })
        assert d.remote_allowed is False
        assert d.policy_id == deny.policy_id

    def test_remote_requires_remote_provider_allowed(self) -> None:
        """A matching ALLOW is not sufficient if the transport gate is off."""
        allow = _route_policy("ALLOW")
        d = _route([allow], _principal(), {
            "sensitivity_class": "internal",
            "remote_provider_allowed": False,
        })
        assert d.remote_allowed is False
        assert d.model_tier == ModelTier.MEDIUM_LOCAL.value
        assert d.policy_id == allow.policy_id

    def test_missing_sensitivity_class_fails_closed(self) -> None:
        """No classification metadata → fail closed to confidential (local)."""
        allow = _route_policy("ALLOW")
        d = _route([allow], _principal(), {"remote_provider_allowed": True})
        assert d.remote_allowed is False
        assert d.sensitivity_class == SensitivityClass.CONFIDENTIAL.value

    def test_principal_must_be_nc_principal(self) -> None:
        """A non-NCPrincipal fails closed to local-only (never raises)."""
        d = PolicyDecisionPoint([_route_policy("ALLOW")]).infer_route(
            object(), {"sensitivity_class": "public"}
        )
        assert d.remote_allowed is False
        assert "malformed_principal" in d.notes

    def test_policy_id_none_on_default_deny(self) -> None:
        """Default deny names no policy (policy_id is None)."""
        d = _route(
            [], _principal(), {"sensitivity_class": "public", "remote_provider_allowed": True}
        )
        assert d.policy_id is None

    def test_model_tier_vocabulary(self) -> None:
        """AC-R14: model_tier is always one of the frozen tier literals."""
        for tier in ModelTier:
            assert tier.value in {"small-local", "medium-local", "large-reasoning"}


# ---------------------------------------------------------------------------
# R107 task step 5 — distinct future sending capability
# ---------------------------------------------------------------------------


class TestSendingCapability:
    def test_sending_capability_is_declared_and_distinct(self) -> None:
        """A distinct future sending capability is exposed in the PDP
        (addressable by policy) without any sending endpoint/MCP tool."""
        assert SENDING_CAPABILITY == "personal.sending"
        # It is a distinct name from the routing action, and stable.
        assert SENDING_CAPABILITY != INFER_ROUTE_ACTION
        assert INFER_ROUTE_ACTION == "resource.infer:route"

    def test_sending_capability_addressable_by_policy(self) -> None:
        """A policy can gate the sending capability explicitly; the
        capability is declared but no route is bound to it yet, so an
        ungated request is addressed only by an explicit policy match."""
        pr = _principal()
        send_resource = "nc://personal/inference/routing/sending/next"
        pdp = PolicyDecisionPoint([
            NCPolicy(
                id="row-pol", policy_id=_id("pol"), effect="ALLOW",
                principal_match="kind:human",
                action_match="personal.send:draft",
                resource_match="nc://personal/inference/routing/sending/*",
            ),
        ])
        d = pdp.evaluate(
            principal=pr,
            action="personal.send:draft",
            resource=send_resource,
        )
        assert d.effect == "ALLOW"
        # no policy match for a different action → default deny
        d2 = pdp.evaluate(principal=pr, action="personal.send:other", resource=send_resource)
        assert d2.effect == "DENY"
