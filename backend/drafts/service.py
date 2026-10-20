"""In-memory Drafts service (gaps1 G-1.4 — R105, prompt2 §40–§41, §63).

Design notes
------------
- **NerveCenter never sends.** The drafts plane stores, edits, and discards
  drafts; the only status transition it may perform is
  ``DRAFT -> DISCARDED``. ``SENT`` is unreachable through this plane — it
  is set exclusively by the relay send path, which is out of scope for this
  feature (G-3). Nothing in this module claims a send occurred.
- **No persistence yet.** In-memory singleton, house style (``attention``,
  ``waiting_on``, ``email``): the API and the tests work without
  ``NERVE_DB_URL``; a later persistence adapter is a recorded follow-up.
- **Deterministic fallback is the required path.** The rule-based
  :class:`~backend.drafts.drafter.DeterministicDrafter` always works
  (``model_used == "deterministic"``). A model-assisted drafter is an
  *injectable* protocol implementation; it is invoked only after the R107
  inference policy (``PolicyDecisionPoint.infer_route``) allows the tier it
  needs. Confidential / ``LOCAL_ONLY`` content is never routed remote — on
  any denial the service falls back to the deterministic drafter and
  records the denial in the draft's ``action_history`` with the PDP reason
  and policy id (§37–§39, §71).
- **No plaintext leakage.** Drafts carry ``basis``/``evidence`` as
  *references* (communication refs, thread ids, policy ids, short reasons).
  Raw third-party communication content is never stored on a draft, and
  action-history details are short, secret-free strings.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from backend.drafts.drafter import (
    DeterministicDrafter,
    DraftContext,
    ModelAssistedDrafter,
)
from backend.drafts.models import Draft, DraftAction, DraftStatus
from backend.policy.pdp import INFER_ROUTE_ACTION, PolicyDecisionPoint
from schemas.nc_platform import NCPrincipal

__all__ = [
    "DraftsService",
    "InvalidDraftTransition",
    "DraftNotFound",
    "DraftPolicyDenied",
    "default_drafter_principal",
    "get_drafts_service",
    "reset_drafts_service",
]

# -- errors -------------------------------------------------------------------


class DraftNotFound(KeyError):
    """A draft id does not exist in the store."""

    def __init__(self, draft_id: str) -> None:
        super().__init__(draft_id)
        self.draft_id = draft_id

    def __str__(self) -> str:  # pragma: no cover — repr convenience
        return f"Draft {self.draft_id} not found."


class InvalidDraftTransition(Exception):
    """A draft status transition outside ``DRAFT -> DISCARDED``.

    ``SENT`` is unreachable through the drafts plane; any attempt to move a
    draft to another state (or to re-discard an already-discarded draft)
    raises this.
    """

    def __init__(self, draft_id: str, from_status: str, to_status: str) -> None:
        super().__init__(
            f"Illegal draft transition {from_status} -> {to_status} for {draft_id}."
        )
        self.draft_id = draft_id
        self.from_status = from_status
        self.to_status = to_status


class DraftPolicyDenied(Exception):
    """The R107 inference policy denied the requested inference route.

    Raised by the *low-level* gate helper; the high-level
    :meth:`DraftsService.draft` catches this and falls back to the
    deterministic drafter, recording the denial in the action history.
    """

    def __init__(self, reason: str, policy_id: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.policy_id = policy_id


# -- default PDP principal -----------------------------------------------------


def default_drafter_principal() -> NCPrincipal:
    """The stable human principal the drafts plane uses for INFER_ROUTE.

    Namespace ``personal`` — personal content stays inside the personal
    namespace; a cross-namespace route resource is denied by the PDP's
    namespace-isolation invariant.
    """
    return NCPrincipal(
        id="row-drafts-principal",
        principal_id="prn_" + "0123456789ABCDEFGHJKMNPTWX"[:26],
        kind="human",
        display_name="NerveCenter user",
        namespace="personal",
    )


# -- the service ---------------------------------------------------------------


class DraftsService:
    """Singleton in-memory draft store + policy-gated drafting.

    Public surface:
    - CRUD: :meth:`create`, :meth:`list`, :meth:`get`, :meth:`update`,
      :meth:`discard`.
    - Drafting: :meth:`draft` — the deterministic fallback with an
      optional, policy-gated model-assisted drafter.

    The store is process-wide; :func:`get_drafts_service` returns the
    singleton and :func:`reset_drafts_service` is the test hook that
    replaces it with a fresh, empty service.
    """

    def __init__(
        self,
        *,
        pdp: PolicyDecisionPoint | None = None,
        principal: NCPrincipal | None = None,
        model_assisted: ModelAssistedDrafter | None = None,
        remote_provider_allowed: bool = False,
    ) -> None:
        self._lock = threading.RLock()
        self._drafts: dict[str, Draft] = {}
        self._deterministic = DeterministicDrafter()
        # Policy gate (R107): default-deny PDP — with no policies an
        # inference route to any remote tier is denied; the deterministic
        # path is unaffected (it needs no inference).
        self._pdp: PolicyDecisionPoint = pdp or PolicyDecisionPoint([])
        self._principal: NCPrincipal = principal or default_drafter_principal()
        self._model_assisted: ModelAssistedDrafter | None = model_assisted
        # Transport-level gate: even an ALLOW policy cannot lift a route
        # to remote while the provider is not allowed (fail closed).
        self._remote_provider_allowed = bool(remote_provider_allowed)

    # -- injection hooks (tests / later wiring) --------------------------------

    def set_pdp(self, pdp: PolicyDecisionPoint) -> None:
        """Swap the inference-routing policy decision point."""
        with self._lock:
            self._pdp = pdp

    def set_principal(self, principal: NCPrincipal) -> None:
        """Swap the principal used for INFER_ROUTE decisions."""
        with self._lock:
            self._principal = principal

    def set_model_assisted_drafter(self, drafter: ModelAssistedDrafter | None) -> None:
        """Inject (or remove) the model-assisted drafter."""
        with self._lock:
            self._model_assisted = drafter

    def set_remote_provider_allowed(self, allowed: bool) -> None:
        """Toggle the transport-level remote gate (fail closed by default)."""
        with self._lock:
            self._remote_provider_allowed = bool(allowed)

    # -- CRUD ------------------------------------------------------------------

    def create(
        self,
        *,
        channel: str,
        text: str,
        conversation_id: str | None = None,
        communication_ref: str | None = None,
        basis: Sequence[str] = (),
        evidence: Sequence[str] = (),
        model_used: str = "manual",
        classification: str = "unclassified",
        source: str = "api",
    ) -> Draft:
        """Create a new DRAFT. Only ``DRAFT`` is ever created here — a send
        can never originate from this plane."""
        now = datetime.now(UTC)
        draft_id = f"draft-{uuid4().hex[:12]}"
        draft = Draft(
            draft_id=draft_id,
            channel=channel,
            conversation_id=conversation_id,
            communication_ref=communication_ref,
            text=text,
            basis=list(basis),
            evidence=list(evidence),
            model_used=model_used,
            classification=classification,
            status=DraftStatus.DRAFT,
            source=source,
            action_history=[
                DraftAction(action="created", at=now, detail=f"created via {source}")
            ],
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._drafts[draft_id] = draft
        return draft

    def list(self, status: str | None = None) -> list[Draft]:
        """All drafts, most recently updated first.

        ``status`` filters by lifecycle state (``draft`` / ``discarded`` /
        ``sent``) — listing is read-only, it never mutates anything.
        """
        if status is None:
            items = list(self._drafts.values())
        else:
            key = status.lower().strip()
            items = [d for d in self._drafts.values() if d.status == key]
        return sorted(items, key=lambda d: d.updated_at, reverse=True)

    def get(self, draft_id: str) -> Draft:
        draft = self._drafts.get(draft_id)
        if draft is None:
            raise DraftNotFound(draft_id)
        return draft

    def update(
        self,
        draft_id: str,
        *,
        text: str | None = None,
        basis: Sequence[str] | None = None,
        evidence: Sequence[str] | None = None,
    ) -> Draft:
        """Edit a live draft (text / basis / evidence).

        Human-controlled editing is the point of the plane (§13). Any edit
        on a non-DRAFT draft raises :class:`InvalidDraftTransition` —
        discarded drafts are frozen, and ``SENT`` is unreachable anyway.
        """
        with self._lock:
            draft = self._drafts.get(draft_id)
            if draft is None:
                raise DraftNotFound(draft_id)
            if draft.status != DraftStatus.DRAFT:
                raise InvalidDraftTransition(draft_id, draft.status, "updated")
            now = datetime.now(UTC)
            if text is not None:
                draft.text = text
            if basis is not None:
                draft.basis = list(basis)
            if evidence is not None:
                draft.evidence = list(evidence)
            draft.updated_at = now
            draft.action_history.append(
                DraftAction(action="updated", at=now, detail="edited via api")
            )
            return draft

    def discard(self, draft_id: str, *, reason: str | None = None) -> Draft:
        """The only lifecycle transition the drafts plane performs:
        ``DRAFT -> DISCARDED``. Discarding is idempotent-safe — re-discarding
        is an invalid transition (the draft is already terminal)."""
        with self._lock:
            draft = self._drafts.get(draft_id)
            if draft is None:
                raise DraftNotFound(draft_id)
            if draft.status != DraftStatus.DRAFT:
                raise InvalidDraftTransition(draft_id, draft.status, DraftStatus.DISCARDED)
            now = datetime.now(UTC)
            draft.status = DraftStatus.DISCARDED
            draft.updated_at = now
            draft.discarded_at = now
            draft.action_history.append(
                DraftAction(
                    action="discarded",
                    at=now,
                    detail=reason or "discarded via api",
                )
            )
            return draft

    # -- drafting (deterministic fallback + policy-gated model assist) ---------

    def _infer_route(self, classification: str, context: DraftContext):
        """Ask the R107 PDP how the draft's content may be routed.

        Confidential / personal content is always ``LOCAL_ONLY`` — the PDP
        never routes it remote, no policy can lift it (§36–§37).
        """
        return self._pdp.infer_route(
            self._principal,
            {
                "sensitivity_class": classification,
                "contains_personal_communications": classification
                in ("confidential", "local-only"),
                "remote_provider_allowed": self._remote_provider_allowed,
            },
            action=INFER_ROUTE_ACTION,
        )

    def gate_inference(
        self,
        drafter: ModelAssistedDrafter,
        classification: str,
    ) -> Any:
        """Low-level gate: deny (raise :class:`DraftPolicyDenied`) unless the
        PDP allows the tier the drafter needs.

        - A drafter on a *local* tier needs nothing from the remote: it is
          always allowed (the PDP decision is still recorded for audit).
        - A drafter on the remote tier (``large-reasoning``) requires
          ``remote_allowed=True`` — which for confidential / ``LOCAL_ONLY``
          input is impossible, so personal data can never be routed remote.
        """
        context = DraftContext(
            channel="draft",
            classification=classification,
        )
        decision = self._infer_route(classification, context)
        needs_remote = drafter.required_tier in ("large-reasoning",)
        if needs_remote and not decision.remote_allowed:
            raise DraftPolicyDenied(
                f"remote inference denied for {classification} content: {decision.reason}",
                policy_id=decision.policy_id,
            )
        return decision

    def draft(
        self,
        *,
        channel: str,
        classification: str = "unclassified",
        conversation_id: str | None = None,
        communication_ref: str | None = None,
        inbound_excerpt: str | None = None,
        context: DraftContext | None = None,
        source: str = "rule-based",
    ) -> Draft:
        """Produce a draft: policy-gated model assist, deterministic fallback.

        Order of operations:

        1. If a model-assisted drafter is injected, ask the PDP
           (:meth:`gate_inference`) **before** invoking it.
        2. On denial (or drafter failure), fall back to the deterministic
           drafter — the deterministic path is the required, always-green
           path, and the denial is recorded in ``action_history`` with the
           PDP reason and policy id.
        3. The resulting draft always carries ``basis`` and ``evidence``
           (refs only, no raw content) and never claims a send.
        """
        now = datetime.now(UTC)
        ctx = context or DraftContext(
            channel=channel,
            conversation_id=conversation_id,
            communication_ref=communication_ref,
            inbound_excerpt=inbound_excerpt,
            classification=classification,
        )

        model_assisted: ModelAssistedDrafter | None = None
        with self._lock:
            model_assisted = self._model_assisted

        used: str
        text: str
        basis: list[str] = []
        evidence: list[str] = []
        history: list[DraftAction] = []
        model_used = "deterministic"

        if model_assisted is not None:
            try:
                decision = self.gate_inference(model_assisted, classification)
                needs_remote = model_assisted.required_tier in ("large-reasoning",)
                detail = (
                    f"INFER_ROUTE {decision.reason}"
                    + (f" (policy {decision.policy_id})" if decision.policy_id else "")
                )
                if needs_remote:
                    basis.append(f"model-assisted draft; routed {decision.model_tier}")
                history.append(
                    DraftAction(
                        action="routing_decided",
                        at=now,
                        detail=detail,
                        policy_id=decision.policy_id,
                    )
                )
                text = model_assisted.draft(ctx)
                used = model_assisted.name
                model_used = used
                source = "model-assisted"
                history.append(
                    DraftAction(
                        action="model_assisted",
                        at=now,
                        detail=f"produced by {used}",
                        policy_id=decision.policy_id,
                    )
                )
            except DraftPolicyDenied as denied:
                history.append(
                    DraftAction(
                        action="policy_denied",
                        at=now,
                        detail=denied.reason,
                        policy_id=denied.policy_id,
                    )
                )
                basis.append(f"model-assist denied by policy: {denied.reason}")
                text = self._deterministic.draft(ctx)
                model_used = self._deterministic.name
                history.append(
                    DraftAction(
                        action="fallback",
                        at=now,
                        detail="deterministic fallback after policy denial",
                    )
                )
            except Exception as exc:  # noqa: BLE001 — fail closed to fallback
                history.append(
                    DraftAction(
                        action="model_assist_failed",
                        at=now,
                        detail=f"model-assist raised {type(exc).__name__}; fell back",
                    )
                )
                basis.append("model-assist failed; deterministic fallback used")
                text = self._deterministic.draft(ctx)
                model_used = self._deterministic.name
        else:
            text = self._deterministic.draft(ctx)
            model_used = self._deterministic.name
            basis.append("rule-based deterministic fallback (no model-assist configured)")
            history.append(
                DraftAction(
                    action="fallback",
                    at=now,
                    detail="deterministic drafter (default path)",
                )
            )

        if not text.strip():
            text = self._deterministic.draft(ctx)

        evidence.append("drafts-plane: rule-safe refs only (no raw content)")
        if communication_ref:
            evidence.append(f"communication:{communication_ref}")
        if conversation_id:
            evidence.append(f"conversation:{conversation_id}")

        draft_id = f"draft-{uuid4().hex[:12]}"
        draft = Draft(
            draft_id=draft_id,
            channel=channel,
            conversation_id=conversation_id,
            communication_ref=communication_ref,
            text=text,
            basis=basis,
            evidence=evidence,
            model_used=model_used,
            classification=classification,
            status=DraftStatus.DRAFT,
            source=source,
            action_history=[DraftAction(action="created", at=now, detail=f"drafted via {source}")]
            + history,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._drafts[draft_id] = draft
        return draft


# -- process-wide singleton (mirrors the P0 attention / waiting-on services) ----

_service: DraftsService | None = None
_service_lock = threading.Lock()


def get_drafts_service() -> DraftsService:
    global _service
    with _service_lock:
        if _service is None:
            _service = DraftsService()
        return _service


def reset_drafts_service() -> None:
    """Test hook — replace the singleton with a fresh, empty service."""
    global _service
    with _service_lock:
        _service = DraftsService()
