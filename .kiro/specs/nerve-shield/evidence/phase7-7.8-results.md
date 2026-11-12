# Task 7.8 — Adversarial release review, rollback rehearsal, real-account SHADOW and REVIEW_GATED canary

Date: 2026-11-12. Owner decisions (verbatim answers to the four 7.8 questions): "1. all 2. yes 3. 10 minutes is fine 4. sure".

## Selected accounts and rules

**Accounts (all connected mail accounts):**

| Scope | Kind |
|---|---|
| `conn:53b03939-bdf6-4313-a7e3-b91144c598d2` | Gmail, enabled |
| `conn:fc4e3c57-2f5f-49b6-87e7-9f3e87fd813e` | Gmail, enabled |

**SHADOW observation** stays on for both accounts. The mode is `SHADOW`: no provider mail is moved, deleted, marked or answered (R-012.1).

**REVIEW_GATED canary** is on for both accounts. They are listed explicitly in `NERVE_MAIL_SECURITY_PROMOTION_GATE_SCOPES`, so a future account never joins silently.

**Gate rules** are the 7.4 defaults (owner kept them):
- SAFE_TO_PROCESS and LOW_PRIORITY release with the exact original id.
- REVIEW, RESTRICTED and INCOMPLETE become review candidates.
- No verdict means the item is held (fail closed).

**Confirmation rules** are the 7.6 defaults: a step-up within 10 minutes (owner confirmed) plus sender attestation for PAYMENT, OUTBOUND_MESSAGE and EXTERNAL_DISCLOSURE. Execution stays default-deny, and no executor exists.

**Code change for the canary.** `gate_scopes()` and `MailPromotionGate(scopes=…)` implement the allow-list:

| Value | Effect |
|---|---|
| `*` | all mail accounts |
| list of `conn:<uuid>` | exactly those accounts |
| unset, empty, or any malformed entry | no account is gated |

There are 3 new tests (parsing, canary-only gating, empty canary gates nothing), and the 7.4 suite still passes: 31 tests.

## Adversarial release review (R-022)

Each R-022.2 class maps to automated tests in the current tree:

| Attack class | Covered by |
|---|---|
| Forged / untrusted auth headers | `test_mail_security_auth_evidence.py`, `_harness.py`, `_prompt_injection.py` (UNTRUSTED_HEADER provenance never upgrades trust) |
| Signed-but-compromised sender | `_policy.py` (high-impact credential or payment ask mandates REVIEW even with independent auth), `_prompt_injection.py`, 7.2 advice shown when auth passes |
| Homoglyph / confusable domains | `_inspections.py`, `_first_party_domains.py`, `_auth_evidence.py` |
| Benign SPF failures | `_policy.py` |
| Encrypted attachments | `_attachments.py` (encrypted means not scannable, so INCOMPLETE, never clean) |
| Link deception | `_parser.py::test_deceptive_link_captures_both_display_and_target`, `_inspections.py::test_deceptive_link_display_target_mismatch`, `_harness.py::test_deceptive_link_plus_high_impact_ask_is_exactly_restricted` |
| Poisoned datasets | Phase 6: `_training_selection.py` (approved feedback only), `_dataset_manifest.py` (signed manifest, split keys), `_feeds.py`, privacy evaluation canaries (6.4) |
| Prompt injection / instructions in mail | `_inspections.py`, `_harness.py`, `_prompt_injection.py`; 7.5 proposals have no free-text field; 7.6 bodies are `extra=forbid` |

Phase 7 escalation paths:

| Threat | Covered by |
|---|---|
| Malicious mail minting actionable items | `test_mail_security_trust_gate.py` |
| Feedback becoming trust | `test_mail_security_feedback_api.py` (no allowlist, no intel, training BLOCKED_NO_CONSENT) |
| Proposal capability escalation | `test_mail_security_task_proposals.py` |
| Malicious instruction escalating to approval | `test_mail_security_confirmations.py` |
| Rendering / remote fetch | `mail-safety-card.spec.ts`, `security-review.spec.ts`, `security-ops.spec.ts` |
| Redaction of operational data | `test_mail_security_ops_dashboard.py` |

**Release gate run:**
- `uv run pytest tests/backend -k "mail_security or attention_durable or detection"` across 57 NerveShield test modules: **6682 passed**, 10 skipped.
- The **real-Postgres concurrency suite** ran against an empty throwaway database (`nervcenter_pgtest`) on the cluster Postgres via port-forward: **7 passed**. The database was dropped afterwards, and no production data was involved.
- Full suite: **8450 passed**, 1 known date-dependent failure (`test_routines::TestWellnessSleepSeam`).

