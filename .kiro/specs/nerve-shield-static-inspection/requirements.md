# Static deep inspection of attachments — spec (G0 approved 2026-11-13; built, G1 pending)

Origin: detonation G0 (`nerve-shield-detonation/g0-gate.md`, D-4). Owner: "go ahead" (2026-11-13). This is the **intermediate step instead of detonation**: deeper analysis of attachment structure, **without executing anything**, in an isolated container with no network and no secrets. Nothing is built.

## 1. Problem

ClamAV gives a signature verdict only, and a clean result is explicitly not safety (`ENGINE_CLEAN_VERDICT_NOT_SAFETY`). Phishing and malware attachments usually aren't signature-known. What gives them away is structure:
- a PDF with JavaScript, a launch action or an embedded file;
- an Office file with macros, external template injection or an embedded OLE object;
- a ZIP with encrypted entries, executables, double extensions, path traversal or a nested archive;
- a lure document whose only content is a link.

## 2. Measured need (live, aggregate only, 2026-11-13)

| | Count |
|---|---|
| Messages / with attachments | 348 / **13** |
| `application/pdf` | 45 |
| `application/zip` | 11 |
| OOXML (`.xlsx` 2, `.docx` 1) | 3 |
| Images / AMP HTML | 12 / 4 |
| Legacy OLE (`.doc`/`.xls`) | 0 |

Volume is tiny (about 4% of messages), so latency and throughput are not constraints. Coverage of PDF + ZIP + OOXML handles 100% of the non-image attachments seen.

## 3. Requirements

1. **SI-1 No execution.** No macro, script, JavaScript or embedded code is ever run, emulated or rendered. Only parsers read bytes. No external tool is invoked with the sample as an argument except the reviewed parsers below.
2. **SI-2 Isolation.** The analyzer runs as its own container, a sidecar of the api pod like clamd (NS-ADR-065), reached over a UNIX socket. As a sidecar it shares the pod's network namespace, so isolation is per process: each parse child seals its descriptors (`RLIMIT_NOFILE=3`) before reading bytes, so it cannot create any socket (see `g1-evidence.md`). Container settings: no egress, no service account token, a read-only root filesystem, non-root, all capabilities dropped, seccomp `RuntimeDefault`, and no secrets or env from `nervecenter-env`. The sample goes in over the socket and only a bounded JSON report comes out.
3. **SI-3 Bounded.** Per-sample hard limits:
   - input bytes;
   - decompressed bytes (zip-bomb guard, ratio and absolute);
   - archive depth (2) and entry count;
   - PDF object count;
   - wall clock (10 s);
   - memory (cgroup limit).

   Exceeding any of them gives `INCOMPLETE` with a typed reason, never clean.
4. **SI-4 Typed findings only.** The report is a closed schema of finding codes, counts and booleans, for example:
   - PDF: `PDF_JAVASCRIPT`, `PDF_LAUNCH_ACTION`, `PDF_EMBEDDED_FILE`, `PDF_OPENACTION_URI`, `PDF_SINGLE_LINK_LURE`, `PDF_ENCRYPTED`;
   - Office: `OOXML_MACRO_PROJECT`, `OOXML_EXTERNAL_TEMPLATE`, `OOXML_EMBEDDED_OLE`, `OOXML_DDE_FIELD`;
   - ZIP: `ZIP_ENCRYPTED_ENTRY`, `ZIP_EXECUTABLE_ENTRY`, `ZIP_DOUBLE_EXTENSION`, `ZIP_PATH_TRAVERSAL`, `ZIP_NESTED_ARCHIVE`;
   - any parser: `PARSE_FAILED`.

   No filenames, text, URLs or extracted content leave the analyzer. Link **hosts** may be returned for the existing link inspections, run through the same host regex as the observer packet.
5. **SI-5 Untrusted report.** The api validates the report strictly, the same way it treats model output (R-022.1). A report can only **add** findings or mark `INCOMPLETE`; it never clears a message or lowers a disposition.
6. **SI-6 Policy.** New findings feed the deterministic policy as a new version (policy@6), SHADOW first. A REVIEW-impact code (for example a macro, PDF JavaScript, an encrypted ZIP or an executable in a ZIP) adds a review reason; it never auto-acts.
7. **SI-7 Opt-in and kill switch.** `NERVE_MAIL_SECURITY_STATIC_INSPECTION` is off by default. Unset means not run and recorded as `NOT_EVALUATED`, never clean.
8. **SI-8 Evidence.** Reports are stored as metadata (codes and counts) only. Sample bytes stay in the existing encrypted object store (NS-ADR-062) and are not copied.

## 4. Measurable acceptance

| Metric | Target |
|---|---|
| Synthetic malicious-structure corpus (built in tests: macro-bearing OOXML, PDF with `/JavaScript` and `/Launch`, encrypted and traversal ZIPs, zip bomb) | 100% of planted structures reported |
| Benign corpus (synthetic plus the 13 live messages, SHADOW) | REVIEW added by SI on at most 1 live message without owner confirmation |
| Isolation probes from the analyzer container | egress denied, no SA token, root FS read-only, no env secrets |
| Bounds | zip bomb and oversized PDF → `INCOMPLETE` within the time limit, container memory stays under the limit |
| Live | every attachment-bearing message gets a report or a typed `INCOMPLETE` |

## 5. Owner decisions (G0)

- **D-1 Parser libraries** (each pinned, reviewed and vendored into the analyzer image only, never the api):
  - ZIP: stdlib `zipfile`.
  - PDF: `pikepdf` (qpdf, C++, robust) or `pypdf` (pure Python, simpler, slower, more parser edge cases)?
  - OOXML: stdlib zip + XML with `defusedxml`; `oletools` only if legacy OLE appears (0 seen).
- **D-2** Sidecar (like clamd, recommended) or a separate pod? A separate pod needs network ingress, which weakens SI-2.
- **D-3** Should the analyzer also return link hosts from PDFs and Office documents, so the existing link checks can cover them?
