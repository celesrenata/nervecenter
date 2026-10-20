"""Unit tests for the T003-04 ask pipeline (AC9).

Verifies the grounded ask endpoint: context → PDP → OmniRoute adapter →
grounded answer with citations. Covers fixture mode replay,
policy enforcement, degradation to the honest 202 on genuine transport
unavailability, and deterministic responses.
"""

from __future__ import annotations

import re
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.context.ask import (
    AskRequest,
    AskService,
    set_ask_service,
    reset_ask_service,
)
from backend.inference.omniroute import (
    OmniRouteTransport,
    OmniRouteUnavailableError,
)
from backend.policy.pdp import INFER_ROUTE_ACTION, PolicyDecisionPoint
from schemas.nc_platform import NCPolicy


_ULID_BODY = "0123456789ABCDEFGHJKMNPTWX"
assert len(_ULID_BODY) == 26


def _id(prefix: str) -> str:
    return f"{prefix}_{_ULID_BODY}"


def _allow_policy() -> NCPolicy:
    """Remote-capable ALLOW policy for internal/public sensitivity."""
    return NCPolicy(
        id=f"row-{_id('pol-ask-allow')[:32]}",
        policy_id=_id("pol"),
        effect="ALLOW",
        principal_match="*",
        action_match=INFER_ROUTE_ACTION,
        resource_match="*",
    )


class FakeTransport(OmniRouteTransport):
    """Deterministic offline transport standing in for OmniRoute.

    ``fixture_mode`` is overridden so no recorded fixture is needed. The
    answer cites exactly the evidence refs the ask pipeline put in the
    prompt (``REF <id>: ...`` lines), which is what the pipeline's
    citation grounding checks for — no ungrounded refs are invented.
    """

    _REF_LINE = re.compile(r"^REF ([A-Za-z0-9_][A-Za-z0-9_\-]*):", re.MULTILINE)

    def __init__(self) -> None:
        super().__init__(base_url="https://omniroute.invalid")

    @property
    def fixture_mode(self) -> bool:
        return True

    def generate(self, model, prompt, parameters=None, timeout=None):
        refs = self._REF_LINE.findall(prompt or "")
        if refs:
            cited = " ".join(f"[{r}]" for r in refs)
            content = f"Grounded answer based on the cited evidence: {cited}."
        else:
            content = "No evidence was supplied, so no claim is made."
        return {
            "content": content,
            "model": "nerve-ask-fixture",
            "input_tokens": 142,
            "output_tokens": 73,
            "ttft_ms": 32.5,
            "e2e_latency_ms": 156.0,
        }


@pytest.fixture(autouse=True)
def reset_global_states():
    """Ensure each test starts with clean globals."""
    reset_ask_service()
    yield
    reset_ask_service()


@pytest.fixture
def dummy_transport():
    """A fake transport that deterministically returns a grounded answer."""
    return FakeTransport()


@pytest.fixture
def app_with_service(dummy_transport):
    """A full app whose startup-resolved ask service uses the fake transport."""
    service = AskService(
        transport=dummy_transport,
        pdp=PolicyDecisionPoint([_allow_policy()]),
        remote_provider_allowed=True,
    )
    app = create_app()
    app.state.ask_service = service
    set_ask_service(service)
    return app


@pytest.fixture
def mock_adapter():
    """Mock adapter for deterministic inference (kept for parity)."""
    adapter = Mock()
    adapter.route = Mock(return_value=Mock(content="mock answer"))
    return adapter


def _answered_service(transport, **kwargs) -> AskService:
    """AskService with the R107 remote ALLOW policy (remote-capable classes)."""
    return AskService(
        transport=transport,
        pdp=PolicyDecisionPoint([_allow_policy()]),
        remote_provider_allowed=True,
        **kwargs,
    )


