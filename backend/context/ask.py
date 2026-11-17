"""Grounded ask pipeline (T003-04 / T003.AC9, spec gap-closure-t003).

Replaces the honest 202-ACK stub on ``POST /api/context/ask`` with the real
pipeline:

    request → ContextCompiler (bounded, partition-firewalled package)
            → R107 PDP pre-flight (``PolicyDecisionPoint.infer_route``)
            → OmniRoute adapter/transport (ADR-0010; fixture mode in tests)
            → grounded answer with citations + provenance

Honest-degradation contract (house style, spec T003.AC9):

- **Success (200):** ``status="answered"`` — a grounded answer whose
  citations each point at a reference inside the compiled context package
  (R107 firewall: only record references / summaries may cross to the
  remote; no raw personal plaintext in the remote payload), model/route
  metadata, and a deterministic fixture marker when the transport replays
  recorded fixtures.
- **Policy denial (200):** ``status="policy_denied"`` — an explicit,
  structured denial carrying the PDP reason / reason code / policy id.
  Confidential / ``LOCAL_ONLY`` content is never routed remote, and the
  pipeline never fabricates an answer in response to a denial.
- **Genuine unavailability (202):** ``status="unavailable"`` /
  ``request_status="pending"`` — the route was allowed but the OmniRoute
  transport could not be reached (or is not configured). Reason, request
  id, and the sanitized candidate list are returned; **no answer**. This is
  the only remaining use of the ``{accepted, executed:false}`` 202 shape.

Nothing here touches the real network: the transport is injected, and in
fixture mode it replays recorded JSON from a local directory.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import os
import threading
import time
from collections.abc import Iterator

import structlog
from pydantic import BaseModel, Field

from backend.context.compiler import BudgetConfig, ContextCompiler, ContextSource
from backend.inference.omniroute import (
    InferenceRequest,
    OmniRouteAdapter,
    OmniRouteTransport,
    OmniRouteUnavailableError,
)
from backend.agents.registry import get_agent_registry
from backend.policy.pdp import INFER_ROUTE_ACTION, InferRouteDecision, PolicyDecisionPoint
from backend.context.retrieval.policy import kind_for
from backend.context.retrieval.service import RetrievalResult
from schemas.context import ContextPackage
from schemas.nc_platform import NCPrincipal, NCPolicy
from schemas.routes import InferenceRoute

#: Streaming ask patience: a cold or busy model refuses fast while it loads, so
#: the stream retries (before the first token only) instead of giving up.
ASK_STREAM_ATTEMPTS = int(os.environ.get("NERVE_ASK_STREAM_ATTEMPTS", "8") or 8)
ASK_STREAM_PATIENCE_S = float(os.environ.get("NERVE_ASK_STREAM_PATIENCE_S", "240") or 240)
ASK_STREAM_BACKOFF_MAX_S = 30.0
#: Real rejections: retrying cannot help.
_ASK_PERMANENT = (
    "http_400", "http_401", "http_403", "http_404", "http_413", "http_422",
    "no fixture", "not configured",
)


def _ask_reason(exc: BaseException) -> str:
    reasons = getattr(exc, "reasons", None)
    if isinstance(reasons, dict) and reasons:
        return ", ".join(sorted({str(v) for v in reasons.values()}))[:120]
    return type(exc).__name__


def _ask_retryable(exc: BaseException) -> bool:
    reason = _ask_reason(exc).lower()
    return not any(code in reason for code in _ASK_PERMANENT)


def _ask_retry_delay(attempt: int, exc: BaseException) -> float:
    retry_after = getattr(exc, "retry_after", None)
    if isinstance(retry_after, (int, float)) and retry_after > 0:
        return min(float(retry_after), 60.0)
    return min(2.0 ** attempt, ASK_STREAM_BACKOFF_MAX_S)


def _open_stream(transport: Any, model: str, prompt: str) -> Iterator[str]:
    """One attempt: the secure transport's own retries are folded into the
    ask loop so each wait is reported to the user."""
    try:
        return iter(transport.generate_stream(model, prompt, attempts=1))
    except TypeError:  # a transport without the ``attempts`` override
        return iter(transport.generate_stream(model, prompt))

log = structlog.get_logger(__name__)

#: Deterministic fixture marker namespace: reported when the OmniRoute
#: transport replays recorded fixtures instead of the network.
FIXTURE_MARKER = "fixture"

#: Citation markers embedded in model output, e.g. ``[INC-7]``.
_CITATION_RE = re.compile(r"\[([A-Za-z0-9_][A-Za-z0-9_\-]*)\]")


class AskContext(BaseModel):
    """Optional caller-supplied context for an ask (T003-04 AC9/req 1).

    All fields are *references / summaries* (R107 firewall): record ids to
    ground the answer on, and per-record summary text safe to carry to a
    remote model. Raw personal content is never a field of this model.
    """

    model_config = {"extra": "forbid"}

    partition: Literal["engineering", "personal"] | None = Field(
        default=None,
        description="Plane the question compiles against: 'engineering' | 'personal'.",
    )
    sensitivity: Literal["public", "internal", "confidential"] | None = Field(
        default=None,
        description="Sensitivity class for the PDP pre-flight: 'public' | 'internal' | 'confidential'.",
    )
    fixture_refs: list[str] = Field(
        default_factory=list,
        description="Record ids that ground the question (deterministic in fixture mode).",
    )
    fixture_kind: Literal[
        "entities",
        "relationships",
        "evidence",
        "claims",
        "decisions",
        "tasks",
        "goals",
        "agent_results",
        "notes",
    ] | None = Field(
        default=None,
        description="ContextSectionKind for the fixture source (defaults to 'evidence').",
    )
    summaries: dict[str, str] = Field(
        default_factory=dict,
        description="Per-record-id summary text (safe to remote; never raw personal content).",
    )


class AskInference(BaseModel):
    """Explicit per-request inference override (contract §2).

    Both keys are optional; a ``None`` value means "use the value resolved from
    the agent binding (or the service default) for that dimension". An empty
    ``AskInference`` object therefore resolves fully from the agent/default.
    """

    model_config = {"extra": "forbid"}

    provider: str | None = Field(
        default=None,
        description="Override the inference provider for this request.",
    )
    model: str | None = Field(
        default=None,
        description="Override the inference model for this request.",
    )


class AskRequest(BaseModel):
    """Body of ``POST /api/context/ask``.

    ``question`` is intentionally *not* length-validated at the model
    level: the endpoint is the validation authority and returns the
    structured ``422 status="invalid_request"`` body (T003.AC9) for an
    empty or missing question. A pydantic ``min_length`` would pre-empt
    that with FastAPI's generic 422, so the endpoint handler owns the
    rejection (it also treats ``null`` as empty).
    """

    model_config = {"extra": "forbid"}

    question: str | None = Field(
        default=None,
        description="The question to answer (empty/None → 422 invalid_request).",
    )
    context: AskContext | None = Field(
        default=None,
        description="Optional grounding context (partition / sensitivity / fixture refs).",
    )
    agent: str | None = Field(
        default=None,
        description="Optional agent id whose inference binding seeds provider/model (contract §2).",
    )
    inference: AskInference | None = Field(
        default=None,
        description="Explicit per-request inference override; highest priority (contract §2).",
    )
    account_id: str | None = Field(
        default=None,
        description="Calendar account chosen in the UI for a calendar request (overrides the default).",
    )
    session_id: str | None = Field(
        default=None,
        description="Optional client conversation id (echoed in the stream meta event).",
    )


def default_ask_principal() -> NCPrincipal:
    """Stable human principal the ask pipeline presents to the R107 PDP."""
    return NCPrincipal(
        id="row-ask-principal",
        principal_id="prn_" + "0123456789ABCDEFGHJKMNPTWX"[:26],
        kind="human",
        display_name="NerveCenter user",
        namespace="personal",
    )


#: Stable id for the built-in ask allow policy (deterministic, not per-request).
_ASK_ALLOW_POLICY_ID = "pol_" + "0123456789ABCDEFGHJKMNPTWX"


def default_ask_pdp() -> PolicyDecisionPoint:
    """Default-deny PDP pre-seeded with an explicit ALLOW for the ask route.

    The ask pipeline previously ran against a bare :class:`PolicyDecisionPoint`
    (no policies) — i.e. **default-deny for everything** — so even the
    benign, public-facing question ``hello world`` (sensitivity
    ``internal``) was refused with the structured ``policy_denied`` answer.

    This factory wires the explicit, scoped ALLOW policy that restores the
    intended behavior without lifting the honest-degradation contract:

    - ``public`` / ``internal`` (remote-capable) tiers are now explicitly
      **allowed** for the ask action, so a benign question is no longer
      policy-denied. If the route is allowed but the inference transport is
      not configured/unreachable, the pipeline still returns the honest
      202 ``unavailable`` shape — never a fabricated answer.
    - ``confidential`` / ``LOCAL_ONLY`` content is **still denied**: the PDP
      applies the hard floor at ``sensitivity_class is confidential`` before
      any policy matching, so this ALLOW policy cannot lift it to a remote
      tier. Denial precedence and namespace isolation remain intact.

    The ``*`` principal wildcard is safe here: the PDP never expands a
    wildcard to ``kind:agent`` / ``kind:function`` principals (ADR-0003
    no-wildcard-privilege), and the ask principal is the stable
    ``kind=human`` principal above.
    """
    allow_policy = NCPolicy(
        id="row-ask-allow",
        policy_id=_ASK_ALLOW_POLICY_ID,
        effect="ALLOW",
        principal_match="*",
        action_match=INFER_ROUTE_ACTION,
        resource_match="*",
        condition=None,
        priority=10,
    )
    return PolicyDecisionPoint([allow_policy])


def context_ref_uri(record_id: str, package_id: str | None = None) -> str:
    """Stable reference URI for a context record (citations point here)."""
    return f"nc://personal/context/{package_id or 'package'}/records/{record_id}"


def _stable_marker(model: str, content: str) -> str:
    """Deterministic marker for fixture replayed answers (same input → same marker)."""
    return hashlib.sha256(f"{model}\n{content}".encode("utf-8")).hexdigest()[:16]


def _build_sources(
    question: str,
    ctx: AskContext | None,
    request_id: str,
    retrieval: RetrievalResult | None = None,
) -> list[ContextSource]:
    """Build the compiler sources for an ask.

    Retrieved records become one source per section kind, each carrying the
    real record ids as ``refs`` (R107: references and bounded summaries only).
    Client-supplied ``fixture_refs`` keep working. With no refs at all there
    is **no source**, so the compiler never invents placeholder ids.
    """
    sources: list[ContextSource] = []
    if retrieval is not None and retrieval.records:
        by_kind: dict[str, list[str]] = {}
        for rec in retrieval.records:
            by_kind.setdefault(kind_for(rec.domain), []).append(rec.id)
        for kind, ids in sorted(by_kind.items()):
            sources.append(
                ContextSource(
                    id=f"ask-{request_id}-{kind}",
                    kind=kind,  # type: ignore[arg-type]
                    query={"question": question, "refs": ids},
                    relevance_score=1.0,
                    partition="personal",
                    summary=None,
                )
            )
    refs = sorted({rid for rid in (ctx.fixture_refs if ctx else []) if rid})
    if refs:
        kind = ctx.fixture_kind if ctx and ctx.fixture_kind else "evidence"
        summaries = {rid: ((ctx.summaries.get(rid) if ctx else None) or rid) for rid in refs}
        sources.append(
            ContextSource(
                id=f"ask-{request_id}",
                kind=kind,  # type: ignore[arg-type]
                query={"question": question, "refs": refs},
                relevance_score=1.0,
                partition=(ctx.partition if ctx and ctx.partition else "engineering"),
                summary="; ".join(summaries.values()),
            )
        )
    return sources


def _package_refs(package: ContextPackage) -> list[str]:
    """All record ids referenced by the compiled package (deterministic order)."""
    return [rid for section in package.sections for rid in section.record_ids]


def _grounded_citations(
    content: str,
    package: ContextPackage,
) -> tuple[list[dict[str, Any]], list[str], bool]:
    """Parse ``[REF]`` citation markers and verify each against the package.

    Returns ``(citations, ungrounded, grounded)``. Every returned citation
    points at a record id that is actually present in the compiled context
    package — an ungrounded marker is dropped from ``citations`` and listed
    in ``ungrounded`` instead of being reported as evidence (no ungrounded
    claim is presented).
    """
    refs: dict[str, str] = {}
    for section in package.sections:
        for rid in section.record_ids:
            refs.setdefault(rid, section.kind)

    seen: list[str] = []
    ungrounded: list[str] = []
    for marker in _CITATION_RE.findall(content or ""):
        if marker in refs:
            if marker not in seen:
                seen.append(marker)
        elif marker not in ungrounded:
            ungrounded.append(marker)

    citations = [
        {
            "record_id": rid,
            "ref": context_ref_uri(rid, package.id),
            "kind": refs[rid],
        }
        for rid in seen
    ]
    grounded = bool(citations) and not ungrounded
    return citations, ungrounded, grounded


@dataclass
class AskOutcome:
    """Internal result of one ask run (status code + response body)."""

    status_code: int
    body: dict[str, Any]


class AskService:
    """Compiler → R107 PDP pre-flight → OmniRoute → grounded answer.

    The transport is always injected (never constructed from network
    config here); in fixture mode it replays recorded JSON from a local
    directory, so the whole pipeline is deterministic and offline.
    """

    def __init__(
        self,
        *,
        compiler: ContextCompiler | None = None,
        pdp: PolicyDecisionPoint | None = None,
        principal: NCPrincipal | None = None,
        transport: OmniRouteTransport | None = None,
        remote_provider_allowed: bool = False,
        model: str = "nerve-ask",
        provider: str = "omniroute",
        token_budget: int = 8000,
    ) -> None:
        self._compiler = compiler or ContextCompiler()
        # Default PDP: explicit ALLOW for benign (public/internal) tiers,
        # still default-deny for confidential / LOCAL_ONLY content. A denied
        # route yields the structured policy-denied answer; an allowed route
        # with an unavailable transport yields the honest 202 — never a
        # fabricated answer.
        self._pdp = pdp if pdp is not None else default_ask_pdp()
        self._principal = principal or default_ask_principal()
        self._transport = transport
        self._sleep = time.sleep  # tests replace this
        self._remote_provider_allowed = remote_provider_allowed
        self._model = model
        self._provider = provider
        self._budget = BudgetConfig(total_tokens=token_budget)

    # -- inference resolution ------------------------------------------------

    def _resolve_inference(
        self,
        agent: str | None,
        inference: AskInference | None,
    ) -> dict[str, str]:
        """Resolve the primary provider/model for one ask (contract §2 order).

        Order: (1) the service-level default; (2) the binding for ``agent``
        (native registry type store, else the persisted-instance binding the
        API layer mirrored into the registry); (3) the request ``inference``
        override (applied last and wins). A partial override keeps the other
        dimension from the winning step. Returns the audit echo:
        ``{"provider", "model", "source"}`` where ``source`` is
        ``"default" | "agent" | "request"`` (request only when the request
        override supplied at least one dimension).
        """
        provider: str = self._provider
        model: str = self._model
        source = "default"

        if agent is not None and agent != "":
            # Persisted-instance binding first (mirrored from the ``inference``
            # jsonb column by the API layer); fall back to the native-type
            # manifest binding (set via ``AgentRegistry.set_inference``).
            binding = get_agent_registry().agent_binding(agent)
            if binding is None:
                binding = get_agent_registry().inference(agent)
            if binding:
                if binding.get("provider"):
                    provider = binding["provider"]
                if binding.get("model"):
                    model = binding["model"]
                source = "agent"

        if inference is not None:
            if inference.provider is not None and inference.provider != "":
                provider = inference.provider
            if inference.model is not None and inference.model != "":
                model = inference.model
            if inference.provider is not None or inference.model is not None:
                source = "request"

        return {"provider": provider, "model": model, "source": source}

    # -- transport ---------------------------------------------------------

    @property
    def transport_available(self) -> bool:
        """Whether the inference transport can serve a remote hop at all.

        Fixture mode is always available (local replay). Live mode requires
        a configured base URL — an unconfigured deployment is genuinely
        unavailable and degrades to the honest 202.
        """
        if self._transport is None:
            return False
        if self._transport.fixture_mode:
            return True
        return bool((self._transport.base_url or "").strip())

    # -- context -------------------------------------------------------------

    @property
    def personal_allowed(self) -> bool:
        """Personal records may be read only when the transport is the secure
        local combo (NS-ADR-108): it has ``check``."""
        return self._transport is not None and hasattr(self._transport, "check")

    def _compile(
        self,
        question: str,
        ctx: AskContext | None,
        request_id: str,
        retrieval: RetrievalResult | None = None,
    ) -> ContextPackage:
        sources = _build_sources(question, ctx, request_id, retrieval)
        personal = bool(
            (ctx and ctx.partition == "personal") or (retrieval and retrieval.records)
        )
        return self._compiler.compile(
            purpose="inference",
            task_id=f"ask-{request_id}",
            sources=sources,
            budget=self._budget,
            personal=personal,
        )

    # -- policy --------------------------------------------------------------

    def _preflight(
        self, ctx: AskContext | None, personal: bool = False
    ) -> InferRouteDecision:
        """Run the R107 PDP pre-flight **before** any inference (req 2)."""
        sensitivity = (ctx.sensitivity if ctx and ctx.sensitivity else None) or "internal"
        return self._pdp.infer_route(
            self._principal,
            {
                "sensitivity_class": sensitivity,
                # Retrieved personal records make this true: the PDP then
                # returns a local tier, and Ask proceeds only on the secure
                # local transport (``personal_allowed``).
                "contains_personal_communications": personal,
                "remote_provider_allowed": self._remote_provider_allowed,
            },
            action=INFER_ROUTE_ACTION,
            simulate=True,
        )

    @staticmethod
    def _denial_body(
        request_id: str,
        decision: InferRouteDecision,
        package: ContextPackage,
    ) -> AskOutcome:
        """Structured policy-denied answer — never a fabricated one (req 2)."""
        body = {
            "accepted": True,
            "executed": False,
            "service": "context",
            "request_id": request_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "status": "policy_denied",
            "request_status": "denied",
            "answer": (
                "This question cannot be answered here: the inference policy "
                "denies the requested processing for its sensitivity class. "
                "No answer is fabricated in response to a policy denial."
            ),
            "grounded": False,
            "citations": [],
            "policy": {
                "denied": True,
                "reason": decision.reason,
                "reason_code": "infer_route_denied",
                "policy_id": decision.policy_id,
                "sensitivity_class": decision.sensitivity_class,
                "remote_allowed": decision.remote_allowed,
                "model_tier": decision.model_tier,
            },
            "provenance": {
                "context_package_id": package.id,
                "context_records": _package_refs(package),
                "policy_decision": {
                    "model_tier": decision.model_tier,
                    "remote_allowed": decision.remote_allowed,
                    "policy_id": decision.policy_id,
                    "reason": decision.reason,
                    "sensitivity_class": decision.sensitivity_class,
                },
            },
            "result": None,
        }
        return AskOutcome(200, body)

    # -- inference -----------------------------------------------------------

    def _remote_payload(
        self,
        question: str,
        ctx: AskContext | None,
        evidence: list[str],
        retrieval: RetrievalResult | None = None,
    ) -> dict[str, Any]:
        """The question plus the evidence the model may cite. Retrieved records
        carry real title, date and a short scrubbed snippet; client-supplied
        refs carry the caller's summaries. Never a bare id as its own summary."""
        records = retrieval.by_id() if retrieval is not None else {}
        items: list[dict[str, Any]] = []
        for rid in evidence:
            rec = records.get(rid)
            if rec is not None:
                parts = [rec.title, rec.when or "", rec.snippet]
                items.append({
                    "id": rid,
                    "summary": " | ".join(p for p in parts if p),
                    "domain": rec.domain,
                })
            else:
                summary = (ctx.summaries.get(rid) if ctx else None) or ""
                items.append({"id": rid, "summary": summary})
        return {
            "question": question,
            "partition": (ctx.partition if ctx and ctx.partition else "engineering"),
            "sensitivity": (ctx.sensitivity if ctx and ctx.sensitivity else "internal"),
            "evidence": items,
        }

    @staticmethod
    def _prompt(question: str, payload: dict[str, Any]) -> str:
        ev = payload["evidence"]
        lines = [
            "Answer strictly from the records below.",
            "Cite each claim as [REF] where REF is a record id from the list; never invent a REF.",
            "If the records do not contain the answer, say so plainly instead of guessing.",
            f"Question: {question}",
        ]
        if not ev:
            lines.append("No records were found for this question.")
        for item in ev:
            lines.append(f"REF {item['id']}: {item['summary']}")
        return "\n".join(lines)

    def _unavailable_body(
        self,
        request_id: str,
        exc: OmniRouteUnavailableError,
        package: ContextPackage,
    ) -> AskOutcome:
        """Genuine transport unavailability → honest 202, no answer (req 4)."""
        attempted = [f"{p}/{m}" for p, m in exc.attempted]
        body = {
            "accepted": True,
            "executed": False,
            "service": "context",
            "request_id": request_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "status": "unavailable",
            "request_status": "pending",
            "reason": (
                "inference transport unavailable; the question is accepted "
                "and will be answered when the route is reachable — no "
                "answer is fabricated for an unavailable transport"
            ),
            "attempted": attempted,
            "error": str(exc),
            "provenance": {
                "context_package_id": package.id,
                "context_records": _package_refs(package),
            },
            "answer": None,
            "grounded": None,
            "citations": [],
            "result": None,
        }
        return AskOutcome(202, body)

    # -- pipeline ------------------------------------------------------------

    def ask(
        self,
        request: AskRequest,
        *,
        calendar: Any = None,
        retrieval: RetrievalResult | None = None,
    ) -> AskOutcome:
        """Run the full grounded ask pipeline (offline under fixture mode).

        Order of operations (T003.AC9):
        1. Compile the grounded context package (existing compiler, R107 firewall).
        2. Run the R107 PDP pre-flight. A denial is a structured
           ``policy_denied`` answer — no inference is ever attempted.
        3. Route the reference-only payload through the OmniRoute
           adapter/transport. ``OmniRouteUnavailableError`` (or an
           unconfigured transport) → the honest 202 unavailable shape.
        4. On success, verify every citation against the compiled package
           and return the grounded answer with provenance.
        """
        question = (request.question or "").strip()
        if not question:
            # Empty / missing question → structured 422, no inference (AC9).
            return AskOutcome(
                422,
                {
                    "accepted": False,
                    "executed": False,
                    "service": "context",
                    "request_id": str(uuid4()),
                    "timestamp": datetime.now(UTC).isoformat(),
                    "status": "invalid_request",
                    "inference": self._resolve_inference(
                        request.agent, request.inference
                    ),
                    "detail": "'question' is required and must be non-empty.",
                    "answer": None,
                },
            )
        request_id = str(uuid4())
        ctx = request.context
        # Contract §2: resolve the primary provider/model *before* any inference.
        # The audit echo is injected into every response shape (answered /
        # policy_denied / unavailable / invalid_request) — "source" records which
        # level won (default | agent | request).
        audit = self._resolve_inference(request.agent, request.inference)
        provider = audit["provider"]
        model = audit["model"]
        retrieval = self._usable_retrieval(retrieval)
        personal = bool(retrieval and retrieval.records)
        package = self._compile(question, ctx, request_id, retrieval)
        decision = self._preflight(ctx, personal)

        # R107 pre-flight: a denial is a structured policy-denied answer —
        # no inference is ever attempted, nothing is fabricated. Personal
        # records are allowed only on the secure local transport.
        if not decision.remote_allowed and not (personal and self.personal_allowed):
            outcome = self._denial_body(request_id, decision, package)
            outcome.body["inference"] = audit
            return outcome

        if (missing := self._not_found_outcome(request_id, question, audit, decision, package, retrieval)):
            return missing

        if not self.transport_available:
            exc = OmniRouteUnavailableError(
                [(provider, model)],
                {(provider, model): "no transport configured"},
            )
            outcome = self._unavailable_body(request_id, exc, package)
            outcome.body["inference"] = audit
            return outcome

        calendar_outcome = self._calendar_outcome(
            request_id=request_id,
            question=question,
            model=model,
            audit=audit,
            decision=decision,
            package=package,
            calendar=calendar,
        )
        if calendar_outcome is not None:
            return calendar_outcome

        evidence = _package_refs(package)
        payload = self._remote_payload(question, ctx, evidence, retrieval)
        prompt = self._prompt(question, payload)

        route = InferenceRoute(
            id=f"route-{request_id}",
            request_key=f"ask-{request_id}",
            primary_model=model,
            primary_provider=provider,
            strategy="policy",
        )
        req = InferenceRequest(
            route_key=route.request_key,
            prompt=prompt,
            parameters={"grounded": True, "evidence_ids": list(evidence)},
        )

        adapter = OmniRouteAdapter()
        if self._transport is not None:
            adapter.add_provider(provider, self._transport)

        try:
            response = adapter.route(route, req)
        except OmniRouteUnavailableError as exc:
            outcome = self._unavailable_body(request_id, exc, package)
            outcome.body["inference"] = audit
            return outcome

        outcome = self._answered_outcome(
            request_id=request_id,
            question=question,
            content=response.content,
            model_name=response.model,
            provider_name=response.provider,
            usage={
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "ttft_ms": response.ttft_ms,
                "e2e_latency_ms": response.e2e_latency_ms,
            },
            audit=audit,
            request_key=route.request_key,
            decision=decision,
            package=package,
            evidence=evidence,
            retrieval=retrieval,
        )
        return outcome

    def _usable_retrieval(self, retrieval: RetrievalResult | None) -> RetrievalResult | None:
        """Personal records never reach a transport that is not the secure route."""
        if retrieval is not None and retrieval.records and not self.personal_allowed:
            return None
        return retrieval

    def _not_found_outcome(
        self,
        request_id: str,
        question: str,
        audit: dict[str, str],
        decision: InferRouteDecision,
        package: ContextPackage,
        retrieval: RetrievalResult | None,
    ) -> AskOutcome | None:
        """A lookup that matched nothing is answered in code: no model call,
        no citations, and a plain statement of what was searched."""
        if retrieval is None or retrieval.records:
            return None
        intent = retrieval.intent
        if not (intent.lookup_shaped and intent.has_terms):
            return None
        subject = ", ".join(intent.search_terms[:4])
        where = ", ".join(d.replace("_", " ") for d in retrieval.searched) or "any connected source"
        answer = f"I found nothing about {subject} in {where}."
        if retrieval.skipped:
            answer += " I could not search " + ", ".join(
                d.replace("_", " ") for d in retrieval.skipped) + " just now."
        body: dict[str, Any] = {
            "accepted": True,
            "executed": True,
            "service": "context",
            "request_id": request_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "status": "answered",
            "request_status": "answered",
            "answer": answer,
            "grounded": False,
            "citations": [],
            "records": [],
            "ungrounded_claims": [],
            "inference": audit,
            "policy": {
                "denied": False,
                "model_tier": decision.model_tier,
                "remote_allowed": True,
                "policy_id": decision.policy_id,
                "sensitivity_class": decision.sensitivity_class,
            },
            "provenance": {
                "context_package_id": package.id,
                "context_records": [],
                "searched": list(retrieval.searched),
            },
            "result": None,
        }
        return AskOutcome(200, body)

    def ask_stream(
        self,
        request: AskRequest,
        *,
        calendar: Any = None,
        retrieval: RetrievalResult | None = None,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        """Streaming twin of :meth:`ask`: yields ``(event, data)`` pairs.

        Events: ``meta`` {request_id}, ``delta`` {text}, ``proposal`` {...},
        ``done`` {the same body ``ask`` returns} and ``error`` {code, message}.
        Compile, PDP, denial and unavailable handling are shared with ``ask``;
        the secure route guard runs inside ``transport.generate_stream`` for
        every streamed ask, and citations are verified once the stream ends.
        """
        from backend.inference.secure_client import SecureRouteDenied

        question = (request.question or "").strip()
        request_id = str(uuid4())
        yield "meta", {"request_id": request_id, "session_id": request.session_id}
        if not question:
            yield "error", {
                "code": "invalid_request",
                "message": "'question' is required and must be non-empty.",
            }
            return
        ctx = request.context
        audit = self._resolve_inference(request.agent, request.inference)
        provider, model = audit["provider"], audit["model"]
        retrieval = self._usable_retrieval(retrieval)
        personal = bool(retrieval and retrieval.records)
        package = self._compile(question, ctx, request_id, retrieval)
        decision = self._preflight(ctx, personal)

        if not decision.remote_allowed and not (personal and self.personal_allowed):
            outcome = self._denial_body(request_id, decision, package)
            outcome.body["inference"] = audit
            yield "done", outcome.body
            return
        missing = self._not_found_outcome(request_id, question, audit, decision, package, retrieval)
        if missing is not None:
            yield "delta", {"text": missing.body["answer"]}
            yield "done", missing.body
            return
        if not self.transport_available:
            exc = OmniRouteUnavailableError(
                [(provider, model)], {(provider, model): "no transport configured"}
            )
            outcome = self._unavailable_body(request_id, exc, package)
            outcome.body["inference"] = audit
            yield "done", outcome.body
            return

        try:
            action = self._calendar_outcome(
                request_id=request_id,
                question=question,
                model=model,
                audit=audit,
                decision=decision,
                package=package,
                calendar=calendar,
            )
        except SecureRouteDenied as exc:
            yield "error", {"code": "secure_route_denied", "message": exc.reason}
            return
        if action is not None:
            yield "delta", {"text": action.body["answer"]}
            if "proposal" in action.body:
                yield "proposal", action.body["proposal"]
            if "delete_proposal" in action.body:
                yield "delete_proposal", action.body["delete_proposal"]
            yield "done", action.body
            return

        evidence = _package_refs(package)
        prompt = self._prompt(
            question, self._remote_payload(question, ctx, evidence, retrieval)
        )
        parts: list[str] = []
        started = time.monotonic()
        try:
            transport = self._transport
            if hasattr(transport, "generate_stream"):
                deadline = started + ASK_STREAM_PATIENCE_S
                attempt = 0
                while True:
                    attempt += 1
                    try:
                        for chunk in _open_stream(transport, model, prompt):
                            parts.append(chunk)
                            yield "delta", {"text": chunk}
                        if not "".join(parts).strip():
                            raise OmniRouteUnavailableError(
                                [(provider, model)], {(provider, model): "empty"}
                            )
                        break
                    except OmniRouteUnavailableError as exc:
                        if "".join(parts).strip():
                            raise  # partial answer: never retried (would duplicate text)
                        parts.clear()
                        delay = _ask_retry_delay(attempt, exc)
                        if (
                            not _ask_retryable(exc)
                            or attempt >= ASK_STREAM_ATTEMPTS
                            or time.monotonic() + delay > deadline
                        ):
                            raise
                        reason = _ask_reason(exc)
                        log.info("ask.stream_retry", attempt=attempt, reason=reason, delay_s=delay)
                        yield "status", {
                            "code": "retrying",
                            "attempt": attempt,
                            "max_attempts": ASK_STREAM_ATTEMPTS,
                            "retry_in_s": round(delay, 1),
                            "reason": reason,
                            "message": (
                                f"The model is busy or still starting ({reason}). "
                                f"Retrying in {round(delay)} s "
                                f"(attempt {attempt + 1} of {ASK_STREAM_ATTEMPTS})."
                            ),
                        }
                        self._sleep(delay)
            else:
                text = str(transport.generate(model, prompt).get("content", ""))
                parts.append(text)
                if text:
                    yield "delta", {"text": text}
            if not "".join(parts).strip():
                raise OmniRouteUnavailableError([(provider, model)], {(provider, model): "empty"})
        except SecureRouteDenied as exc:
            yield "error", {"code": "secure_route_denied", "message": exc.reason}
            return
        except OmniRouteUnavailableError as exc:
            if parts and "".join(parts).strip():
                yield "error", {
                    "code": "stream_interrupted",
                    "message": "The answer stopped before it finished.",
                }
                return
            outcome = self._unavailable_body(request_id, exc, package)
            outcome.body["inference"] = audit
            yield "done", outcome.body
            return
        except Exception as exc:  # noqa: BLE001 - one safe error event, no detail
            log.warning("ask.stream_failed", error=type(exc).__name__)
            yield "error", {"code": "ask_failed", "message": "The answer could not be produced."}
            return

        outcome = self._answered_outcome(
            request_id=request_id,
            question=question,
            content="".join(parts),
            model_name=model,
            provider_name=provider,
            usage={
                "input_tokens": 0,
                "output_tokens": 0,
                "ttft_ms": None,
                "e2e_latency_ms": (time.monotonic() - started) * 1000.0,
            },
            audit=audit,
            request_key=f"ask-{request_id}",
            decision=decision,
            package=package,
            evidence=evidence,
            retrieval=retrieval,
        )
        yield "done", outcome.body

    def _answered_outcome(
        self,
        *,
        request_id: str,
        question: str,
        content: str,
        model_name: str,
        provider_name: str,
        usage: dict[str, Any],
        audit: dict[str, str],
        request_key: str,
        decision: InferRouteDecision,
        package: ContextPackage,
        evidence: list[str],
        retrieval: RetrievalResult | None = None,
    ) -> AskOutcome:
        """The 200 ``answered`` body (shared by ``ask`` and ``ask_stream``)."""
        citations, ungrounded, grounded = _grounded_citations(content, package)
        found = retrieval.by_id() if retrieval is not None else {}
        cited_records: list[dict[str, Any]] = []
        for cite in citations:
            rec = found.get(cite["record_id"])
            if rec is not None:
                cite.update(title=rec.title, domain=rec.domain, link=rec.link, when=rec.when)
                cited_records.append(rec.public())
        fixture = bool(self._transport and self._transport.fixture_mode)
        body: dict[str, Any] = {
            "accepted": True,
            "executed": True,
            "service": "context",
            "request_id": request_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "status": "answered",
            "request_status": "answered",
            "answer": content,
            "grounded": grounded,
            "citations": citations,
            "records": cited_records,
            "ungrounded_claims": ungrounded,
            "model": model_name,
            "provider": provider_name,
            "inference": audit,
            "route": {
                "model": self._model,
                "provider": self._provider,
                "request_key": request_key,
            },
            "usage": usage,
            "fixture_marker": _stable_marker(model_name, content) if fixture else None,
            "policy": {
                "denied": False,
                "model_tier": decision.model_tier,
                "remote_allowed": True,
                "policy_id": decision.policy_id,
                "sensitivity_class": decision.sensitivity_class,
            },
            "provenance": {
                "context_package_id": package.id,
                "context_records": evidence,
                "policy_decision": {
                    "model_tier": decision.model_tier,
                    "remote_allowed": decision.remote_allowed,
                    "policy_id": decision.policy_id,
                    "reason": decision.reason,
                    "sensitivity_class": decision.sensitivity_class,
                },
            },
            "result": None,
        }
        if fixture:
            # Best-effort linkage to a synthetic-lab ask case by question
            # (deterministic; the corpus is read-only here — T003.AC10 owns
            # the corpus and this only annotates provenance).
            self._annotate_synthetic_case(question, body)
        return AskOutcome(200, body)

    def _calendar_outcome(
        self,
        *,
        request_id: str,
        question: str,
        model: str,
        audit: dict[str, str],
        decision: InferRouteDecision,
        package: ContextPackage,
        calendar: Any,
    ) -> AskOutcome | None:
        """Calendar-create action (kept out of the grounded citation flow).

        ``None`` falls through to the normal grounded answer: not a calendar
        request, extraction failed, or no calendar context is available.
        """
        from backend.context import ask_actions, ask_delete

        if calendar is None:
            return None
        if ask_delete.looks_like_calendar_delete(question):
            action = ask_delete.propose_calendar_delete(self._transport, model, question, calendar)
        elif ask_actions.looks_like_calendar_create(question):
            action = ask_actions.propose_calendar_event(self._transport, model, question, calendar)
        else:
            return None
        if action is None:
            return None
        status = action["status"]
        body: dict[str, Any] = {
            "accepted": True,
            "executed": status == "created",
            "service": "context",
            "request_id": request_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "status": status,
            "request_status": "proposed" if status in ("proposal", "delete_proposal") else "answered",
            "answer": action["answer"],
            "grounded": False,
            "citations": [],
            "inference": audit,
            "policy": {
                "denied": False,
                "model_tier": decision.model_tier,
                "remote_allowed": True,
                "policy_id": decision.policy_id,
                "sensitivity_class": decision.sensitivity_class,
            },
            "provenance": {
                "context_package_id": package.id,
                "context_records": [],
            },
            "result": None,
        }
        for key in ("proposal", "delete_proposal", "event", "undo", "conflicts", "warnings",
                    "assumptions"):
            if key in action:
                body[key] = action[key]
        return AskOutcome(200, body)

    @staticmethod
    def _annotate_synthetic_case(question: str, body: dict[str, Any]) -> None:
        """Best-effort link to the synthetic-lab ask case for this question."""
        try:
            corpus = Path(__file__).resolve().parents[2] / "synthetic-lab" / "corpus" / "corpus.json"
            if not corpus.is_file():
                return
            data = json.loads(corpus.read_text(encoding="utf-8"))
            for case in data.get("cases", []):
                if case.get("plane") == "ask" and case.get("question") == question:
                    body.setdefault("provenance", {})["synthetic_lab_case"] = case.get("id")
                    break
        except Exception:  # noqa: BLE001 — annotation is best-effort
            log.warning("synthetic_lab_case_annotation_failed")


# -- process-wide service (startup wiring hook) ---------------------------------

_ask_service_lock = threading.Lock()
_ask_service: AskService | None = None


def get_ask_service() -> AskService:
    """The process-wide ask service (constructed lazily).

    Startup wiring may replace it via :func:`set_ask_service` with a
    service carrying the deployed transport (``OmniRouteTransport.from_env``)
    and the transport-level remote-permission gate.
    """
    global _ask_service
    with _ask_service_lock:
        if _ask_service is None:
            # The default service carries the explicit benign-tier ALLOW policy
            # (:func:`default_ask_pdp`) and opens the ``remote_provider_allowed``
            # transport gate so that the *policy* — not the transport gate — is
            # what decides routing. Confidential / LOCAL_ONLY content is still
            # denied by the PDP hard floor before policy matching. When the
            # route is allowed but no transport is configured, the pipeline
            # degrades to the honest 202 ``unavailable`` shape (transport
            # unavailability), never a fabricated answer.
            _ask_service = AskService(remote_provider_allowed=True)
        return _ask_service


def set_ask_service(service: AskService | None) -> None:
    """Replace the process-wide ask service (startup wiring / tests)."""
    global _ask_service
    with _ask_service_lock:
        _ask_service = service


def reset_ask_service() -> None:
    """Test hook — drop the singleton so it rebuilds lazily."""
    set_ask_service(None)
