# Phase 4 — G4 Gate summary (owner-facing) — STOP, decision requested

**Status: STOPPED AT G4. Phase 5 has NOT been started and will not start without an explicit owner decision recorded below.**
**Scope run:** tasks 4.1 to 4.6, fixtures only. No real mail reached any model. No cluster or host was contacted. Nothing is wired into the assessment pipeline. All changes are uncommitted in the working tree.
**Legend:** VERIFIED = observed by the orchestrator (full suite re-run, import greps). REVIEWED = approved by an independent reviewer from the diff. NOT MET = not done.

## Verdict in one paragraph
The Phase 4 library layer is built and tested against hostile fixtures: strict observation contract, a fail-closed local-only route resolver, a Granite observer, an optional Qwen challenger, a versioned escalation policy and a prompt-injection red-team suite. The safety properties hold on fixtures. Nothing has been proven against the real OmniRoute, the real Granite endpoint or any Qwen deployment, so the layer is not ready to be wired in.

## Checklist
| Task | Status | Evidence |
| --- | --- | --- |
| 4.1 Strict `SemanticObservation` | **VERIFIED, REVIEWED** | Omitted lists, `schema_version` and `model_confidence` now rejected; strict confidence 0..1; model/schema sync test. `test_mail_security_observation_contract.py`. |
| 4.2 Fail-closed local-only route resolver | **VERIFIED on fixtures, REVIEWED** | `route_guard.py`, NS-ADR-028. Denies on unknown alias, cycle, depth bomb, missing provider, unreachable registry, any remote hop. Live registry shape NOT verified. |
| 4.3 Granite observer | **VERIFIED, REVIEWED** | `granite_observer.py`, NS-ADR-029. No tools, minimal evidence, bounded retry, typed errors, route guard before send. |
| 4.4 Qwen challenger | **VERIFIED, REVIEWED** | `qwen_challenger.py`, NS-ADR-030. Optional; typed unavailable; can only escalate. Qwen deployment is unproven. |
| 4.5 Escalation policy | **VERIFIED, REVIEWED** | `escalation.py`, NS-ADR-031 (`mail-escalation-policy@1`). Later stages never lower the deterministic/PDP floor; failures go to REVIEW/INCOMPLETE. |
| 4.6 Prompt-injection red team | **VERIFIED, REVIEWED** | 13 payloads x 5 placements, 55 hostile model replies, canary leak check. 728 tests. |
| Not wired into pipeline | **VERIFIED** | Grep: no module outside `backend/mail_security/` imports the new modules; static tests enforce it. |

Full suite (`pytest -m "not integration"`): **7524 passed, 1 failed, 2 skipped.** The failure is the pre-existing date-dependent `test_routines.py::TestWellnessSleepSeam` test, unrelated.

## Gap closed after the first draft: live registry check
See `phase4-live-registry-check.md`. The real OmniRoute shapes parse with the existing guard (no code change). Over all 91 live combos, 28 resolve local-only and 63 deny. `hybrid/code` and `hybrid/reader` DENY (cloud fallback hops: openrouter, mistral); `hybrid/coder` does not exist. Every `local/*` alias ALLOWS, backed by real private base URLs (`vllm` at 10.1.1.12:2701 = Granite, 192.168.133.3:2701, `llama-cpp` at 192.168.133.2:7777). The alias question is therefore decided by evidence: use a `local/*` alias, suggest `local/reader`. Owner to confirm.

## What is NOT proven
1. (Closed, see above.) Live registry shapes are verified read-only.
2. The resolver does no DNS. A private-IP proxy that forwards to a cloud provider would look local (NS-ADR-028). "Local" means private network, not on-host, and those endpoints are unauthenticated. Re-run the registry check whenever the registry changes.
3. (Narrowed.) Which `local/*` alias NerveShield uses is an owner confirmation; the contents and quality of `local/reader` are not evaluated.
4. Granite 4.2 serving route and a Qwen deployment are unproven. No real model has seen a synthetic prompt through this code.
5. Escalation defaults (Arc band edges 0.2/0.8, call and evidence budgets) are unmeasured. Cost and high-risk coverage metrics are not computed (no labelled corpus).
6. Prompt quality against a real model is untested; the red team simulates compromised models, not real ones.
7. Unauthenticated ArcForge (8100) and Granite (2701) endpoints remain an open G3 risk and are not decided.

## Housekeeping to be aware of
Working tree also holds modified tracked artifacts (`__pycache__`, `egg-info`, `node_modules/.package-lock.json`) that must not be committed, and untracked `.agents/`, `.kiro/steering/`, `nerve-shield-kiro-spec*` which stay out of commits.

## Options for your decision
1. **Approve Phase 5 on fixtures.** Wiring behind the flag, SHADOW mode only.
2. **Prove the route first.** A read-only contract check of the live OmniRoute registry against `route_guard.py`, plus a decision on the alias names and endpoint authentication, before any wiring.
3. **Pause here.** Commit the Phase 4 library locally and stop.

My recommendation is 2 before 1: the guard is only as good as its assumptions about the live registry, and that is cheap to check read-only.

## Owner decision (to be completed by owner)
```
Decision:
Date:
Notes:
```
