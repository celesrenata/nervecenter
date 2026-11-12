# Task 8.5 — Independent scoping of SMTP ingress and detonation microVMs

Date: 2026-11-12. Status: **two separate draft specs**, not approved and not built. Both are new security boundaries, so they live outside the NerveShield spec:

- `.kiro/specs/nerve-shield-smtp-ingress/requirements.md`
- `.kiro/specs/nerve-shield-detonation/requirements.md`

## Facts gathered (read-only)

**Production mail fidelity.** 632/632 assessments are `BODY_ONLY` and 632/632 evidence records are `LEGACY_BODY_ONLY`. No header, envelope or provider auth evidence is stored.

**Cluster:**
- KubeVirt is deployed, with 3 VMIs running.
- There's no Kata or Firecracker runtime class.
- No node advertises the KubeVirt KVM label in the label query.
- The `nervecenter` namespace has **no NetworkPolicy**.
- No inbound SMTP service (25/465/587) exists anywhere in the cluster.

## Measurable tradeoffs

Both specs have tables of measurable security and operational tradeoffs.

**SMTP ingress:** inline MX vs. parallel journal vs. raw RFC 822 via the Gmail API, compared on:
- fidelity, independent-auth coverage, detector uplift and false-positive delta;
- availability and open-relay suite results.

**Detonation:**
- detection uplift on `INCOMPLETE` attachments and false positives on benign documents;
- latency and node-hour cost;
- isolation checks: egress denied, no service account token, no metadata access;
- patch cadence.

## Recommendations

**SMTP.**
- Do **raw RFC 822 acquisition via the Gmail API first** (option C). It is a contained change that removes the `BODY_ONLY` blocker shared by 8.2, 8.3 and the existing auth-evidence detector.
- An SMTP perimeter is only possible for owner-controlled domains (not `@gmail.com`). It should be a non-blocking parallel journal (B) at most.

**Detonation.**
- **Don't approve yet.** Prerequisites: an enforced NetworkPolicy, a dedicated tainted node, and an owner decision on handling live malware.
- Consider a no-execution "static deep inspection" spec as the intermediate step.
