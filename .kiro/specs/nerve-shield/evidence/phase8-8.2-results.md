# Spike 8.2 — Conversation continuity / thread-hijack detection (research)

Date: 2026-11-12. Status: research only. The code lives in `backend/mail_security/research/` and nothing imports it (a test enforces this). Nothing is wired, no flag exists, and no real mail was used.

## Question

Can NerveShield tell a genuine reply in an existing conversation from mail that only *claims* to continue one? The cases studied are:
- spoofed `References` / `In-Reply-To`;
- a lookalike domain;
- a changed `Reply-To`;
- a compromised but authenticated sender who changes payment details.

## Prototype (`thread_continuity.py`)

**Ledger.** Each account gets a ledger of previously received mail. It holds:
- an HMAC fingerprint (per-account salt) of each normalized `Message-ID`;
- the registrable sender and Reply-To domains;
- an independent-authentication flag;
- an opaque payee fingerprint.

It stores no subject, body, local part or full address; a test checks this.

**Headers are attacker-controlled.** `CONTINUOUS` requires all three of:
1. a cited parent that exists in this account's ledger;
2. a sender domain that is already a participant in that thread;
3. no authentication downgrade relative to that participant's history.

A forwarded or mailing-list delivery (ARC, `List-Id`) does not count as a downgrade.

**Signals and weights.** `BROKEN` is a score of 3 or more; `SUSPECT` is 1–2.

| Signal | Weight |
|---|---|
| UNKNOWN_PARENT | 1 |
| REPLY_SUBJECT_WITHOUT_REFERENCES | 1 |
| NEW_PARTICIPANT_DOMAIN | 1 |
| REPLY_TO_DIVERGES | 2 |
| AUTH_DOWNGRADE | 2 |
| LOOKALIKE_OF_PARTICIPANT | 3 (reuses `_domains.is_confusable_domain` + `inspections` edit distance) |
| PAYMENT_DETAILS_CHANGED | 3 |

**Outputs.** `NOT_A_REPLY` / `UNKNOWN` (abstain) / `CONTINUOUS` / `SUSPECT` / `BROKEN`. **No output means safe.**

## Synthetic evaluation (`thread_continuity_eval.py`)

The generator is seeded (seed 7), runs 400 scenarios per category over 10 organizations, and builds threads of 1–6 messages with distractor threads. Raw output: `phase8-8.2-eval.json`.

| Category | Result | Flagged (SUSPECT+BROKEN) | BROKEN |
|---|---|---|---|
| L_REPLY | CONTINUOUS 400 | 0% | 0% |
| L_LIST_FORWARD (auth broken by forwarding) | CONTINUOUS 400 | 0% | 0% |
| L_NEVER_AUTH | CONTINUOUS 400 | 0% | 0% |
| L_TICKET_REPLY_TO (same org) | CONTINUOUS 400 | 0% | 0% |
| L_COLD_LEDGER (parent never ingested) | UNKNOWN 400 | 0% | 0% |
| **L_NEW_COLLEAGUE** (other org joins) | SUSPECT 400 | **100%** | 0% |
| L_VENDOR_PAYMENT_CHANGE (legit) | BROKEN 400 | 100% | 100% (intended: should trigger independent verification) |
| A_LOOKALIKE | BROKEN 400 | 100% | 100% |
| A_COMPROMISED_PAYMENT (auth passes) | BROKEN 400 | 100% | 100% |
| A_SPOOFED_FROM_REPLY_TO | BROKEN 358 / SUSPECT 42 | 100% | 89.5% |
| **A_UNRELATED_INJECT** | SUSPECT 400 | 100% | **0%** |
| **A_FABRICATED_REFS** | UNKNOWN 400 | **0%** | 0% |
| **A_RE_SUBJECT_ONLY** | UNKNOWN 400 | **0%** | 0% |

Excluding the intended vendor case, the legitimate categories show a mean flag rate of 16.7%, all of it from L_NEW_COLLEAGUE, and 0% BROKEN.

## Findings

1. **High-signal hijacks separate cleanly:** lookalike participants, compromised-sender payee changes, and a forged participant with an attacker Reply-To. Even an authenticated compromised sender is caught by the payee change. That's consistent with R-013.4 and the 7.2 advice.
2. **A legitimate new external participant is indistinguishable from thread injection by an unrelated domain.** Both are 100% SUSPECT on this evidence. Separating them needs a *verified prior channel*: the user, or an earlier authenticated thread, vouching for the new party. That belongs to 8.3 and the trust graph, not to header analysis.
3. **Fabricated `References` and "Re:"-only subjects abstain** (UNKNOWN). The ledger can't refute what it never saw, and treating unknown parents as evidence would flag every reply to mail sent before ingest began. Detection there has to come from other signals.
4. **Spoofed-From is only partly caught (89.5% BROKEN)** when the participant never authenticated before, because there's no baseline for a downgrade.
5. **Blocking prerequisite: production evidence doesn't carry these headers.** All live assessments are `BODY_ONLY` (632/632) and all evidence is `LEGACY_BODY_ONLY`. The mail worker stores decoded body text, not `Message-ID` / `References` / `Reply-To` / `Authentication-Results`. **This detector cannot run on production data until header acquisition exists** (Gmail `format=metadata` for the needed headers, or `format=raw` for ORIGINAL_RFC822 fidelity). The same gap limits the existing SPF/DKIM provenance findings.

## Limits

- **The synthetic data is idealized.** Real threads have CC changes, aliasing, shared-hosting senders and broken `References`, so the numbers prove the logic, not real-world accuracy.
- **Payee fingerprints are assumed as input.** Extracting them from bodies (IBAN, routing number, payment-link host) is not built.
- **Registrable-domain logic uses the minimal built-in PSL** from `inspections`.
- **The weights are hand-set,** not calibrated.

## Proposed follow-on (needs a separate approval at G8)

1. **Header acquisition** (metadata-only Gmail fetch for the threading and authentication headers), stored as evidence fields rather than content. This is the prerequisite.
2. **Then** run the ledger in SHADOW, emitting only `SUSPECT`/`BROKEN` as review-queue reasons. Don't use it to clear anything.

## Tests

`tests/backend/test_mail_security_research_8_2.py`: **15 passed**. It covers:
- a genuine reply is CONTINUOUS, and no status means safe;
- lookalike, compromised payee and forged-From-with-Reply-To are BROKEN;
- forwarding is not a downgrade;
- fabricated headers never prove continuity, and a cold ledger abstains;
- the ledger holds no ids, addresses or subjects, and fingerprints are normalized and salted per account;
- the evaluation targets hold, and the documented limits are asserted so they stay visible;
- the evaluation is deterministic;
- no runtime module imports `mail_security.research`;
- reply-subject forms are recognized.
