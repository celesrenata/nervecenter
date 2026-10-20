"""R105 — Drafts plane tests (gaps1 G-1.4 — AC-R08, prompt2 §40–§41, §63).

Covers:

- **CRUD:** create (201, ``DRAFT`` only), list (ordering + status filter,
  422 on a bad status), get (404 when missing), patch (edit a live draft;
  409 on a non-DRAFT draft), and every response carrying ``basis`` and
  ``evidence``.
- **Discard:** the only lifecycle transition the API performs
  (``DRAFT -> DISCARDED``); re-discard and edit-after-discard are 409;
  the ``discarded_at`` stamp and action history are recorded.
- **SENT unreachable / never claim a send:** no drafts route performs a
  send, no request field can set ``sent``, ``?status=sent`` is always
  empty, and service-side drafting only ever returns ``DRAFT``.
- **Deterministic fallback:** the rule-based drafter is the required,
  always-green path (``model_used == "deterministic"``) — question-aware
  echo, per-channel defaults, deterministic for identical input.
- **Policy-gated model assist:** the R107 ``INFER_ROUTE`` decision runs
  *before* the injected model-assisted drafter is invoked. Confidential /
  ``LOCAL_ONLY`` content is never routed remote (no policy can lift it);
  default-deny holds with no policy; an explicit ALLOW + provider gate
  admits the remote tier; denial and drafter failure both fall back to
  the deterministic drafter with the PDP reason recorded.
- **No plaintext leakage:** audit fields (``basis`` / ``evidence`` /
  ``action_history``) carry refs and short reasons only — raw source
  content never appears in them.

The service is an in-process singleton (no engine, no DB), so API tests get
a fresh app per test via :func:`backend.api.app.create_app` and fresh
singletons via the reset hook — no database or NATS required.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.drafts import (
    DeterministicDrafter,
    Draft,
    DraftContext,
    DraftsService,
    DraftStatus,
    InvalidDraftTransition,
    reset_drafts_service,
)
from backend.policy.pdp import INFER_ROUTE_ACTION, PolicyDecisionPoint
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


class StubModelDrafter:
    """Injectable model-assisted drafter (stub inference client).

    Records every invocation and optionally raises, so tests can assert
    the policy gate decides *before* any inference happens.
    """

    def __init__(self, *, name: str = "stub-model", tier: str = "large-reasoning",
                 text: str = "stub drafted text", raise_exc: Exception | None = None):
        self._name = name
        self._tier = tier
        self._text = text
        self._raise = raise_exc
        self.invocations: list[DraftContext] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def required_tier(self) -> str:
        return self._tier

    def draft(self, context: DraftContext) -> str:
        self.invocations.append(context)
        if self._raise is not None:
            raise self._raise
        return self._text


@pytest.fixture
def client():
    reset_drafts_service()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_drafts_service()


def _create(client: TestClient, **over) -> dict[str, Any]:
    payload = {
        "channel": "email",
        "text": "Thanks — let me confirm and get back to you.",
        "basis": ["reply to inbound ask"],
        "evidence": ["email:thread-1:msg-7"],
    }
    payload.update(over)
    res = client.post("/api/drafts", json=payload)
    assert res.status_code == 201, res.text
    return res.json()


def _audit_fields(draft: dict[str, Any]) -> list[str]:
    """Every audit-facing string on a draft (basis, evidence, action
    history). Used by the no-plaintext-leakage assertions."""
    strings: list[str] = []
    strings.extend(draft["basis"])
    strings.extend(draft["evidence"])
    for entry in draft["action_history"]:
        strings.append(entry["action"])
        if entry.get("detail"):
            strings.append(entry["detail"])
    return strings


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_draft_defaults():
    from datetime import UTC, datetime

    d = Draft(
        draft_id="draft-x",
        channel="imessage",
        text="hi",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    assert d.status == DraftStatus.DRAFT
    assert d.model_used == "deterministic"
    assert d.classification == "unclassified"
    assert d.source == "api"
    assert d.basis == []
    assert d.evidence == []
    assert d.action_history == []
    assert d.discarded_at is None
    assert d.is_draft and not d.is_discarded


def test_draft_status_parse():
    assert DraftStatus.parse("DRAFT") == "draft"
    assert DraftStatus.parse(" discarded ") == "discarded"
    assert DraftStatus.parse("sent") == "sent"
    with pytest.raises(ValueError):
        DraftStatus.parse("shipped")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_create_and_get_draft(client: TestClient):
    body = _create(client, channel="imessage", conversation_id="conv-1",
                   communication_ref="comm-9")
    assert body["draft_id"].startswith("draft-")
    assert body["channel"] == "imessage"
    assert body["conversation_id"] == "conv-1"
    assert body["communication_ref"] == "comm-9"
    assert body["status"] == "draft"
    assert body["model_used"] == "manual"
    assert body["source"] == "api"
    assert body["action_history"][0]["action"] == "created"

    got = client.get(f"/api/drafts/{body['draft_id']}")
    assert got.status_code == 200
    assert got.json() == body


def test_create_rejects_empty_text_and_bad_channel(client: TestClient):
    res = client.post("/api/drafts", json={"channel": "email", "text": ""})
    assert res.status_code == 422
    res = client.post("/api/drafts", json={"channel": "carrier-pigeon", "text": "x"})
    assert res.status_code == 422


def test_get_missing_draft_404(client: TestClient):
    assert client.get("/api/drafts/draft-nope").status_code == 404


def test_list_drafts_ordering_and_filter(client: TestClient):
    first = _create(client, channel="email")
    second = _create(client, channel="imessage", text="later draft")

    # Most recently updated first: second was created after first.
    ids = [d["draft_id"] for d in client.get("/api/drafts").json()["data"]]
    assert ids == [second["draft_id"], first["draft_id"]]

    # Discarding is a lifecycle action that bumps updated_at, so the
    # discarded draft becomes the most-recently-updated row.
    client.post(f"/api/drafts/{first['draft_id']}/discard", json={})
    all_items = client.get("/api/drafts").json()
    assert all_items["meta"]["pagination"]["total"] == 2
    assert [d["draft_id"] for d in all_items["data"]] == [first["draft_id"], second["draft_id"]]

    only_drafts = client.get("/api/drafts?status=draft").json()["data"]
    assert [d["draft_id"] for d in only_drafts] == [second["draft_id"]]
    only_discarded = client.get("/api/drafts?status=discarded").json()["data"]
    assert [d["draft_id"] for d in only_discarded] == [first["draft_id"]]
    assert client.get("/api/drafts?status=sent").json()["data"] == []

    bad = client.get("/api/drafts?status=shipped")
    assert bad.status_code == 422


def test_patch_live_draft(client: TestClient):
    body = _create(client)
    res = client.patch(
        f"/api/drafts/{body['draft_id']}",
        json={"text": "revised", "basis": ["re-based"], "evidence": ["ref:2"]},
    )
    assert res.status_code == 200
    updated = res.json()
    assert updated["text"] == "revised"
    assert updated["basis"] == ["re-based"]
    assert updated["evidence"] == ["ref:2"]
    assert updated["status"] == "draft"
    assert [a["action"] for a in updated["action_history"]] == ["created", "updated"]

    # Partial patch keeps other fields.
    partial = client.patch(f"/api/drafts/{body['draft_id']}", json={"text": "v3"})
    assert partial.status_code == 200
    assert partial.json()["text"] == "v3"
    assert partial.json()["basis"] == ["re-based"]


def test_every_response_includes_basis_and_evidence(client: TestClient):
    body = _create(client)
    did = body["draft_id"]
    responses = [
        client.get("/api/drafts"),
        client.get(f"/api/drafts/{did}"),
        client.patch(f"/api/drafts/{did}", json={"text": "x"}),
        client.post(f"/api/drafts/{did}/discard", json={}),
    ]
    assert all(r.status_code in (200, 201) for r in responses)
    records = [responses[0].json()["data"]] + [r.json() for r in responses[1:]]
    for record in records:
        for item in record if isinstance(record, list) else [record]:
            assert "basis" in item and "evidence" in item
            assert isinstance(item["basis"], list)
            assert isinstance(item["evidence"], list)


# ---------------------------------------------------------------------------
# Discard + invalid transitions
# ---------------------------------------------------------------------------


def test_discard_is_the_only_transition(client: TestClient):
    body = _create(client)
    did = body["draft_id"]

    res = client.post(f"/api/drafts/{did}/discard", json={"reason": "not needed"})
    assert res.status_code == 200
    discarded = res.json()
    assert discarded["status"] == "discarded"
    assert discarded["discarded_at"] is not None
    assert discarded["action_history"][-1]["action"] == "discarded"
    assert discarded["action_history"][-1]["detail"] == "not needed"

    # Re-discard is an invalid transition (already terminal).
    again = client.post(f"/api/drafts/{did}/discard", json={})
    assert again.status_code == 409
    # Editing a discarded draft is an invalid transition too.
    assert client.patch(f"/api/drafts/{did}", json={"text": "z"}).status_code == 409
    assert "discarded" in again.json()["detail"]


def test_discard_missing_draft_404(client: TestClient):
    assert client.post("/api/drafts/draft-nope/discard", json={}).status_code == 404


def test_sent_status_unreachable_via_api(client: TestClient):
    # (1) No drafts route performs a send.
    from backend.api.endpoints.drafts import router as drafts_router

    paths = {r.path for r in drafts_router.routes}
    assert not any("send" in p for p in paths)
    assert paths == {
        "/api/drafts",
        "/api/drafts/",
        "/api/drafts/{draft_id}",
        "/api/drafts/{draft_id}/discard",
    }

    # (2) The create request body has no status field — the API can only
    #     ever create DRAFT.
    from backend.api.endpoints.drafts import CreateDraftRequest

    assert "status" not in CreateDraftRequest.model_fields
    assert "sent" not in Draft.model_fields or Draft.model_fields["status"].default == "draft"

    # (3) A 'sent' filter is always empty: nothing can reach that state.
    _create(client)
    assert client.get("/api/drafts?status=sent").json()["data"] == []

    # (4) The service never returns a sent draft, no matter what.
    svc = DraftsService()
    made = svc.draft(channel="email", classification="internal",
                     inbound_excerpt="anything")
    assert made.status == "draft"


def test_sent_unreachable_at_service_level():
    """No service method exists to move a draft to ``sent`` — the only
    transition the plane performs is ``DRAFT -> DISCARDED``."""
    svc = DraftsService()
    assert not hasattr(svc, "send")
    assert not hasattr(svc, "mark_sent")
    d = svc.create(channel="email", text="t")
    svc.discard(d.draft_id)
    assert d.status == "discarded"
    with pytest.raises(InvalidDraftTransition):
        svc.discard(d.draft_id)


# ---------------------------------------------------------------------------
# Deterministic fallback
# ---------------------------------------------------------------------------


def test_deterministic_fallback_is_the_required_path():
    svc = DraftsService()
    draft = svc.draft(
        channel="email",
        classification="internal",
        conversation_id="conv-1",
        communication_ref="comm-9",
        inbound_excerpt="Can you review the proposal?",
    )
    assert draft.status == "draft"
    assert draft.model_used == "deterministic"
    assert draft.source == "rule-based"
    assert draft.text == "Sure — let me check and get back to you."
    assert "rule-based deterministic fallback" in " ".join(draft.basis)
    assert [a.action for a in draft.action_history] == ["created", "fallback"]
    # Evidence carries refs only.
    assert draft.evidence == [
        "drafts-plane: rule-safe refs only (no raw content)",
        "communication:comm-9",
        "conversation:conv-1",
    ]


def test_deterministic_drafter_is_deterministic_and_rule_based():
    drafter = DeterministicDrafter()
    assert drafter.name == "deterministic"
    assert drafter.required_tier == "none"

    q = DraftContext(channel="email", inbound_excerpt="Are we still on for Thursday?")
    assert drafter.draft(q) == "Sure — let me check and get back to you."
    assert drafter.draft(q) == drafter.draft(q)  # no clock, no randomness

    stmt = DraftContext(channel="email", inbound_excerpt="The invoice arrived. Details inside.")
    assert drafter.draft(stmt) == "Thanks, noted. (The invoice arrived.) I'll follow up soon."

    # Channel defaults when there is no inbound excerpt.
    assert drafter.draft(DraftContext(channel="email")) == (
        "Thanks — noted. I'll get back to you with the details."
    )
    assert drafter.draft(DraftContext(channel="voicemail")) == (
        "Thanks for the voicemail — I'll call you back."
    )
    assert drafter.draft(DraftContext(channel="imessage")) == (
        "Got it — I'll reply properly in a moment."
    )


def test_deterministic_fallback_with_model_assist_denied_keeps_basis():
    # Confidential content, remote-tier drafter, even with an explicit ALLOW
    # policy and provider gate open: the PDP floor holds, the stub is never
    # invoked, and the deterministic text + denial basis are recorded.
    svc = DraftsService(
        pdp=PolicyDecisionPoint([_route_policy("ALLOW")]),
        principal=_principal(),
        model_assisted=StubModelDrafter(),
        remote_provider_allowed=True,
    )
    raw = "My bank account number is 123-456-789."
    draft = svc.draft(channel="email", classification="confidential",
                      inbound_excerpt=raw)
    assert draft.model_used == "deterministic"
    assert any(a.action == "policy_denied" for a in draft.action_history)
    assert any(a.action == "fallback" for a in draft.action_history)
    assert any("model-assist denied" in b for b in draft.basis)


# ---------------------------------------------------------------------------
# Policy-gated model assist
# ---------------------------------------------------------------------------


def test_policy_denial_default_deny_without_policies():
    svc = DraftsService(
        pdp=PolicyDecisionPoint([]),
        principal=_principal(),
        model_assisted=StubModelDrafter(),
        remote_provider_allowed=True,
    )
    stub = StubModelDrafter()
    svc.set_model_assisted_drafter(stub)
    draft = svc.draft(channel="email", classification="internal",
                      inbound_excerpt="quick question?")
    assert draft.model_used == "deterministic"
    assert stub.invocations == []  # policy ran first; drafter never called
    denied = [a for a in draft.action_history if a.action == "policy_denied"]
    assert denied and "denied" in denied[0].detail.lower()


def test_policy_allows_remote_tier_with_explicit_allow_and_gate():
    stub = StubModelDrafter(text="model drafted reply")
    svc = DraftsService(
        pdp=PolicyDecisionPoint([_route_policy("ALLOW")]),
        principal=_principal(),
        model_assisted=stub,
        remote_provider_allowed=True,
    )
    draft = svc.draft(channel="email", classification="internal",
                      inbound_excerpt="question?")
    assert draft.model_used == "stub-model"
    assert draft.source == "model-assisted"
    assert draft.text == "model drafted reply"
    assert len(stub.invocations) == 1
    actions = [a.action for a in draft.action_history]
    assert "routing_decided" in actions and "model_assisted" in actions
    # The PDP named the policy that allowed the route (audit, §71).
    routing = next(a for a in draft.action_history if a.action == "routing_decided")
    assert routing.policy_id is not None


def test_policy_denies_remote_when_provider_gate_closed():
    stub = StubModelDrafter()
    svc = DraftsService(
        pdp=PolicyDecisionPoint([_route_policy("ALLOW")]),
        principal=_principal(),
        model_assisted=stub,
        remote_provider_allowed=False,  # transport-level gate closed
    )
    draft = svc.draft(channel="email", classification="internal",
                      inbound_excerpt="question?")
    assert stub.invocations == []
    assert draft.model_used == "deterministic"
    assert any(a.action == "policy_denied" for a in draft.action_history)


def test_policy_denies_remote_for_local_only_content_even_with_allow():
    # AC-R08: confidential / local-only cannot be routed remote — no policy
    # can lift the §36 floor.
    stub = StubModelDrafter()
    svc = DraftsService(
        pdp=PolicyDecisionPoint([_route_policy("ALLOW")]),
        principal=_principal(),
        model_assisted=stub,
        remote_provider_allowed=True,
    )
    draft = svc.draft(channel="imessage", classification="confidential",
                      inbound_excerpt="personal question?")
    assert stub.invocations == []
    assert draft.model_used == "deterministic"
    denied = next(a for a in draft.action_history if a.action == "policy_denied")
    assert "confidential" in denied.detail or "LOCAL_ONLY" in denied.detail
    assert any(a.action == "fallback" for a in draft.action_history)


def test_local_tier_drafter_allowed_for_confidential_content():
    # A local tier processes personal content *locally* — that is exactly
    # the LOCAL_ONLY disposition the PDP grants; no remote is named.
    stub = StubModelDrafter(name="local-llm", tier="small-local", text="local text")
    svc = DraftsService(
        pdp=PolicyDecisionPoint([]),  # default deny; local tier needs no remote
        principal=_principal(),
        model_assisted=stub,
        remote_provider_allowed=False,
    )
    draft = svc.draft(channel="email", classification="confidential",
                      inbound_excerpt="private?")
    assert draft.model_used == "local-llm"
    assert draft.text == "local text"
    assert len(stub.invocations) == 1
    routing = next(a for a in draft.action_history if a.action == "routing_decided")
    assert "LOCAL_ONLY" in routing.detail or "local" in routing.detail


def test_model_assist_failure_falls_back_to_deterministic():
    stub = StubModelDrafter(raise_exc=RuntimeError("provider down"))
    svc = DraftsService(
        pdp=PolicyDecisionPoint([_route_policy("ALLOW")]),
        principal=_principal(),
        model_assisted=stub,
        remote_provider_allowed=True,
    )
    draft = svc.draft(channel="email", classification="internal",
                      inbound_excerpt="question?")
    assert draft.model_used == "deterministic"
    assert draft.text == "Sure — let me check and get back to you."
    assert any(a.action == "model_assist_failed" for a in draft.action_history)


# ---------------------------------------------------------------------------
# No plaintext leakage
# ---------------------------------------------------------------------------


def test_no_plaintext_leakage_in_audit_fields():
    raw = "SSN 123-45-6789 and account 9876."
    stub = StubModelDrafter()
    svc = DraftsService(
        pdp=PolicyDecisionPoint([_route_policy("ALLOW")]),
        principal=_principal(),
        model_assisted=stub,
        remote_provider_allowed=True,
    )
    draft = svc.draft(channel="email", classification="confidential",
                      conversation_id="conv-7", communication_ref="comm-3",
                      inbound_excerpt=raw)
    strings = _audit_fields(draft.model_dump(mode="json"))
    for s in strings:
        assert "123-45-6789" not in s
        assert "account 9876" not in s
    # Evidence is refs only.
    for ref in draft.evidence:
        assert ref.startswith(("communication:", "conversation:", "drafts-plane:"))


def test_no_plaintext_leakage_after_denial_and_fallback():
    raw = "divorce settlement details inside."
    svc = DraftsService(
        pdp=PolicyDecisionPoint([]),
        principal=_principal(),
        model_assisted=StubModelDrafter(),
        remote_provider_allowed=False,
    )
    draft = svc.draft(channel="imessage", classification="confidential",
                      inbound_excerpt=raw)
    strings = _audit_fields(draft.model_dump(mode="json"))
    for s in strings:
        assert "divorce settlement" not in s
    # The PDP denial reason is a short policy string, not the content.
    denied = next(a for a in draft.action_history if a.action == "policy_denied")
    assert "divorce" not in denied.detail
    # The deterministic fallback text is local-only and human-reviewed.
    assert draft.status == "draft"