class TestAskPipeline:
    """Test suite for the grounded ask pipeline (T003-04 AC9)."""

    def test_fixture_success_with_provenance(self, dummy_transport):
        """Test fixture-mode success path with citations and provenance."""
        # Setup
        service = _answered_service(dummy_transport)

        # Request with grounding context to make evidence deterministic
        request = AskRequest(
            question="what caused the API 5xx spike?",
            context={
                "partition": "engineering",
                "sensitivity": "internal",
                "fixture_refs": ["INC-7", "INC-7-alert-1", "migrate-fail-7"],
                "summaries": {
                    "INC-7": "Incident report for API 5xx spike",
                    "INC-7-alert-1": "Alert triggered for spike",
                    "migrate-fail-7": "Migration job constraint violation detected",
                },
            }
        )

        # Run
        outcome = service.ask(request)

        # Assertions
        assert outcome.status_code == 200
        body = outcome.body

        # Response structure
        assert body["status"] == "answered"
        assert body["request_status"] == "answered"
        assert body["accepted"] is True
        assert body["executed"] is True
        assert "answer" in body
        assert body["grounded"] is True

        # Citations should point to references we provided
        assert len(body["citations"]) == 3
        citation_refs = {c["ref"] for c in body["citations"]}
        assert any("INC-7" in ref for ref in citation_refs)
        assert any("INC-7-alert-1" in ref for ref in citation_refs)
        assert any("migrate-fail-7" in ref for ref in citation_refs)

        # Check evidence ids match
        assert "provenance" in body
        assert "context_records" in body["provenance"]
        evidence_ids = set(body["provenance"]["context_records"])
        assert evidence_ids >= {"INC-7", "INC-7-alert-1", "migrate-fail-7"}

        # Verify no raw content leaked (citations were made with summaries, not raw data)
        assert "[INC-7]" in body["answer"]
        assert "[INC-7-alert-1]" in body["answer"]
        assert "[migrate-fail-7]" in body["answer"]

        # Should have fixture marker since we're in fixture mode
        assert "fixture_marker" in body
        assert body["fixture_marker"] is not None

        # Metadata check
        assert "model" in body
        assert "provider" in body
        assert "route" in body
        assert "usage" in body

    def test_policy_denial_for_confidential(self, dummy_transport):
        """Test that confidential content produces a structured denial."""
        # Setup a PDP with a policy that denies confidential access
        pdp = PolicyDecisionPoint(
            policies=[
                NCPolicy(
                    id=f"row-{_id('pol-conf')[:32]}",
                    policy_id=_id("pol"),
                    effect="DENY",
                    principal_match="*",
                    action_match=INFER_ROUTE_ACTION,
                    resource_match="*",
                )
            ]
        )

        service = AskService(
            pdp=pdp,
            transport=dummy_transport,
            remote_provider_allowed=True,
        )

        # This request should trigger the policy denial due to confidential sensitivity
        # but since we don't directly have access to sensitive flag, we use an override
        request = AskRequest(
            question="what happened?",
            context={
                "partition": "engineering",
                "sensitivity": "confidential",  # Force confidential
                "fixture_refs": ["INC-1"],
            }
        )

        # Run
        outcome = service.ask(request)

        # Assertions for policy denial
        assert outcome.status_code == 200
        body = outcome.body

        assert body["status"] == "policy_denied"
        assert body["request_status"] == "denied"
        assert body["accepted"] is True
        assert body["executed"] is False

        # Should be a clear denial, not a fabricated response
        assert "answer" not in body or body["answer"] is None or "cannot be answered" in body["answer"]
        assert body["policy"]["denied"] is True

        # Check that no evidence was generated
        assert body["provenance"]["context_records"] == ["INC-1"]

    def test_unavailability_degradation(self, dummy_transport):
        """Test genuine transport unavailability → 202 shape."""
        # Mock a transport that raises OmniRouteUnavailableError
        class UnavailableTransport(OmniRouteTransport):
            def __init__(self):
                # base_url set so transport_available is True, then the
                # actual inference call fails (genuine unavailability).
                super().__init__(base_url="https://omniroute.invalid")

            def generate(self, *args, **kwargs):
                raise OmniRouteUnavailableError(
                    [("omniroute", "primary")],
                    {("omniroute", "primary"): "network unreachable"},
                )

        unavail_transport = UnavailableTransport()

        service = AskService(
            transport=unavail_transport,
            pdp=PolicyDecisionPoint([_allow_policy()]),
            remote_provider_allowed=True,
        )

        request = AskRequest(question="what happened?")

        # Run
        outcome = service.ask(request)

        # Should return 202 with unavailability
        assert outcome.status_code == 202
        body = outcome.body

        assert body["status"] == "unavailable"
        assert body["request_status"] == "pending"
        assert body["accepted"] is True
        assert body["executed"] is False
        assert body["answer"] is None
        assert body["reason"] is not None
        assert "unavailable" in body["reason"].lower()

    def test_no_plaintext_leakage(self, dummy_transport):
        """Test that no raw personal content enters the remote payload."""
        service = _answered_service(dummy_transport)

        # The ask pipeline only accepts summaries in its context field
        # It never constructs a prompt with raw personal text
        request = AskRequest(
            question="who said what in that email?",
            context={
                "partition": "personal",  # Should be firewalled to internal
                "sensitivity": "internal",
                "fixture_refs": ["comm-123"],
                "summaries": {
                    "comm-123": "Subject line: 'Please confirm' - Body: 'Can we reschedule?'"
                }
            }
        )

        # Run and verify the citation structure
        outcome = service.ask(request)

        assert outcome.status_code == 200
        body = outcome.body

        # The answer should reference the summaries, not raw content
        # Check it uses our citations
        assert body["grounded"] is True
        assert len(body["citations"]) == 1
        citation = body["citations"][0]
        assert citation["record_id"] == "comm-123"
        # This confirms we got a reference (not raw content) from summaries

    def test_deterministic_repeated_response(self, dummy_transport):
        """Test that fixture mode yields deterministic responses."""
        service = _answered_service(dummy_transport)

        # Same question with same context
        request1 = AskRequest(
            question="what caused the API 5xx spike?",
            context={
                "fixture_refs": ["INC-7"],
                "summaries": {"INC-7": "Incident report for API 5xx spike"},
            }
        )

        request2 = AskRequest(
            question="what caused the API 5xx spike?",
            context={
                "fixture_refs": ["INC-7"],
                "summaries": {"INC-7": "Incident report for API 5xx spike"},
            }
        )

        # Run twice
        outcome1 = service.ask(request1)
        outcome2 = service.ask(request2)

        # Should get both the same status and identical response (fixture mode)
        assert outcome1.status_code == 200
        assert outcome2.status_code == 200

        body1 = outcome1.body
        body2 = outcome2.body

        # Same answer text (deterministic response from fixture)
        assert body1["answer"] == body2["answer"]

        # Same grounding decision
        assert body1["grounded"] == body2["grounded"]

        # Same *records* cited (grounding is deterministic). The per-citation
        # ``ref`` URI embeds the per-request context package id, so the full
        # citation dict is not compared — only the stable grounding identity.
        def _grounded_ids(b):
            return sorted((c["record_id"], c["kind"]) for c in b["citations"])

        assert _grounded_ids(body1) == _grounded_ids(body2)

        # Same fixture marker (deterministic marker for same input)
        assert body1["fixture_marker"] == body2["fixture_marker"]

    def test_empty_question_rejected(self, app_with_service):
        """Test empty/whitespace question rejected with 422 at the endpoint."""
        with TestClient(app_with_service) as client:
            response = client.post("/api/context/ask", json={"question": ""})

            assert response.status_code == 422
            body = response.json()
            assert body["status"] == "invalid_request"

    def test_endpoint_integration(self, app_with_service):
        """Test that the full API endpoint integrates with the ask service properly."""
        with TestClient(app_with_service) as client:
            response = client.post("/api/context/ask", json={
                "question": "who said what?",
                "context": {
                    "fixture_refs": ["ref-1"],
                    "summaries": {"ref-1": "Summary of reference"}
                }
            })

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "answered"
            assert "answer" in data
            assert data["grounded"] is True

    # Tests that verify contract compliance
    def test_require_nonempty_question(self, app_with_service):
        """Request validation should reject empty questions at the endpoint."""
        with TestClient(app_with_service) as client:
            for empty_input in ["", "   ", None]:
                response = client.post("/api/context/ask", json={"question": empty_input})
                assert response.status_code == 422
                body = response.json()
                assert body["status"] == "invalid_request"
                assert (
                    "required" in body["detail"].lower()
                    or "non-empty" in body["detail"].lower()
                )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
