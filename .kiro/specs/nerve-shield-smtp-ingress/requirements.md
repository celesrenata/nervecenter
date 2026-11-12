# Optional SMTP ingress perimeter — scoping spec (DRAFT, not approved)

Origin: NerveShield task 8.5 (design §15, "Optional inbound SMTP perimeter"). This is a **new security boundary**: a separate product spec, not an extension task. Nothing here is built.

## 1. Problem

NerveShield sees mail **after** Gmail delivered it, through the Gmail API. Two things are missing as a result:

- **SMTP-time evidence:** the client IP, `HELO`, envelope `MAIL FROM`/`RCPT TO`, delivery-time DNS (SPF/DKIM keys and DMARC as they were *at delivery*), and the TLS parameters. True SPF validation needs the connecting IP and envelope (design §5.2). Historical DKIM is limited by key rotation.
- **Full headers.** Production evidence is 100% `BODY_ONLY` today (spike 8.2), and that blocks header-based detectors before SMTP even matters.

## 2. Hard constraint to resolve first

**Custom MX is only possible for domains the owner controls.** `@gmail.com` mailboxes can't be fronted by an owner MTA. If both connected accounts are `@gmail.com` addresses, options A and B below don't apply and only C is possible.

> Owner decision D-1: Which receiving domains (if any) are owner-controlled custom domains?

## 3. Options

| | A. Inline MX gateway (in front of Gmail / Workspace) | B. Parallel journal copy (BCC/forward to own MTA, analysis only) | C. No SMTP; acquire full RFC 822 via Gmail API (`format=raw`) |
|---|---|---|---|
| Evidence gained | full SMTP-time evidence; could reject at SMTP | SMTP-time evidence for the *copy* (the relay hop, not the original sender's IP); full RFC 822 | full RFC 822 and the provider's `Authentication-Results`; **no** sender IP or envelope |
| Can block mail | yes (high risk) | no | no |
| Availability risk | **critical**: MX outage queues or bounces all mail | none for the primary mailbox | none |
| New attack surface | public port 25, TLS, open-relay risk, abuse traffic, DoS | public or private SMTP listener, narrower | none (existing OAuth) |
| Operational cost | high: IP reputation, PTR, MTA-STS/TLS-RPT, cert rotation, queue ops, spam load | medium | low |
| Residential ISP | inbound 25 often blocked; needs a VPS/relay front | same | n/a |
| Fit with R-012 (SHADOW first) | violates SHADOW unless pass-through only | fits | fits |

## 4. Recommendation for the gate

**C first.** Raw RFC 822 acquisition is a contained change to the existing worker. It lifts fidelity from `BODY_ONLY` to `ORIGINAL_RFC822` and unblocks spikes 8.2, 8.3 and the existing auth-evidence detector. Consider **B** only for owner-controlled domains, once C is in and measured. **A** is out of scope until there's a measured need that B can't meet.

## 5. Requirements, if B is approved later

1. **SR-1:** The listener accepts only from configured relay sources (an allow-list of IPs) and only for configured recipient domains. It is never an open relay; this is tested with an open-relay test suite.
2. **SR-2:** Mail is accepted to a durable queue encrypted at rest (reusing the NS-ADR-062 envelope) before any analysis. No analysis step can affect delivery to the primary mailbox.
3. **SR-3:** SMTP-time evidence (client IP, HELO, envelope, TLS version/cipher, delivery-time DNS answers) is recorded as metadata with explicit `INDEPENDENT` provenance. The message itself goes through the existing parser limits.
4. **SR-4:** The listener runs in its own namespace, with a NetworkPolicy allowing only egress to DNS and the NerveShield queue. It holds no NerveCenter secrets other than its queue write credential.
5. **SR-5:** It is rate-limited per source and size-limited per message; overload rejects with 4xx and never drops silently.
6. **SR-6:** Retention and deletion follow the same per-account rules as Gmail-acquired evidence.

## 6. Measurable acceptance (for any option)

| Metric | Target to propose after baseline |
|---|---|
| Evidence fidelity | share of assessments at `ORIGINAL_RFC822` (today 0/632) |
| Independent auth coverage | share with an `INDEPENDENT` SPF/DKIM/DMARC result |
| Detector uplift | 8.2 continuity BROKEN/SUSPECT recall on synthetic hijacks run on real-shaped headers |
| False-positive delta | change in REVIEW rate on owner mail (SHADOW only) |
| Availability (B) | 0 primary-mailbox delivery changes; listener uptime is not on the critical path |
| Security (B) | open-relay suite 100% rejected; external port scan shows only 25/TCP with STARTTLS |

## 7. Owner decisions

- **D-1:** Which domains (if any) are owner-controlled? (Gates A and B.)
- **D-2:** Approve option C (raw RFC 822 via the Gmail API, encrypted, SHADOW) as the next product spec?
- **D-3:** For B, where does the listener terminate (VPS front vs. in-cluster)? Who owns IP reputation and abuse handling?
