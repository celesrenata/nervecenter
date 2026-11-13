# Semantic observer live in SHADOW (NS-ADR-073, G8 item 3)

Owner decision: "do em" (2026-11-13). This is the first time real owner mail goes to a model. Commits: `9b2666ba` (wiring), `85cf819f` and `32f94b82` (registry size and timeout fixes). Migration head is `023_mail_semantic_obs`.

## Preconditions proven before any real mail was sent

All checks used synthetic canaries only. Keys were read into environment variables and never printed.

| Check | Method | Result |
|---|---|---|
| OmniRoute memory writes | Canary A through `local/reader` (positive control), canary B through `secure/reader`, both with NerveCenter's own key from inside the api pod. Polled `/api/memory` for about 4.5 min. | **Control written within 15 s.** The model returned a `memory_save` tool call and about 1000 prompt tokens were injected. **The secure canary was never written:** 53 prompt tokens, no tools. The control entry was deleted afterwards; memory is back to its original 22 entries. |
| Existing memory | Listed metadata for all 22 entries | 1 email-shaped entry (`email:syn:1`, 2026-11-10, synthetic gardening text from the phase-4 tests, management key). No real mail. |
| Request/response body storage | `/api/usage/call-logs` metadata | The `local/reader` canary had stored bodies (`detailState: ready`). The **`secure/reader` canary stored no bodies** (`detailState: none`). Neither canary appears in usage, request or proxy logs. |
| Model host logging | Read-only SSH to gremlin-1: process args, source grep, and the `granite4-rtx.service` journal | Custom `granite4-api` (plain HTTP server plus a GPU engine over stdin). Its journal holds only startup lines; there are **no per-request entries** and 0 canary matches. |

This closes the long-open item "OmniRoute memory-write absence not proven".

## Wiring

- **Transports**, in `backend/connectors/omniroute_observer.py`. `backend/mail_security` stays network-free, which a test enforces.
  - Registry GET on exactly `/v1/models`, `/api/combos` and `/api/providers`.
  - Chat POST to `/v1/chat/completions` with the stricter-only no-memory and no-compression headers.
  - No redirects, 2 MiB response cap, credentials never in errors. A tool-call reply is handed to the validator, which rejects it.
- **`ObserverRuntime`** (`backend/mail_security/observer_wiring.py`, network-free) runs on every call. It reads NerveCenter's `Reader` combo, then passes it through `LocalOnlyRouteResolver`, which requires a secure combo and local-only leaves. It then runs the unchanged `granite_observer` with its compact contract, strict validation and no tools. Any deny or error is recorded as a typed status, and nothing else happens.
- **Evidence packet** (bounded):
  - included: the subject, at most 4000 characters of normalized text, link **hosts**, attachment **types** (sniffed, else claimed) and deterministic finding codes;
  - never included: filenames, raw MIME or addresses.
- **Storage.** Table `mail_semantic_observations` holds **enum codes only**: category, claimed role, identity consistency, request type, urgency and flags, and risk indicator codes, plus status, prompt version, route digest, attempts and latency. **The model's free-text `gist` is never stored**; 0 rows contain it.
- **Hook.** The security worker calls the observer after the verdict commits, and it can never change that verdict. The opt-in is `NERVE_MAIL_SECURITY_OBSERVER_SCOPES`, set to both Gmail accounts. Removing the key stops all model calls.
- **Guard tests.** The Phase-4 "not wired" guards are narrowed to an exact allow-list: `api/wiring.py`, `mail_security/observer_wiring.py` and `owner_tools/observe_existing.py`. `qwen_challenger` and `escalation` remain unwired.

## Live results (owner tool `observe_existing`, all 342 stored messages)

| Status | Count | Mean latency |
|---|---|---|
| COMPLETED | 289 (85%) | 4.3 s |
| ROUTE_DENIED (guard failed closed on registry reads) | 29 (8%) | — |
| OUTPUT_NOT_JSON | 15 | 19 s |
| OUTPUT_UNKNOWN_EVIDENCE_REF | 8 | 4.9 s |
| OUTPUT_SCHEMA_INVALID | 1 | 4.2 s |

Every failure is typed, and nothing partial is stored. Two fixes were needed during the first runs:
- `/v1/models` is 369 KB (988 models), so the cap was raised from 256 KB to 2 MiB.
- `/api/combos` sometimes takes more than 3 s, so the registry transport timeout is now 12 s and the resolver budget 15 s.

**Agreement with the deterministic verdict (completed only):**

| Observer category | LOW_PRIORITY | REVIEW |
|---|---|---|
| MARKETING | 107 | 6 |
| **PHISHING_SUSPECTED** | **101** | 10 |
| OTHER | 23 | 0 |
| NEWSLETTER | 19 | 1 |
| RECEIPT | 11 | 0 |
| AUTOMATED / PERSONAL / APPOINTMENT / ACCOUNT_SECURITY | 10 | 0 |

**Finding.** Granite 8B labels **38%** of real mail PHISHING_SUSPECTED, and 101 of those 111 are mail the deterministic pipeline rated LOW_PRIORITY with provider-reported DMARC pass. That is mostly marketing, read as phishing because of link-text mismatches and credential-style wording. It also emits `INSTRUCTIONS_TO_AI` on ordinary mail. **The observer's category and risk signals are not fit to influence anything.** Its request types (`OPEN_LINK` dominates, plus `REPLY`, `PAY`, `SCHEDULE`, `READ`) are the useful output, and they are the input personal expectedness (8.3) needs.

## Open

- 8% of messages fail closed because the guard re-reads the registry twice per message (no cache, by design). Options: one retry, or a very short-lived snapshot. A cache changes a stated guard property, so it is an owner decision.
- NerveCenter's OmniRoute key is the one named **`OmnirouteMGMT`**, which is broader than an app key needs (it can read the registry). A dedicated key limited to `secure/*` combos plus registry read would be tighter. That is an OmniRoute change on shared infrastructure, so it is left to the owner.
- Next: personal expectedness (8.3), using request types with a population prior, in SHADOW.
