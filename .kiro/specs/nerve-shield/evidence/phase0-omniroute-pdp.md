# Phase 0.5 — OmniRoute / PDP path, hybrid routes, model identity

**Task:** 0.5 Audit OmniRoute/PDP path, `hybrid/reader`+`hybrid/code` fallback, verify model ids or record UNKNOWN; inference transport + structured-output patterns. **Req:** R-009, R-010, R-017. **Depends:** 0.1.
**Legend:** `[VERIFIED]`; `[INFERRED]`; `[UNKNOWN]`.
**Sources:** `backend/inference/{omniroute,policy,adapter,providers,telemetry}.py`, `backend/policy/{pdp,sensitivity}.py`, `backend/context/ask.py`, `backend/drafts/service.py`, `schemas/routes.py`.

## Summary answer

**`hybrid/reader` and `hybrid/code` DO NOT EXIST in the codebase.** `[VERIFIED]` A repo-wide grep finds them (and the words "Granite"/"Qwen") **only in the NerveShield spec and steering docs — never in `backend/` or `schemas/`.** The installed OmniRoute client resolves routes from an `InferenceRoute(primary_provider, primary_model, hops[])` object and POSTs to `<NERVE_OMNIROUTE_URL>/v1/chat/completions` (OpenAI-compatible). There is **no alias-to-model resolution**, **no local-vs-remote enforcement inside the transport**, and **no pinned Granite/Qwen model identity**. Whether any route reaches a cloud provider is therefore determined entirely by `NERVE_OMNIROUTE_URL` and the (currently non-existent) routing rules — i.e. **`UNKNOWN` at HEAD; the `hybrid/*` alias cannot be assumed local.** The privacy enforcement point that DOES exist is the PDP `INFER_ROUTE` decision keyed on sensitivity class (`LOCAL_ONLY` for `confidential`).

## Model identifiers — recorded UNKNOWN `[VERIFIED grep]`

```
grep -rniE "granite|qwen"            backend/ schemas/  => (no matches)
grep -rnE  "hybrid/(reader|code)…"   backend/ schemas/  => (no matches)
```

- **Granite 4.2 deployed?** `UNKNOWN` — no reference in code. R-009 route is unimplemented.
- **Qwen 27B deployed?** `UNKNOWN` — no reference in code. R-010 route is unimplemented.
- **Exact model ids / artifact digests?** `UNKNOWN` — the schema `modelRun.model_id`/`artifact_digest` fields exist in `security-assessment.schema.json`, but nothing populates them.

## OmniRoute transport — what exists `[VERIFIED]`

`OmniRouteTransport` (`omniroute.py`):
- Config: `NERVE_OMNIROUTE_URL`, `NERVE_OMNIROUTE_API_KEY` (bearer, never logged), `NERVE_OMNIROUTE_FIXTURE[_DIR]` (fixture replay for CI).
- `generate(model, prompt, …)` → POST `/v1/chat/completions` body `{"model", "messages":[{"role":"user","content":prompt}]}`; parses OpenAI `choices`/`usage` or flat `content` envelope. On any failure raises `OmniRouteUnavailableError` (never fabricates). `[VERIFIED]`
- `route_candidates()` → GET `/v1/routes` (spec §32 candidate envelope). `[VERIFIED]`
- **No model whitelist, no provider allowlist, no LOCAL_ONLY check in the transport.** The destination is `NERVE_OMNIROUTE_URL`. `[VERIFIED]`

`OmniRouteAdapter.route(route, request)`: tries `route.primary_provider/primary_model`, then each `route.hops` (provider,model) until one returns; else `OmniRouteUnavailableError(attempted, reasons)`. **The fallback chain can hop to any provider named in `hops` — including a remote one — with no content-policy gate at this layer.** `[VERIFIED]` This is precisely threat T-13.

`InferencePolicyEngine.select_route` (`policy.py`): matches `PolicyRule`s; `_parse_target("provider/model")` splits on `/`. So a target string `"hybrid/reader"` would parse to `provider="hybrid", model="reader"` — **but no such rule or provider client is registered anywhere.** Default when unmatched: `provider="local", model="default"`. `[VERIFIED]`

