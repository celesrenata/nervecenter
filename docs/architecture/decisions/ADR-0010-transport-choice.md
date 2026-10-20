# ADR-0010: Inference transport — HTTP/JSON for v0; gRPC deferred

- **Number:** ADR-0010
- **Title:** Inference transport — HTTP/JSON is the v0 transport; gRPC and NATS-direct are deferred
- **Status:** Proposed
- **Date:** 2026-10-20
- **Spec:** NC-SPEC-2026-010 (C0); T003.AC11 / plan T003-01

## Context

NerveCenter's inference plane (`backend/inference/`) routes model calls through
an external **OmniRoute** service. The routing/telemetry machinery exists —
[`InferencePolicyEngine`](../../../backend/inference/policy.py) builds an
[`InferenceRoute`](../../../schemas/routes.py) with an ordered primary +
fallback chain, and `OmniRouteAdapter.route()` walks that chain — but the
adapter has **no real transport**: its provider clients are whatever objects
a caller injects ([`backend/inference/omniroute.py:94`](../../../backend/inference/omniroute.py),
`_call_provider` only calls `client.generate(...)` on injected objects). No
`httpx`/`aiohttp`/`grpc` client code exists in `backend/inference/` (grep
checked 2026-10-20). T003.AC8 requires this gap to close: the adapter must
reach a real — or recorded-fixture — OmniRoute over a network transport,
with env-configured URL and auth.

T003.AC11 asks for the transport decision itself to be a tracked ADR:
*record HTTP as the v0 transport and defer gRPC*, with rationale,
alternatives, consequences, and a re-decision trigger.

Forces shaping the decision:

- **One principal today.** NerveCenter's sole inference consumer is its own
  API process; the only inference *server* is the external OmniRoute
  deployment. There is no second in-process gRPC consumer.
- **Client surface already covered.** Every existing client — the web SPA,
  the MCP server, the CLI, the mobile relay — talks to NerveCenter over
  HTTP/JSON, WebSocket, or MCP; none of them would consume gRPC directly.
- **OmniRoute is a foreign deployment.** NerveCenter does not own the
  OmniRoute service's wire format; its fabric API is HTTP/JSON (see spec §32
  Route Candidate Interface, JSON envelope —
  [docs/nervcenter-spec.md:1349](../../../docs/nervcenter-spec.md)).
- **Ops parity.** Ingress/TLS/secret delivery for the deployed API
  ([deployments/kubernetes/base/ingress.yaml](../../../deployments/kubernetes/base/ingress.yaml))
  is already HTTP-oriented; adding gRPC means a second port, a second
  health path, and cert handling that no consumer uses.
- **Deterministic testing.** Tests and CI must run without a live OmniRoute
  (spec §5, T003.AC8 "real **or recorded-fixture**"), which pushes toward
  a transport whose responses are trivially recorded as JSON.

## Decision

1. **HTTP/JSON is the v0 inference transport.** NerveCenter's OmniRoute
   client speaks plain HTTP with JSON request/response bodies, an
   `Authorization: Bearer …` header (key from `NERVE_OMNIROUTE_API_KEY`,
   env-only, never logged), and env-configured base URL
   (`NERVE_OMNIROUTE_URL`).
2. **gRPC is deferred.** No `.proto`, no gRPC code, no gRPC dependency is
   written in v0 (T003.AC11). `grpc` is not added to `pyproject.toml`.
3. **NATS-direct is deferred.** Inference requests are point-to-point
   request/response with per-hop timeouts; they do not need the durable
   event-fabric bus (ADR-0002) and would complicate backpressure for no
   current consumer. Telemetry still flows as records
   ([`OmniRouteTelemetry`](../../../schemas/routes.py)) into the routes
   plane, not as raw bus messages.
4. **Fixture mode is part of the transport, not a test-only hack.**
   `NERVE_OMNIROUTE_FIXTURE=1` makes the transport replay recorded fixture
   JSON from a local directory instead of hitting the network, so tests and
   CI are deterministic and no test may touch the real network.
5. **Explicit failure.** If no route candidate in an
   `InferenceRoute` (primary + fallback hops) is reachable, the transport
   raises `OmniRouteUnavailableError`. It never fabricates a response and
   never degrades silently; the injected-client path from before this ADR
   remains the backwards-compatible seam.

## Alternatives considered

### A. gRPC from the start

