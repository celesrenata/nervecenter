# Static deep inspection — build evidence (NS-ADR-086)

Date: 2026-11-13. Owner G0 answers: PDF parser "if it works leave it" (pikepdf works: kept), placement "recommended" (sidecar), links "scan". Commit `4ef63b1e` plus docs. Live since the same day; existing assessments unchanged, new mail is inspected.

## What was built

- **Sidecar image `nervecenter/inspector`** (`Dockerfile.inspector`): python 3.11-slim with `pikepdf==10.18.1` (qpdf) and `defusedxml==0.7.1`. It contains no NerveCenter code. The api image gets neither library; they are the `inspector` extra in `pyproject.toml`.
- **Analyzer** (`inspector/nerve_inspector/analyze.py`):
  - PDF via the qpdf object model. Compressed object streams and `#xx` names are seen as a reader sees them, and direct dictionaries nested inside objects are walked too.
  - OOXML via zip + `defusedxml`, which refuses DTDs and entities.
  - ZIP via stdlib, recursing into documents and archives to depth 2.
  - Closed code list (20), closed incomplete reasons; link **hosts** only.
- **Per-sample child** (`child.py`):
  - started with rlimits: CPU 8 s, address space 768 MiB, file size 0, no core;
  - killed at a 10 s wall clock;
  - warms every lazy import, reads the sample, then **seals its descriptors** (closes everything above 2 and sets `RLIMIT_NOFILE=3`). After that no socket, file or pipe can be created.
- **Server** (`server.py`): UNIX socket, framed bounded input, 2 concurrent children. A spawn failure or overrun is a typed `INCOMPLETE`.
- **api client** (`backend/mail_security/static_inspection.py`, the second local-IPC module next to `clamav.py`, guard-tested to use only `open_unix_connection`):
  - strict closed-schema validation; an invalid or unreachable sidecar gives `INCOMPLETE`, never clean;
  - codes become `ATTACHMENT` findings (HIGH/MEDIUM/LOW), so structure alone never reaches RESTRICTED;
  - document link hosts go through the existing confusable/typosquat checks.
- **Wiring:** `collect_attachment_results(deep_inspector=...)`; the worker retains payloads when either the scanner or the inspector is configured. Kill switch: `NERVE_MAIL_SECURITY_STATIC_INSPECTION_SOCKET` (ConfigMap). Unset means PDF/ZIP/OOXML attachments record `ATTACHMENT_DEEP_INSPECTION_NOT_RUN`.

## Deviations from the G0 draft (honest)

1. **Network isolation is per process, not per pod.** A sidecar shares the api pod's network namespace, so a NetworkPolicy can't isolate it. The control is the descriptor seal, proven in the production image with networking **enabled**: before the seal 1.1.1.1:53 was reachable; after it `socket()`, `connect` and `open()` all fail with `EMFILE`. The child also tries `unshare(CLONE_NEWUSER|CLONE_NEWNET)`. That is refused under the restricted profile (live: `netns=False` on 47/47), as expected.
2. **No policy@6.** The existing policy already maps MEDIUM/HIGH `ATTACHMENT` findings to REVIEW and only HIGH `MALWARE` to RESTRICTED. New codes use `ATTACHMENT`, so no rule change was needed. `policy_version` is unchanged; findings carry `detector_version=attachment-deep@1`.
3. **`PDF_SINGLE_LINK_LURE` became `PDF_LARGE_LINK_AREA`** (LOW). pikepdf has no reliable text extraction, so the lure signal is a page whose link annotations cover at least 25% of its area.
4. **Pod hardening beyond the spec:** `automountServiceAccountToken: false` for the whole api pod (verified: nothing in `backend/` calls the Kubernetes API; the token path is absent live).

## Proof

| Check | Result |
|---|---|
| Tests | `tests/inspector` 62 (planted structures for all formats, benign quiet, bounds, fuzz, hostile sidecar replies, kill switch, wiring, sealed child parses every format) + guard test; full suite 8798 passed |
| Regression caught | in the Linux image, the sealed child first failed on every ZIP: `zipfile` loads the `cp437` codec lazily. Fixed by a warm-up before sealing; a test pins it |
| Hardened local container | `--read-only --cap-drop ALL --no-new-privileges --user 10001`: JS in compressed object streams, external template, double extension found; 40 MiB zip bomb gives `TOO_LARGE` |
| Live pod | 3/3 ready; socket `srwxrwx---` shared by uid 10001; inspector env has no secrets; root FS read-only; no SA token |
| Live dry run (aggregate) | 9 messages / 47 documents (36 PDF, 11 OOXML), 0 incomplete, 0 structural codes, 8 link hosts, **1 message gains a review reason** (`ASCII_BRAND_TYPOSQUAT` on a document link; target ≤ 1) |

## Open

- The one live document-link typosquat hit is unreviewed (aggregate only). `ASCII_BRAND_TYPOSQUAT` is the noisiest rule overall (339 findings live), so it may be a false positive.
- Earlier, the descriptor count showed 56 PDF/ZIP attachments, but 47 carried a payload starting with PDF/ZIP magic. The rest are claimed-type-only or empty parts, and the existing static checks still cover them.
- Stored assessments were not re-run. Re-running needs a reassessment (new revision); available on request.
