# G0 — SMTP ingress perimeter (scoping gate)

Date: 2026-11-13. Owner: "yes" to scoping this spec (G8 follow-on gate, decision 4). Read-only checks only: public DNS lookups and the live NerveShield database. Nothing was built, and no listener was started.

**Verdict: STOP. Option A stays out of scope. Option B is now possible, but no measured need for it exists yet. Two DNS problems need fixing first, and they matter more than any perimeter.**

## What changed since the draft

| Draft assumption | Now (verified 2026-11-13) |
|---|---|
| Fidelity is 100% `BODY_ONLY` | **342/342** latest assessments are `ORIGINAL_RFC822` (option C, NS-ADR-064, live) |
| Auth is untrusted | 324/342 have DMARC `PASS` with `PROVIDER_REPORTED` provenance (trusted boundary `mx.google.com`, NS-ADR-070); 14 `UNTRUSTED_HEADER`, 4 `UNKNOWN` |
| D-1 unknown: owner domains? | **`celestium.life` and `frameshift.net` are owner domains**, both with MX on Google Workspace (`aspmx.l.google.com` + alt1-4) |

So the draft's main motivation (fidelity) is solved by C. What a perimeter would still add is **independent** SMTP-time evidence (connecting IP, HELO, envelope, delivery-time DNS) instead of Google's reported result.

## DNS findings (owner action, outside NerveShield)

| Domain | SPF | DMARC | DKIM (`google` selector) | MTA-STS / TLS-RPT |
|---|---|---|---|---|
| `celestium.life` | `v=spf1 include:_spf.google.com include:celestium.life ~all` | `p=reject`, but `rua=mailto:dmarc@yourdomain.com` | present | none |
| `frameshift.net` | **none** | **none** | **none found** | none |

1. **`frameshift.net` can be spoofed.** With no SPF, DKIM or DMARC, anyone can send as `@frameshift.net`, and receivers (Gmail included) have no policy to reject it. This is the highest-value fix in this whole spec. Suggested records: SPF `v=spf1 include:_spf.google.com ~all`; turn on DKIM in the Workspace admin console (Apps › Gmail › Authenticate email) and publish the `google._domainkey` key; then DMARC, starting at `p=none` with a `rua` you own, moving to `quarantine`/`reject` after reports look clean.
2. **`celestium.life` DMARC reports go to a third party.** `rua=mailto:dmarc@yourdomain.com` is template text. Aggregate reports (sending IPs and volumes for the domain) are addressed to `yourdomain.com`, a domain you don't control. Replace it with an address you own, or remove `rua`. The SPF `include:celestium.life` is a self-include; it is harmless only if the apex has no other SPF content. Worth reviewing.
3. Neither domain publishes MTA-STS or TLS-RPT. Inbound TLS to Google is opportunistic; this is low priority compared with 1 and 2.

These are DNS changes on live domains, so I have not made any of them.

## Options, re-scored

| | A. Inline MX gateway | B. Workspace journal/BCC copy to an owner listener | C. Gmail API raw (live) |
|---|---|---|---|
| Possible now? | yes (owner domains) | yes (Workspace routing rule) | **done** |
| New evidence over C | full SMTP-time evidence, independent auth | SMTP evidence of the **Google→listener hop only**; the original sender's IP survives only in Google's own `Received`/`Authentication-Results`, which C already has | — |
| Availability risk | critical (all mail) | none for the mailbox | none |
| New attack surface | public :25, open relay, DoS | an SMTP listener reachable from Google (VPS or exposed port), TLS, allow-list | none |
| Measured need | none | none yet | — |

Key point for B: because Google is the first hop, a journal copy does **not** give independent evidence about the original sender. It would only re-verify DKIM at a later time, which suffers from key rotation. That is small uplift for a new public listener.

## Recommendation

1. Fix the DNS findings first (owner action, no code).
2. Keep C. Do not build A.
3. Treat B as **not justified yet**. Re-open it only if a measured gap appears: for example, the share of `UNTRUSTED_HEADER`/`UNKNOWN` auth rising above a threshold you choose (today 18/342 = 5.3%), or a phishing miss that independent DKIM re-verification would have caught.

## Owner decisions

- **D-1 (answered by DNS):** owner domains are `celestium.life` and `frameshift.net`, both on Google Workspace.
- **D-2:** Fix the DNS records above yourself, or want me to prepare exact records and a rollout order (`p=none` → `reject`)?
- **D-3:** Close this spec at G0 (recommended), or keep B open with a re-open trigger, e.g. "independent auth needed for > N% of mail"?

## Closed (2026-11-13)

Owner: "we don't have local smtp as we're on comcast ... all email goes through google's gmail to celes@celestium.life and celes@frameshift.net and soon celes.renata66@gmail.com but that's it". Residential Comcast blocks inbound port 25 and there is no relay, so options A and B have no place to terminate. **Spec closed at G0.** Option C (Gmail API raw, NS-ADR-064) is the permanent acquisition path for all three mailboxes. Re-open only if mail moves off Google onto an owner-run MTA.
