# Observer compact@2: closing the 38% gap (NS-ADR-080)

Date: 2026-11-13. Owner: "definitely close all the gaps here, we're only getting 38% accuracy". Commits `16db925e`, `c2bc2d76`, `58eecfab`, `a4d38ffa`, plus the link-number fix and the ConfigMap switch. Still SHADOW: an observation never changes a disposition.

## Why compact@1 was wrong

Joined against the deterministic findings on live data (aggregate queries only):
- The model never knew who the sender was or whether the sender was authenticated. 324/342 messages have provider-reported DMARC pass.
- Every email-service tracking link looked like a mismatch to it: `LINK_MISMATCH` appeared on 113 of the 129 PHISHING_SUSPECTED labels.
- Hygiene rule codes (`REMOTE_IMAGE_RESOURCE` alone on 1706 phishing-labelled rows) were in the prompt and primed it.
- Categories and signals had no definitions, so `INSTRUCTIONS_TO_AI` fired on ordinary calls to action (104 times).
- There were no labels, so accuracy was never measured.

## What changed

- **Trusted context lines** go before the untrusted data block. They are code-derived and can't be forged from mail text (tested). They give the From registrable domain (with free-mail marked), a provider-reported DMARC summary (`DMARC_PASS`/`DMARC_FAIL`/`UNKNOWN`), per-link `(sender)`/`(other)` alignment, and link and attachment counts.
- Hygiene codes are dropped from the packet. Return-path mismatches are dropped only when DMARC passes.
- **Facts, not verdicts.** The model reports these closed-enum facts: `claims_to_be`, `claim_matches_sender`, `asks_secret`, `asks_money` and `asks_identity_data`. **Code decides PHISHING_SUSPECTED** with a fixed rule (`decide_v2`), which flags any of these cases:
  - a high-impact ask combined with a suspicious identity (an organization or person that doesn't own the domain, an organization writing from free mail, or the model's own impersonation signal);
  - a high-impact ask combined with a DMARC fail;
  - a secret asked for by a sender without a verified DMARC result;
  - money combined with new payment details.
- Definitions for every category and signal. `gist` is no longer requested (it was never stored).
- Format robustness, compact@2 only:
  - **Thinking:** sends `chat_template_kwargs.enable_thinking=false`.
  - **Text after the JSON:** a leading JSON object is accepted and the text after it is discarded unread. Prose-first replies and duplicate keys are still rejected.
  - **Unknown values:** request types that aren't in the list become `OTHER`, and urgencies that aren't in the list become `UNKNOWN`. Facts may be `UNKNOWN`, which is never read as YES.
  - **Bad link numbers:** an unlisted link number attaches the ask to the message, and refs stay code-mapped. compact@1 is unchanged and still fails closed.
- A labeled **synthetic eval set**, `research/observer_eval.py`, with 22 benign and 15 phishing cases. The benign cases deliberately include authenticated bulk mail on tracking domains, legitimate sign-in and bill asks, and unauthenticated small senders. The phishing cases cover lookalikes that have their own DMARC pass, spoofs that fail DMARC, BEC from free mail, invoice fraud, a callback scam and a prompt injection. Owner tool `observe_existing`:
  - `--eval-synthetic` runs it and stores nothing.
  - `--compare` gives aggregate-only dry runs over real mail and stores nothing. It is limited to the Reader role.

## Results

Synthetic set, live models, nothing stored:

| | compact@1 Granite | **compact@2 Granite** | compact@2 Qwen (`secure/coder`) |
|---|---|---|---|
| Format OK | 91.9% | **97.3%** | 100% |
| Category accuracy | 67.6% | **86.1%** | 81.1% |
| Phishing false-positive rate | 19% (4/21) | **0% (0/21)** | 0% |
| Phishing recall | 92.3% | **73.3%** | 53.3% |
| Phishing precision | 75% | **100%** | 100% |
| High-impact ask recall | 6.7% | **64.7%** | 76.5% |
| INSTRUCTIONS_TO_AI false positives | 2 | **0** | 0 |
| p50 latency | 0.9 s | 1.1 s | 0.45 s |

Real mail (all 342 stored messages, aggregate only, nothing stored), Granite:

| | compact@1 (stored) | **compact@2 (dry run)** |
|---|---|---|
| Format OK | 89.7% | **99.1%** |
| PHISHING_SUSPECTED | 37% (129/348) | **4.4% (15/339)** |
| …on DMARC-pass LOW_PRIORITY mail | 118 | **13/311** |
| INSTRUCTIONS_TO_AI | 104 | **2** |

## Decisions taken

- **The live observer switched to compact@2:** `NERVE_MAIL_SECURITY_OBSERVER_CONTRACT=COMPACT_V2`. Removing the key reverts to compact@1. New mail uses compact@2.
- **Existing rows are not re-observed.** The 348 stored compact@1 observations stay, labelled by `prompt_version`. Re-observing would mean deleting them and would double-count expectedness, so that's left to the owner.
- **Granite stays the observer.** Qwen is faster and better at high-impact asks, but its phishing recall is lower (53% vs 73%). Its host has not had the no-prompt-logging proof that Granite had, so real mail stays off it: `--compare` refuses any role but Reader.

## Remaining gaps (honest)

- **Recall 73%, misses:**
  - **p01, lookalike with its own DMARC pass:** the model said the brand matches.
  - **p03, CEO gift cards:** the model said the person matches the free-mail sender.
  - **p11, compromised but authenticated sender:** the hardest case.
  - **p15, callback scam with no link or ask.**

  The deterministic typosquat and freemail rules remain the main defense for p01 and p03.
- 13 real messages labelled phishing on DMARC-pass LOW_PRIORITY mail are unreviewed. They could be true lookalikes (DMARC passes for lookalike domains) or false positives. The tool shows aggregates only, by design.
- The synthetic set is small: 37 cases, written by me. It measures the known failure modes, not the real-world distribution.
