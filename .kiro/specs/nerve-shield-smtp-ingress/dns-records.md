# DNS fixes for owner mail domains (draft, NOT applied; on hold)

> **Status 2026-11-13: on hold.** The owner reports "we don't have dns". Public
> lookups show both zones ARE hosted on Amazon Route 53 (`awsdns` name servers),
> so some AWS account or registrar-managed Route 53 setup controls them. These
> records can be applied only by whoever has that access. The `frameshift.net`
> spoofability finding stands until then.

Date: 2026-11-13. Owner: "go ahead" (draft the exact records and rollout order). Read-only `dig` lookups only; nothing was changed. Both zones are on **Route 53** (`awsdns` name servers).

## Current state

| Record | `celestium.life` | `frameshift.net` |
|---|---|---|
| MX | Google Workspace (`aspmx.l.google.com` + alt1-4) | Google Workspace (same) |
| SPF (apex TXT) | `v=spf1 include:_spf.google.com include:celestium.life ~all` | **none** |
| DKIM `google._domainkey` | present | **none** |
| DMARC `_dmarc` | `v=DMARC1; p=reject; rua=mailto:dmarc@yourdomain.com` | **none** |
| A (apex) | CloudFront | `24.19.103.182` |

## Problems

1. **`frameshift.net` is spoofable.** With no SPF, DKIM or DMARC, receivers have no policy to reject forged `@frameshift.net` mail, and your own mail from it can't be DMARC-aligned. That explains why messages from `celes@frameshift.net` can't reach a provider-verified DMARC pass in NerveShield.
2. **`celestium.life` DMARC reports go to a stranger.** `rua=mailto:dmarc@yourdomain.com` is template text. Aggregate reports (which IPs send as your domain, and how much) are mailed to `yourdomain.com`, a domain you don't control.
3. **`celestium.life` SPF includes itself.** Google-sent mail matches `include:_spf.google.com` first and passes. Mail from any other IP recurses into the self-include until the lookup limit, giving `PermError` instead of a clean `softfail`. Remove it.

## Before you start: does anything else send as these domains?

The `frameshift.net` apex points at `24.19.103.182` (looks residential). If a home server, NAS, router or service sends mail **as** `@frameshift.net` or `@celestium.life` without going through Google, it needs its own `include:`/`ip4:` in SPF, or DKIM, before DMARC tightens. Phase 1 below uses `p=none` so the reports reveal any such sender before anything is rejected.

## Rollout order

**Phase 1 (now, safe).** Nothing is rejected yet.

1. `frameshift.net`: Workspace Admin › Apps › Google Workspace › Gmail › **Authenticate email** › select `frameshift.net` › *Generate new record* (2048-bit, prefix `google`). Publish the TXT it shows at `google._domainkey.frameshift.net`, wait about an hour, then click **Start authentication**. Google generates the key, so I can't draft its value.
2. `frameshift.net` SPF and DMARC in monitor mode, plus the two `celestium.life` fixes (change batches below).

**Phase 2 (after about 2 weeks of clean reports).** Set `frameshift.net` to `p=quarantine; pct=100`.

**Phase 3 (after another 2 weeks).** Set `frameshift.net` to `p=reject` (matching `celestium.life`), and optionally SPF `-all`.

Reports go to `celes+dmarc@celestium.life`. Owner decision: no new mailbox (each costs $10/month); Gmail delivers plus-addresses to the existing `celes@` mailbox, and a filter on `to:celes+dmarc@celestium.life` can label or skip-inbox them. Because `frameshift.net` reports go to a different domain, DMARC needs an authorization record on the receiving side, which is included below.

## Route 53 change batches (ready to apply, not applied)

`frameshift.net`, phase 1:

```json
{
  "Comment": "frameshift.net SPF + DMARC monitor (NerveShield NS-ADR-084)",
  "Changes": [
    {"Action": "CREATE", "ResourceRecordSet": {"Name": "frameshift.net.", "Type": "TXT", "TTL": 300,
      "ResourceRecords": [{"Value": "\"v=spf1 include:_spf.google.com ~all\""}]}},
    {"Action": "CREATE", "ResourceRecordSet": {"Name": "_dmarc.frameshift.net.", "Type": "TXT", "TTL": 300,
      "ResourceRecords": [{"Value": "\"v=DMARC1; p=none; rua=mailto:celes+dmarc@celestium.life; adkim=r; aspf=r\""}]}}
  ]
}
```

`celestium.life`, both fixes:

```json
{
  "Comment": "celestium.life SPF self-include + DMARC rua fix (NerveShield NS-ADR-084)",
  "Changes": [
    {"Action": "UPSERT", "ResourceRecordSet": {"Name": "celestium.life.", "Type": "TXT", "TTL": 300,
      "ResourceRecords": [{"Value": "\"v=spf1 include:_spf.google.com ~all\""}]}},
    {"Action": "UPSERT", "ResourceRecordSet": {"Name": "_dmarc.celestium.life.", "Type": "TXT", "TTL": 300,
      "ResourceRecords": [{"Value": "\"v=DMARC1; p=reject; rua=mailto:celes+dmarc@celestium.life\""}]}},
    {"Action": "CREATE", "ResourceRecordSet": {"Name": "frameshift.net._report._dmarc.celestium.life.", "Type": "TXT", "TTL": 300,
      "ResourceRecords": [{"Value": "\"v=DMARC1\""}]}}
  ]
}
```

UPSERT replaces the whole TXT record set at that name. The `celestium.life` apex currently holds only the SPF string. If you add other apex TXT values later (for example a site verification), include them in the same record set.

Apply each batch with:

```sh
aws route53 change-resource-record-sets --hosted-zone-id <ZONE_ID> --change-batch file://batch.json
```

Find `<ZONE_ID>` with `aws route53 list-hosted-zones-by-name --dns-name frameshift.net`.

## Verify after each phase

```sh
dig +short TXT frameshift.net
dig +short TXT _dmarc.frameshift.net
dig +short TXT google._domainkey.frameshift.net
dig +short TXT celestium.life
dig +short TXT _dmarc.celestium.life
```

Then send a test from `celes@frameshift.net` to `celes@celestium.life`. NerveShield's assessment should show DMARC `PASS` with provenance `PROVIDER_REPORTED`, and Gmail's "Show original" should show `spf=pass dkim=pass dmarc=pass`.

## Rollback

Delete the new TXT records, or restore the previous values from the "Current state" table. DNS TTL is 300 s.
