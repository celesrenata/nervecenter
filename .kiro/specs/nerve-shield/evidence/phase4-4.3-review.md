# Phase 4 task 4.3 review: Granite observer

Verdict: APPROVED (no blockers).

Evidence read: granite_observer.py in full, the route_guard dispatch path, the models.py strictness diff, NS-ADR-029, the observer test list and log-capture test, and the coder's recorded results. I did not re-run the suites.

- Route guard: every attempt goes through dispatch_if_local, which does not call the sender on a deny. ROUTE_DENIED is not retryable. The request is built before the guard but is only handed to the transport inside `send`.
- Minimal evidence and no tools: EvidencePacket enforces hard budgets, strips control and bidi characters and delimiter runs, and rejects over-budget input rather than truncating it. The request has a fixed key set and a build-time assert against tool keys.
- Untrusted output: non-text, oversize, non-JSON, duplicate keys, NaN, non-object, tool-shaped keys at any depth, strict-schema violations, role and source_ref mismatch, and unknown evidence refs all raise typed enum errors. There is no repair and no partial acceptance.
- Retry: capped at 3 (default 2). Only timeout, transport failure and malformed or schema-invalid output are retried. Denials, input errors, tool-shaped output and identity or ref mismatches are not.
- Log and exception safety: exceptions use `from None`, and str(exc) is the enum code. Transport exception text is dropped. The log line carries only outcome, digest, prompt version, attempts and latency. A caplog test asserts that no content, secret or sentinel appears.
- Pipeline isolation: a static test shows no network imports and no other backend module referencing granite_observer. No new dependencies.
- models.py strictness change: it makes the schema fields required and strict, which matches the JSON schema and tightens the contract. It does not weaken any requirement.
- Recorded verification: the pytest regression run has one failure, the known unrelated test_routines wellness test. Ruff is clean.

Non-blocking note: claimed_identity.entity_refs is not checked against the packet's allowed refs. The refs are bounded OpaqueRef strings, not free text, so this is not a blocker. Tighten it when the observation is first consumed.

Not verified, as the coder states: the real Granite route, quality and latency.
