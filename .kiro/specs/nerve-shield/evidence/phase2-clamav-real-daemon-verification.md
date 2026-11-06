# ClamAV client verified against a real clamd

**Date:** 2026-11-06. **What ran:** the repo's `ClamdScanner` (commit `b261991`) against a real `clamd` in a throwaway local container, bound to `127.0.0.1` only (host port 53310), removed afterwards.
**Image:** `clamav/clamav-debian:stable`, digest `sha256:c21aa18cc0a358678255a3dfb343a4555b58f71b97d7b869df8fdf7ea0a5b4b1` (the plain `clamav/clamav:stable` has no `linux/arm64` manifest; the Debian variant is multi-arch). Engine ClamAV 1.5.4.
**Not verified:** a clamd inside the cluster; the amd64 build of the image; the cluster deployment shape (no manifest is applied); any real attachment traffic.

## Results (all VERIFIED, observed output)
| Check | Result |
| --- | --- |
| `PING` | PONG |
| `VERSION` parsing of the real reply | engine `1.5.4`, signature DB `28174`, date `2026-11-05 06:26:24 UTC` |
| clean payload | `CLEAN_ENGINE_VERDICT`, recorded as `engine=clamav ver=1.5.4-sig28174-20261105` |
| EICAR standard antivirus test string (in memory only) | `DETECTED`, signature code `Eicar-Test-Signature` |
| max signature age set to 1 s (forces "stale") | clean became `UNAVAILABLE` (not clean); a detection was still reported as `DETECTED` |
| payload over the client cap (17 MiB) | `TOO_LARGE` |
| 16 concurrent scans (8 EICAR, 8 clean) | 8 `DETECTED`, 8 `CLEAN_ENGINE_VERDICT` |
| empty payload | `NOT_RUN` |
| closed port | `UNAVAILABLE` (not clean) |
| deadlines 0.1 ms, 1 us, 0, negative, 2 ms | `TIMEOUT` |

## Findings from real behavior
1. **Staleness rule fired correctly on real data.** First run: the image's bundled signatures were dated Nov 2 (about 3.8 days old at test time), so the client's 3-day default refused to call anything clean. This was the safety rule working, not a bug.
2. **clamd does not pick up a freshly downloaded database on its own at startup.** `freshclam` updated `daily` to version 28174, but clamd had not finished starting, logged "Clamd was NOT notified", and kept serving 28172 until reloaded (its self-check is every 600 s). The client reads the version clamd actually has loaded, which is the correct thing to judge. A real deployment needs the updater to trigger clamd reloads and an alert on signature age.
3. **Deadlines are cooperative.** A 0.5 ms deadline returned a completed clean verdict after 3.7 ms, because asyncio only checks the timeout at suspension points. The verdict was real, only late by milliseconds. For realistic deadlines (seconds) this has no effect.

## Decisions affected
NS-ADR-020 previously said NOT VERIFIED AGAINST REAL CLAMD. It is now verified against clamd 1.5.4 over loopback TCP. Still unverified: the unix-socket path against a real daemon, stream limits configured on clamd, very large payloads, and the in-cluster deployment.