Typed, streamed, efficient — attractive in the abstract. Rejected for v0:
(a) no gRPC consumer exists on either side — NerveCenter's clients are
HTTP/MCP/WS and OmniRoute's fabric surface is HTTP/JSON, so NerveCenter
would be the *only* gRPC speaker, paying proto versioning, generated-code
drift, and ops cost (second port/TLS/health) for zero consumers;
(b) the single-principal argument (AC11) means the cost is pure carry cost;
(c) a foreign service's wire format is not one we can unilaterally switch.
This is the deferred path, kept warm by the re-decision trigger below.

### B. NATS JetStream direct

The event fabric (ADR-0002) is durable, backpressured, and replayable.
Rejected as an inference *transport*: inference is interactive
request/response with strict latency budgets
(`InferenceRoute.latency_budget_ms`), a NATS request/reply adds a consumer
subscription, message-size limits, and a broker dependency on the hot path;
durable delivery is a liability, not a feature, for a 500 ms chat turn.
NATS remains the fabric for *events* (telemetry/audit), never the inference
request path.

### C. SDK-vendored HTTP clients per provider

Reinstate `OpenAIAdapter`/`AnthropicAdapter`-style per-provider HTTP clients
in-tree. Rejected: the house pattern is *connectors as adapters*
(ADR-0007) — provider credentials stay with OmniRoute, not NerveCenter, so
NerveCenter only ever talks to the single OmniRoute URL. One transport,
one auth path.

## Consequences

- **Streaming.** HTTP/JSON v0 is request/response only: no token streaming,
  no server-side backpressure. `OmniRouteTelemetry.ttft_ms` stays
  *optional* and is populated only when a deployment chooses to report it.
  If a latency/throughput consumer needs streaming (e.g. the ask pipeline
  surfaces tokens live), that is a named consumer → re-decision trigger.
- **Backpressure.** There is no transport-level backpressure; the adapter
  enforces per-request timeouts (`request.timeout_ms` / `default_timeout`)
  and bounded retry via the ordered route. Callers must not fan out
  unbounded concurrent requests; the policy engine already caps routes per
  request.
- **Telemetry still flows.** Every routed attempt (success *or* failure,
  including "all hops exhausted") records an
  [`OmniRouteTelemetry`](../../../schemas/routes.py) sample through the
  existing `InferenceTelemetry` public API (`record_success` /
  `record_error`); nothing in `backend/inference/telemetry.py`'s public
  interface changes. Routes-plane persistence (`models_history`) consumes
  those records per T003.AC8.
- **Secrets.** The API key is env-only and is never logged, never stored on
  any record, and never echoed in `OmniRouteUnavailableError` messages
  (house rule: no-secret-logging, already followed by
  [backend/policy/guardrails.py](../../../backend/policy/guardrails.py) and
  [tools/localstack/probe.py](../../../tools/localstack/probe.py)).
- **Foreign wire format.** The request body is a small stable envelope
  (route hop + prompt + parameters); if OmniRoute's real fabric diverges,
  only the transport module's encoding/decoding functions change — the
  adapter seam and policy engine are untouched.

## Re-decision trigger

Revisit this ADR (and ratify gRPC) **iff** a named consumer appears that
HTTP/JSON cannot serve:

1. a p50 inference latency requirement under ~100 ms round-trip that JSON
   framing + connection pooling measurably miss; or
2. a token-streaming requirement for the ask surface that cannot be met
   with SSE/WebSocket over the existing HTTP ingress; or
3. a second principal that speaks gRPC natively (e.g. an in-cluster
   inference mesh) joining NerveCenter.

Until one of these is *named* (owner + requirement, in a spec or plan row),
the carry cost of gRPC is unjustified and this decision stands.

## References

- [specs/gap-closure-t003/spec.md:189](../../../specs/gap-closure-t003/spec.md) — T003.AC11
- [specs/gap-closure-t003/plan.md:59](../../../specs/gap-closure-t003/plan.md) — plan row T003-01
- [docs/nervcenter-spec.md:1349](../../../docs/nervcenter-spec.md) — §32 Route Candidate Interface (JSON)
- [ADR-0002](./ADR-0002-event-fabric.md) — event fabric (NATS) role
- [ADR-0007](./ADR-0007-connectors-as-adapters.md) — connector/adapter pattern, credentials at the edge
