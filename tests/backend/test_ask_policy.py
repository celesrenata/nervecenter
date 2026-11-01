"""Focused tests for the Ask policy-denial bug fix.

Regression coverage for the Ask service's R107 PDP pre-flight.

Root cause fixed: :func:`backend.context.ask.get_ask_service` constructed
:class:`AskService` with a bare :class:`PolicyDecisionPoint` (no policies) —
default-deny for *every* sensitivity class — so a benign question
(``hello world``, sensitivity ``internal``) was refused with the structured
``policy_denied`` answer even though the content was public-facing. The
absence of a transport is a *transport* problem, not a *policy* problem, and
should degrade to the honest 202 ``unavailable`` shape.

After the fix:

- **Benign (public / internal) content is NOT policy-denied.** The default
  service carries an explicit ALLOW ``infer_route`` policy for the
  remote-capable tiers and opens the ``remote_provider_allowed`` transport
  gate so the *policy* — not the transport gate — governs routing. With no
  transport configured the request degrades to the honest 202 — never a
  fabricated answer.
- **Confidential / LOCAL_ONLY content is still denied.** The PDP's hard
  floor fires before any policy matching, so the ALLOW policy cannot lift
  confidential content to a remote tier.
- **No fabricated answer** on the denied or unavailable paths.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.endpoints.context import router as context_router
from backend.context.ask import (
    AskContext,
    AskRequest,
    AskService,
    get_ask_service,
    reset_ask_service,
)
from backend.inference.omniroute import OmniRouteTransport
from backend.policy.pdp import INFER_ROUTE_ACTION, PolicyDecisionPoint
from schemas.nc_platform import NCPolicy

# A valid ULID body (26 chars, Crockford base32 — no I/L/O/U).
_ULID_BODY = "0123456789ABCDEFGHJKMNPTWX"
assert len(_ULID_BODY) == 26


def _id(prefix: str) -> str:
    return f"{prefix}_{_ULID_BODY}"


def _deny_policy() -> NCPolicy:
    """High-priority DENY that must override the default ALLOW."""
    return NCPolicy(
        id="row-deny",
        policy_id=_id("pol"),
        effect="DENY",
        principal_match="*",
        action_match=INFER_ROUTE_ACTION,
        resource_match="*",
        priority=100,
    )


def _benign_request(question: str = "hello world") -> AskRequest:
    """A benign, public-facing question (no sensitivity override → internal)."""
    return AskRequest(question=question)


def _confidential_request(question: str = "hello world") -> AskRequest:
    """A question explicitly classified confidential (LOCAL_ONLY)."""
    return AskRequest(question=question, context={"sensitivity": "confidential"})


def _assert_not_fabricated(body: dict) -> None:
    """No fabricated answer: nothing was executed and no real answer text."""
    assert body.get("executed") is False
    answer = body.get("answer")
    # ``answer`` must be either ``None`` (unavailable) or the fixed, honest
    # denial statement (policy_denied) — never a model-generated answer.
    assert answer is None or "No answer is fabricated" in answer
    assert body.get("grounded") is not True


@pytest.fixture(autouse=True)
def reset_global_state():
    """Each test starts with a clean ask-service singleton."""
    reset_ask_service()
    yield
    reset_ask_service()


class TestBenignIsNotPolicyDenied:
    """Benign (public/internal) content must no longer be policy_denied."""

    def test_factory_service_benign_is_unavailable_not_denied(self):
        """The factory service (the endpoint's service) returns honest 202."""
        service = get_ask_service()
        outcome = service.ask(_benign_request("hello world"))
        # It may honestly be unavailable — but it must NOT be policy_denied.
        assert outcome.status_code == 202
        assert outcome.body["status"] == "unavailable"
        assert outcome.body["request_status"] == "pending"
        _assert_not_fabricated(outcome.body)

    def test_benign_internal_no_longer_policy_denied(self):
        """'hello world' (internal) → allowed by policy, then honest 202."""
        # Mirrors the factory: default ALLOW policy + transport gate open.
        service = AskService(remote_provider_allowed=True)
        outcome = service.ask(_benign_request("hello world"))
        assert outcome.status_code == 202
        assert outcome.body["status"] == "unavailable"
        _assert_not_fabricated(outcome.body)

    def test_benign_explicit_public_not_policy_denied(self):
        """Explicit 'public' tier is remote-capable → allowed, then 202."""
        service = AskService(remote_provider_allowed=True)
        request = AskRequest(question="hello world", context={"sensitivity": "public"})
        outcome = service.ask(request)
        assert outcome.status_code == 202
        assert outcome.body["status"] == "unavailable"
        _assert_not_fabricated(outcome.body)

    def test_default_pdp_allows_remote_for_benign_tiers(self):
        """The default PDP names a remote tier for public/internal classes."""
        service = AskService(remote_provider_allowed=True)
        for sensitivity in ("public", "internal"):
            decision = service._preflight(AskContext(sensitivity=sensitivity))
            assert decision.remote_allowed is True, (
                f"expected remote_allowed for {sensitivity!r}: {decision.reason}"
            )
            assert decision.policy_id is not None

    def test_internal_default_tier_is_not_policy_denied(self):
        """A request with no sensitivity override defaults to internal → 202."""
        service = get_ask_service()
        outcome = service.ask(_benign_request())
        assert outcome.body["status"] != "policy_denied"
        assert outcome.body["status"] == "unavailable"


class TestConfidentialStillDenied:
    """Confidential / LOCAL_ONLY content is still denied (hard floor)."""

    def test_confidential_still_policy_denied(self):
        """Confidential content is denied with the structured denial body."""
        service = get_ask_service()
        outcome = service.ask(_confidential_request())
        assert outcome.status_code == 200
        body = outcome.body
        assert body["status"] == "policy_denied"
        assert body["request_status"] == "denied"
        assert body["policy"]["denied"] is True
        # Reason names the LOCAL_ONLY hard floor, not the transport gate.
        assert "LOCAL_ONLY" in body["policy"]["reason"]
        _assert_not_fabricated(body)

    def test_confidential_denied_even_when_remote_gate_open(self):
        """Opening the transport gate does not lift confidential to remote."""
        service = AskService(remote_provider_allowed=True)
        decision = service._preflight(AskContext(sensitivity="confidential"))
        assert decision.remote_allowed is False
        assert decision.sensitivity_class == "confidential"

    def test_confidential_denial_is_deterministic(self):
        """Two identical confidential requests produce the same denial shape."""
        service = get_ask_service()
        first = service.ask(_confidential_request())
        second = service.ask(_confidential_request())
        assert first.body["status"] == second.body["status"] == "policy_denied"
        assert first.body["policy"] == second.body["policy"]


class TestNoFabricatedAnswer:
    """Denied and unavailable paths never return a fabricated answer."""

    def test_policy_denied_has_no_real_answer(self):
        service = get_ask_service()
        body = service.ask(_confidential_request()).body
        assert body["answer"] is not None
        assert "No answer is fabricated in response to a policy denial" in body["answer"]
        assert body["citations"] == []
        assert body["grounded"] is False

    def test_unavailable_returns_no_answer(self):
        service = get_ask_service()
        body = service.ask(_benign_request()).body
        assert body["answer"] is None
        assert body["citations"] == []

    def test_explicit_deny_overrides_default_allow(self):
        """A DENY policy still wins over the benign-tier ALLOW (deny precedence)."""
        service = AskService(
            pdp=PolicyDecisionPoint([_deny_policy()]),
            remote_provider_allowed=True,
        )
        outcome = service.ask(_benign_request())
        assert outcome.body["status"] == "policy_denied"
        assert outcome.body["policy"]["denied"] is True
        _assert_not_fabricated(outcome.body)

    def test_unavailable_after_allow_decision(self):
        """Transport present but unreachable → honest 202, not fabricated."""
        transport = OmniRouteTransport(base_url=None)  # not configured
        assert transport.fixture_mode is False
        service = AskService(transport=transport, remote_provider_allowed=True)
        outcome = service.ask(_benign_request())
        assert outcome.status_code == 202
        assert outcome.body["status"] == "unavailable"
        _assert_not_fabricated(outcome.body)


def _app_with_default_service() -> FastAPI:
    """Minimal app exposing the ask endpoint, resolving the default service."""
    app = FastAPI()
    app.include_router(context_router)
    return app


class TestEndpointBehavior:
    """Endpoint-level regression: the exact POST /api/context/ask symptom."""

    def test_hello_world_not_policy_denied(self):
        """POST {"question": "hello world"} → honest 202, not policy_denied."""
        app = _app_with_default_service()
        with TestClient(app) as client:
            resp = client.post("/api/context/ask", json={"question": "hello world"})
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "unavailable"
        _assert_not_fabricated(body)

    def test_confidential_endpoint_still_denied(self):
        """Confidential content via the endpoint is still policy_denied."""
        app = _app_with_default_service()
        with TestClient(app) as client:
            resp = client.post(
                "/api/context/ask",
                json={
                    "question": "hello world",
                    "context": {"sensitivity": "confidential"},
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "policy_denied"
        _assert_not_fabricated(body)