## The real privacy enforcement point `[VERIFIED]`

`backend/policy/sensitivity.py` + `backend/policy/pdp.py`:
- `classify_sensitivity(content, label, override)` → `SensitivityClass` ∈ {public, internal, confidential}; **`confidential` ⇒ `RoutingDecision.LOCAL_ONLY`**, and `PERSONAL`/`PRIVATE`/`FINANCIAL` labels map to `confidential`.
- Mail ingest hardcodes `"sensitivity": "PERSONAL"` for every message (`mail_worker._process_batch`), so **all mail classifies CONFIDENTIAL → LOCAL_ONLY** by this rule. `[VERIFIED]`
- PDP `InferRouteDecision` (`pdp.py`): `remote_allowed=False` is **unconditionally `LOCAL_ONLY` regardless of policy**; a remote tier requires an explicit matching ALLOW policy; "confidential / `LOCAL_ONLY` input can never be routed remote." `[VERIFIED]`
- `backend/context/ask.py` and `backend/drafts/service.py` already consult this to keep confidential/`LOCAL_ONLY` content off remote routes. `[VERIFIED]` → **reusable enforcement seam for NerveShield (NS-ADR-008).**

**GAP:** the PDP `INFER_ROUTE` decision governs *tier selection*, but the `OmniRouteTransport`/`OmniRouteAdapter` **do not themselves re-check the decision before serialization** — enforcement depends on the caller passing a `LOCAL_ONLY`-derived route and not adding remote `hops`. For NerveShield (R-009.3/R-017/I-04) a **fail-closed resolver that inspects every candidate+fallback hop before the request leaves the process** must be added (tasks 4.2). It does not exist yet.

## Structured-output patterns supported `[VERIFIED]`

- `InferenceRequest`/`InferenceResponse` are Pydantic with `extra="forbid"`. Output is free-text `content`; **there is no JSON-schema-constrained decoding, no grammar/function-calling** in the transport. Semantic-observation validation (R-009.2) must be implemented on top (Phase 4.1 `SemanticObservation` model + validator); the transport returns a string the caller must parse + validate.

## Local-only test plan (for Phase 4.2, documented now)

1. **Fixture-only unit tests** (`NERVE_OMNIROUTE_FIXTURE=1`): no network; assert resolver denies any candidate/hop whose resolved provider is not in a local allowlist.
2. **Cloud-fallback attack test** (T-13): construct an `InferenceRoute` whose `hops` include a remote provider; with a `LOCAL_ONLY` input the resolver must `DENY_BY_POLICY` and emit a policy finding, **zero bytes sent**, `model_runs[].status=DENIED_BY_POLICY` (schema value exists).
3. **Route-candidate inspection test:** `route_candidates()` returns a node/backend list; assert the resolver maps each to a local/remote verdict and refuses on any remote.
4. **Sensitivity binding test:** mail (`PERSONAL`→confidential→`LOCAL_ONLY`) can never produce `remote_allowed=True` from the PDP.
5. **No-secret-logging test:** `OmniRouteUnavailableError` message and telemetry never contain the API key or prompt.

## Route-policy gap list

| Gap | Evidence | Requirement |
| --- | --- | --- |
| `hybrid/reader`/`hybrid/code` unimplemented | no code refs | R-009.1, R-010.1 |
| Granite/Qwen identity unknown | no code refs | R-008.6, R-009 |
| No LOCAL_ONLY enforcement in transport/adapter | `omniroute.py` has no policy check | R-017.1, I-04, T-13 |
| Fallback hops can reach remote providers | `OmniRouteAdapter.route` iterates `hops` unconditionally | T-13 |
| No schema-constrained output decoding | `generate` returns raw string | R-009.2/.4 |
| PDP INFER_ROUTE not re-checked pre-serialization | enforcement is caller-dependent | R-009.3 |

## Done (0.5): local-only test plan + route-policy gap list delivered; model ids recorded UNKNOWN. ✅