**Residual risks the review accepts and carries into G7:**
- The SSO step-up has not been exercised against live Keycloak (7.6).
- There is no proposal producer and no executor yet (7.5 / 7.6).
- `sweep_overdue` is a latent in-memory bypass with no production caller (7.4).
- ArcForge, Granite and OmniRoute endpoint authentication is still open.
- Object-store encryption is open (raw capture stays OFF).
- PROXY_DATA_ONLY stays strict (G6).

## Migration and rollback rehearsal

The rehearsal used a copy of production made **inside the Postgres pod**: `pg_dump nervcenter | psql nervcenter_rehearsal`, 106 MB. No production data left the cluster.

**Migrations (current image against the clone):**

| Step | alembic | Core tables* | 018–020 tables |
|---|---|---|---|
| before | 020 | 629 / 130 / 108 / 0 / 0 / 629 / 629 / 629 / 49 / 0 | present (0 rows) |
| downgrade → 017 | 017 | identical | dropped |
| upgrade → head | 020 | identical | recreated (0 rows) |

*In order: communications, people, attention_items, commitments, followups, mail_security_jobs, mail_evidence, mail_security_assessments, mail_security_findings, mail_intel_indicators. All 10 core tables were unchanged at every step.

**Image rollback (old image `e671255` against the clone):**
- Against schema 020, the old image's migrate fails: **exit 1**, `Can't locate revision identified by '020_mail_confirmations'`. Its *reads* still work (629 assessments, communication refs readable), because the schema changes are additive.
- After a downgrade to 017 with the current image, the old migrate is a no-op (**exit 0**) and the reads work.

**Rollback runbook (from the rehearsal):**
- **A. Fast, keeps all data.** Roll the image only, skipping the migrate Job:
  ```
  kubectl -n nervecenter set image deploy/api api=registry.celestium.life/nervecenter/api:<TAG>
  ```
  The schema stays at 020, which older code ignores. Do **not** use `TAG=<old> deploy.sh` for this: its migrate Job will fail and time out, as it did in 7.4.
- **B. Full schema rollback.** This drops feedback, holds and confirmations.
  1. With the *current* image: `alembic downgrade <old head>`.
  2. Then `TAG=<old> ./deployments/harbor/deploy.sh`.

**Canary rollback:**
1. Run one release pass so that items holding a SAFE or LOW_PRIORITY verdict are written before the gate goes off:
   ```
   kubectl -n nervecenter exec deploy/api -- python -c "import asyncio; from backend.persistence.connection import config_from_env, create_persistence; from backend.persistence.repositories import RepositoryRegistry; from backend.persistence.repositories.mail_security_gate_repo import HeldPromotionRepository, release_pass; e,f=create_persistence(config_from_env()); r=RepositoryRegistry(f); print(asyncio.run(release_pass(HeldPromotionRepository(f), attention=r.attention, commitments=r.commitments)))"
   ```
2. Delete `NERVE_MAIL_SECURITY_PROMOTION_GATE` and `_SCOPES` from the ConfigMap, then redeploy.

Items still HELD with no verdict at that point stay held. They're visible as holds, but they won't reach Today.

All rehearsal artifacts were removed: the pod, the ConfigMap and the `nervcenter_rehearsal` database. Production stayed at `020` throughout.

## Real-account canary observation

- **Activation:** deployed `f230bb13` at 05:53:44 UTC. The log line `api.wiring.promotion_gate_bound mode=REVIEW_GATED scopes=2` confirms the gate is bound.
- **First live message after activation** (received 05:54:13 UTC):
  - It was held as `attention`.
  - It was assessed LOW_PRIORITY and released after **29 s**.
  - The released `attention_items` row has the **same id** as the hold and is the only row with that id.
  - Active Today items went from 108 to 109.
- **Baseline at activation:** 630 communications and 108 active attention items. Verdicts over all observed mail: 624 LOW_PRIORITY, 4 REVIEW (7.7 dashboard).

The observation window continues below until G7.

**Second observation, at about T+15 min (counts only):**
- Communications since activation: 1.
- Holds: 1 RELEASED (LOW_PRIORITY, attention).
- Active attention: 109.
- Duplicate attention ids: 0.
- Actionable rows for any non-RELEASED hold: 0.
- `RELEASE_PASS_FAILED` and runner failure log lines: 0.

**Not yet observed live.** A REVIEW, RESTRICTED or INCOMPLETE message under the gate (the review-candidate path) hasn't happened yet: live mail is mostly LOW_PRIORITY, with 4 REVIEW in 630 so far. That path is covered by automated tests (`test_restricted_mail_cannot_mint_actionable_attention`, `test_review_and_incomplete_are_candidates`), but has no live event. I didn't inject a synthetic message into production. If the owner wants a live demonstration before deciding, sending a test email to one of the accounts would exercise it.
