# Static deep inspection — tasks

- [x] 0.1 **G0 gate — STOP.** Owner decides D-1 (PDF parser), D-2 (sidecar vs pod), D-3 (link hosts). _Answered 2026-11-13: pikepdf, sidecar, scan links._
- [x] 1.1 Report contract: closed Pydantic schema (finding codes, counts, `INCOMPLETE` reasons) plus a strict api-side validator; hostile-report tests. **Req:** SI-4, SI-5.
- [x] 1.2 Analyzer service (separate image): UNIX-socket server, framed bounded input, per-sample limits (bytes, ratio, depth, entries, objects, 10 s, memory). **Req:** SI-1, SI-3.
- [x] 1.3 ZIP inspector (stdlib): encrypted, executable, double extension, traversal, nested, bomb. **Req:** SI-4.
- [x] 1.4 PDF inspector (per D-1): JavaScript, Launch, OpenAction/URI, embedded file, encrypted, single-link lure. **Req:** SI-4.
- [x] 1.5 OOXML inspector (`defusedxml`): vbaProject, external template rels, embedded OLE, DDE fields. **Req:** SI-4.
- [x] 1.6 api client in `backend/mail_security` (UNIX socket only; add it to the network-free exemption list next to `clamav.py`) plus the worker seam behind `NERVE_MAIL_SECURITY_STATIC_INSPECTION`. **Req:** SI-7, SI-8.
- [x] 1.7 ~~Policy@6~~ not needed: existing rules already map MEDIUM/HIGH ATTACHMENT to REVIEW (see g1-evidence.md). Was: Policy@6: SHADOW review reasons for REVIEW-impact codes; never lowers. **Req:** SI-6.
- [x] 1.8 Deployment: sidecar in the api pod (read-only root FS, non-root, no caps, seccomp, no env/secrets, shared socket `emptyDir`); isolation probes. **Req:** SI-2.
- [x] 1.9 Synthetic malicious/benign corpus tests; live SHADOW run over the 13 attachment-bearing messages (aggregate only). **Req:** §4.
- [x] 1.10 **G1 gate — STOP.** (evidence: `g1-evidence.md`) _Owner "reassess" 2026-11-13: kept enabled; reassessed (REVIEW 9 -> 10)._ Evidence and owner approval to keep the feature enabled.
