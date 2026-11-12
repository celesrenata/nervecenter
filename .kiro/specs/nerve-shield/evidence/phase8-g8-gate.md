# G8 gate — review research findings (STOP)

Date: 2026-11-12. Phase 8 spikes 8.1–8.5 are complete (owner: "continue and complete phase 8"). Per task 8.6 and R-025, the owner reviews the findings and approves any follow-on product spec. **No research feature is enabled, and none can be:**
- research code lives only in `backend/mail_security/research/`;
- a test proves no runtime module imports it;
- there are no flags, routes or workers for it;
- no owner mail was used. The data was synthetic or the public SpamAssassin corpus.

## Results at a glance

| Spike | Question | Headline result | Blocking prerequisite | Evidence |
|---|---|---|---|---|
| 8.2 Thread continuity / hijack | Is a reply really part of the conversation? | Lookalike, compromised-sender payee change and forged-From with Reply-To give 100% / 100% / 89.5% BROKEN; 0% BROKEN on legitimate replies. **A new external colleague is indistinguishable from injection** (both SUSPECT). Fabricated refs abstain | **Production evidence is 100% `BODY_ONLY`**: no Message-ID, References, Reply-To or auth headers | `phase8-8.2-results.md` |
| 8.1 Campaign clustering | Group campaigns without content? | High precision / low recall (P 0.83, R 0.16 at 0.9; 0 ham contamination). **77.5% of raw fingerprints are unique, so they are quasi-identifiers**; only k≥3 aggregates are shareable. Epoch keys are fully unlinkable but split 60% of campaigns | none for an offline study; cross-account sharing needs a privacy review | `phase8-8.1-results.md` |
| 8.4 Cross-channel adapter | Correlate channels without permission leakage? | Structural isolation: ungranted channels are never fetched (spy-verified); per-account, per-channel, per-purpose grants plus the existing PDP; metadata only; no universal score | a durable grant store with owner-only grant/revoke; person resolution accuracy | `phase8-8.4-results.md` |
| 8.3 Personal expectedness | Does personalization beat a global prior? | AUC 0.981 vs 0.917 (high-impact asks: 0.972 vs 0.857). Sparse senders abstain (0% FP, 0% recall). An account-derived prior is dominated by heavy senders. Added an honest `UNCERTAIN` status | **request-type extraction** (Granite observer) isn't live: 0 model runs | `phase8-8.3-results.md` |
| 8.5 SMTP ingress + detonation | Separate specs with measurable tradeoffs | Two draft specs. SMTP: do raw RFC 822 via the Gmail API first; a perimeter only for owner-controlled domains. Detonation: **not yet**; the namespace has no NetworkPolicy, there's no dedicated node, and no malware-handling decision | see specs | `phase8-8.5-results.md`, `../nerve-shield-smtp-ingress/`, `../nerve-shield-detonation/` |

## Cross-cutting finding

Most of the value in 8.2, 8.3 and the existing auth-evidence detectors is blocked by **evidence fidelity**. The mail worker keeps decoded body text only. The highest-leverage follow-on is **raw RFC 822 (or `format=metadata` headers) acquisition through the existing Gmail API connection**: encrypted on the new NFS store, in SHADOW, with no new boundary. That one product spec unblocks most of Phase 8.

## Proposed follow-on product specs (each needs explicit owner approval)

1. **Evidence fidelity: Gmail raw / metadata acquisition** (8.5 option C). It unblocks 8.2 and 8.3 and the existing SPF/DKIM/DMARC provenance. **Recommended first.**
2. **Thread continuity in SHADOW** (8.2), after (1). It emits only SUSPECT/BROKEN as review-queue reasons.
3. **Semantic observer in SHADOW** (Granite request types), followed by **personal expectedness** (8.3) with a population or relationship-class prior. Opt-in per account.
4. **Channel grants store** (8.4), owner-only grant and revoke, before any non-email channel is read.
5. **Static deep attachment inspection** (no execution) as a detonation precursor.
6. **Campaign clustering**: per-account content campaigns, with structural clusters as a secondary signal. Cross-account aggregates only after a privacy review.

**Not recommended now:** an inline SMTP MX gateway, live detonation, cross-account fingerprint sharing.

## Verification

| Check | Result |
|---|---|
| New research tests (8.1–8.4) | 42 passed (5 + 15 + 9 + 13) |
| Full suite `uv run pytest tests/ -m "not integration" -q` | 8519 passed, 1 failed (known date-dependent `test_routines`), 3 skipped |
| Wiring guards | the 6.x guards (owner tools and training selection not imported by the service) caught the 8.1 corpus evaluator importing them from `research/`; it moved to `backend/owner_tools/campaign_cluster_eval.py` (guards unchanged). The 8.2 research guard exempts `owner_tools` the same way |

Things the tests caught and fixed during the spikes:
- In 8.1, the always-on tokens inflating similarity, which led to v2.
- In 8.4, a 4-segment resource URI that made the PDP fail closed. That was the correct behavior.
- In 8.3, a dominant-sender prior bias (now documented and asserted), and uncertain verdicts mislabeled as EXPECTED (now an `UNCERTAIN` status).

## Owner decision required

For each follow-on above: approve, reject or defer. Nothing proceeds without it.
