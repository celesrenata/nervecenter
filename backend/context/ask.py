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

import structlog
from pydantic import BaseModel, Field

from backend.context.compiler import BudgetConfig, ContextCompiler, ContextSource
from backend.inference.omniroute import (
    InferenceRequest,
    OmniRouteAdapter,
    OmniRouteTransport,
    OmniRouteUnavailableError,
)
from backend.policy.pdp import INFER_ROUTE_ACTION, InferRouteDecision, PolicyDecisionPoint
from schemas.context import ContextPackage
from schemas.nc_platform import NCPrincipal
from schemas.routes import InferenceRoute

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


def default_ask_principal() -> NCPrincipal:
    """Stable human principal the ask pipeline presents to the R107 PDP."""
    return NCPrincipal(
        id="row-ask-principal",
        principal_id="prn_" + "0123456789ABCDEFGHJKMNPTWX"[:26],
        kind="human",
        display_name="NerveCenter user",
        namespace="personal",
    )


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
) -> list[ContextSource]:
    """Build the compiler sources for an ask.

    The fixture source carries *references and summaries only* — the record
    ids ground the answer, and the summary is bounded text the caller already
    deemed safe for the remote plane (R107 firewall).
    """
    kind = (ctx.fixture_kind if ctx and ctx.fixture_kind else "evidence")  # type: ignore[assignment]
    refs = sorted({rid for rid in (ctx.fixture_refs if ctx else []) if rid})
    summaries = {
        rid: ((ctx.summaries.get(rid) if ctx else None) or rid) for rid in refs
    }
    partition = (ctx.partition if ctx and ctx.partition else "engineering")
    return [
        ContextSource(
            id=f"ask-{request_id}",
            kind=kind,
            query={"question": question, "refs": refs},
            relevance_score=1.0,
            partition=partition,
            summary="; ".join(summaries.values()) if summaries else None,
        )
    ]


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
        # Default-deny PDP: with no matching ALLOW policy, remote inference
        # is denied — the structured policy-denied answer is the honest
        # outcome, not a fabricated response.
        self._pdp = pdp if pdp is not None else PolicyDecisionPoint([])
        self._principal = principal or default_ask_principal()
        self._transport = transport
        self._remote_provider_allowed = remote_provider_allowed
        self._model = model
        self._provider = provider
        self._budget = BudgetConfig(total_tokens=token_budget)

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

    def _compile(
        self, question: str, ctx: AskContext | None, request_id: str
    ) -> ContextPackage:
        sources = _build_sources(question, ctx, request_id)
        personal = bool(ctx and ctx.partition == "personal")
        return self._compiler.compile(
            purpose="inference",
            task_id=f"ask-{request_id}",
            sources=sources,
            budget=self._budget,
            personal=personal,
        )

    # -- policy --------------------------------------------------------------

    def _preflight(self, ctx: AskContext | None) -> InferRouteDecision:
        """Run the R107 PDP pre-flight **before** any inference (req 2)."""
        sensitivity = (ctx.sensitivity if ctx and ctx.sensitivity else None) or "internal"
        return self._pdp.infer_route(
            self._principal,
            {
                "sensitivity_class": sensitivity,
                # The remote payload carries references/summaries only —
                # never raw personal communications — so this flag is false
                # by construction.
                "contains_personal_communications": False,
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
    ) -> dict[str, Any]:
        """The only thing that crosses to the remote plane (R107 firewall).

        The question, record *references* and their *summaries* — never raw
        personal plaintext.
        """
        return {
            "question": question,
            "partition": (ctx.partition if ctx and ctx.partition else "engineering"),
            "sensitivity": (ctx.sensitivity if ctx and ctx.sensitivity else "internal"),
            "evidence": [
                {
                    "id": rid,
                    "summary": (ctx.summaries.get(rid) if ctx else None) or rid,
                }
                for rid in evidence
            ],
        }

    @staticmethod
    def _prompt(question: str, payload: dict[str, Any]) -> str:
        ev = payload["evidence"]
        lines = [
            "Answer strictly from the cited evidence below.",
            "Cite each claim as [REF] where REF is an evidence id; never invent a REF.",
            f"Question: {question}",
        ]
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

    def ask(self, request: AskRequest) -> AskOutcome:
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
                    "detail": "'question' is required and must be non-empty.",
                    "answer": None,
                },
            )
        request_id = str(uuid4())
        ctx = request.context
        package = self._compile(question, ctx, request_id)
        decision = self._preflight(ctx)

        # R107 pre-flight: a denial is a structured policy-denied answer —
        # no inference is ever attempted, nothing is fabricated.
        if not decision.remote_allowed:
            return self._denial_body(request_id, decision, package)

        if not self.transport_available:
            exc = OmniRouteUnavailableError(
                [(self._provider, self._model)],
                {(self._provider, self._model): "no transport configured"},
            )
            return self._unavailable_body(request_id, exc, package)

        evidence = _package_refs(package)
        payload = self._remote_payload(question, ctx, evidence)
        prompt = self._prompt(question, payload)

        route = InferenceRoute(
            id=f"route-{request_id}",
            request_key=f"ask-{request_id}",
            primary_model=self._model,
            primary_provider=self._provider,
            strategy="policy",
        )
        req = InferenceRequest(
            route_key=route.request_key,
            prompt=prompt,
            parameters={"grounded": True, "evidence_ids": list(evidence)},
        )

        adapter = OmniRouteAdapter()
        if self._transport is not None:
            adapter.add_provider(self._provider, self._transport)

        try:
            response = adapter.route(route, req)
        except OmniRouteUnavailableError as exc:
            return self._unavailable_body(request_id, exc, package)

        citations, ungrounded, grounded = _grounded_citations(response.content, package)
        fixture = bool(self._transport and self._transport.fixture_mode)
        body = {
            "accepted": True,
            "executed": True,
            "service": "context",
            "request_id": request_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "status": "answered",
            "request_status": "answered",
            "answer": response.content,
            "grounded": grounded,
            "citations": citations,
            "ungrounded_claims": ungrounded,
            "model": response.model,
            "provider": response.provider,
            "route": {
                "model": self._model,
                "provider": self._provider,
                "request_key": route.request_key,
            },
            "usage": {
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "ttft_ms": response.ttft_ms,
                "e2e_latency_ms": response.e2e_latency_ms,
            },
            "fixture_marker": (
                _stable_marker(response.model, response.content) if fixture else None
            ),
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
            _ask_service = AskService()
        return _ask_service


def set_ask_service(service: AskService | None) -> None:
    """Replace the process-wide ask service (startup wiring / tests)."""
    global _ask_service
    with _ask_service_lock:
        _ask_service = service


def reset_ask_service() -> None:
    """Test hook — drop the singleton so it rebuilds lazily."""
    set_ask_service(None)
