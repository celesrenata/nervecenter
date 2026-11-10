# Phase 4 tasks 4.1 and 4.2: strict observation contract and local-only route resolver

The change tightens `SemanticObservation` so omitted lists, `schema_version` and `model_confidence` are rejected (they were silently defaulted). It also makes confidence a strict float in 0..1 and the two request flags strict bool. It adds `backend/mail_security/route_guard.py`, a library-only resolver that denies unless every leaf of an OmniRoute alias tree is provably local, and records NS-ADR-028. Fixtures only, and nothing in the backend imports the resolver (a static test enforces this). Review is from the diff and the coder's recorded results; suites were not re-run, per instructions.

Watch for: live registry JSON shapes are assumed (confirmed: tolerant parser, unknown shape denies, unverified against cluster). Host-name trust without DNS is a documented limit (confirmed). Working tree holds unrelated tracked build artifacts that must not be committed (confirmed).

**Verdict**: APPROVED

## High-level view

4.1 closes a real gap. The schema's `required` lists were stronger than the model, so a producer could omit fields and still validate. The model now matches, confidence coercion (`"0.5"`, `True`) is gone, and a sync test compares the pydantic JSON schema with the schema file. The schema file itself is untouched.

4.2 is default-deny throughout. The alias name is never trusted. Every combo-ref is walked to a provider/model leaf, and each leaf is classified by the provider's configured base-URL host. Fallback hops count. Cycles, depth and entry bombs, empty combos, missing providers, unknown localities, an unreachable or slow registry and malformed data all produce a typed deny. Unknown aliases (including `hybrid/reader` and `hybrid/coder`) deny. There is no cache, and only enum codes, redacted bounded labels and a digest reach logs.

Constraints hold. There is no network code in the module, no new dependency, no pipeline wiring, and `.agents/`, `.kiro/steering/` and the spec zip show no coder edits. They are untracked with earlier mtimes (2026-11-05 to 06).

<details>
<summary>Issues (4)</summary>

1. **Unrelated artifacts in working tree (confirmed)**: tracked `__pycache__/*.pyc`, `nervcenter.egg-info/PKG-INFO` and `web/node_modules/.package-lock.json` are modified. Exclude them from any commit.
2. **phase3-g3-gate.md edited (confirmed)**: the owner G3 decision was recorded there. This is not a coder-listed file and not a weakening (it says NixOS endpoint hardening is still undecided). The orchestrator should confirm it is intended.
3. **Live registry shape unverified (confirmed)**: `/api/combos` and `/api/providers` shapes are assumed. Unknown shapes deny, so the failure mode is safe, but a contract check against the real OmniRoute is needed before wiring (Task 4.x or deployment).
4. **Residual trust limits (confirmed, documented in NS-ADR-028)**: no DNS, so an in-cluster Service or ExternalName proxying to a cloud is undetected. Check/use TOCTOU is mitigated only by per-call re-resolution and the digest. Alias naming mismatch needs an owner decision before wiring.

</details>

<details>
<summary>Details</summary>

### Required-field tightening of SemanticObservation

Removing `default_factory` and the `schema_version` default makes omission a validation error, as the schema's `required` demands. `Confidence` is `strict=True, allow_inf_nan=False`, so NaN, infinity, numeric strings and bools are rejected; integer 0/1 still pass, which matches schema `type: number`. Existing model tests were updated only to pass `schema_version` explicitly. A future producer that omits empty lists will now be rejected; this is intended and noted by the coder. The contract tests (about 418 lines) cover extra keys at every nesting level, boundary and overlong strings, collection limits, duplicates, enums, confidence, and model/schema sync in both directions, with uniqueItems and the version const covered separately because pydantic cannot express them.

### Fail-closed walk in route_guard

Locality derives only from provider base-URL host classification: loopback, RFC1918/ULA and `.svc`/`.cluster.local` are local. Public names or IPs are remote. Link-local and metadata addresses, CGNAT, bare or numeric-ish names, userinfo URLs, odd schemes and missing URLs are unknown, and unknown denies. If a provider id has several connections, any remote or unknown connection denies it. The optional allowlist is an extra gate only. The walker checks cycles via the stack, re-checks depth on memoized subtrees, and caps total entries. The OmniRoute endpoint itself must be local. `resolve` converts every exception into a deny without exception text, and `dispatch_if_local` never calls `send` on a deny. Tests cover the cloud-fallback hop at first, middle, last and nested positions, cycles, depth and entry bombs, missing provider, unreachable, timeout and malformed registry, secret non-leak, and no caching.

Not tested: a real transport and the live registry contract; DNS-level or proxy-based locality bypass. Both are out of scope for a fixture-only library.

### NS-ADR-028

The row follows the six-column table format (ID, Status, Date, Decision, Evidence, Rollback). It records the alias mismatch, the assumed shapes and the open risks without overclaiming. The date (2026-11-05) predates the G3 approval date (2026-11-09) by a few days, which is cosmetic.

</details>

<details>
<summary>File map</summary>

- `backend/mail_security/models.py`: required fields, strict confidence/bool
- `tests/backend/test_mail_security_models.py`: pass `schema_version` explicitly
- `tests/backend/test_mail_security_observation_contract.py`: new 4.1 contract and sync tests
- `backend/mail_security/route_guard.py`: new resolver
- `tests/backend/test_mail_security_route_guard.py`: new fixture-only tests
- `.kiro/specs/nerve-shield/decisions.md`: NS-ADR-028
- `.kiro/specs/nerve-shield/evidence/phase4-coder-results.md`: coder record
- `.kiro/specs/nerve-shield/evidence/phase3-g3-gate.md`: owner G3 decision recorded

Full diff: `git -C /Users/celes/sources/celesrenata/nervcenter diff`

</details>
