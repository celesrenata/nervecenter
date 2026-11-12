# Task 7.6 — Explicit confirmation workflow results

Date: 2026-11-11. Status: done, local commit only, not deployed. Adds migration 020 (`020_mail_confirmations`, 22 characters, which fits VARCHAR(32)). No flag change.

**Owner decision:** "independently authenticated approval" means a *recent sign-in* within 10 minutes (option 1, NS-ADR-058).

## What changed

**`backend/mail_security/confirmations.py`** (pure, `mail-confirmation@1`):

- **Closed action set per proposal kind:**

  | Kind | Low risk | High risk |
  |---|---|---|
  | calendar | `resource.write:calendar.events` | `resource.disclose:calendar.invites` (EXTERNAL_DISCLOSURE) |
  | bill | `resource.write:bills.reminders` | `resource.pay:bills.payments` (PAYMENT) |
  | relationship | `resource.write:relationships.followups` | `resource.send:messages.outbound` (OUTBOUND_MESSAGE) |

  Anything else is `ACTION_NOT_ALLOWED`.
- **Explicit confirmation for every action**, low risk included. Confirmations expire: the default is 10 minutes, the allowed range is 1–60, and only human principals can request, approve or reject.
- **High risk needs two more things:**
  - a step-up whose `auth_time` is within 600 seconds (60 seconds of clock skew tolerated);
  - `sender_verified=True`, the approver's attestation that they checked the sender through a channel they already trust.
- **The approval is the existing PDP `NCApproval`.** It binds:
  - the SHA-256 of the execution payload `{proposal, action}`;
  - the proposal's `nc://` resource URI;
  - `{"action": <action>}`;
  - the confirmation's expiry.
- **`authorize_execution`** returns `NOT_APPROVED` unless there is an APPROVED, unexpired confirmation for this exact proposal and action. Otherwise it calls the unchanged PDP with the approval and the exact payload. Execution actions are default-deny, so only a verified binding can allow them.
- **Single use and audit.** `consume` makes the confirmation single-use, and every transition (REQUESTED, APPROVED, REJECTED, EXPIRED, CONSUMED) appends a codes-only audit entry.
- **Rejection** takes a coded reason only (NOT_WANTED / LOOKS_SUSPICIOUS / WRONG_DETAILS) and never feeds training.

**`backend/api/step_up.py`**:

- **SSO (production, Keycloak).** The access token's `iat` is not accepted, because it is minted silently from an existing SSO session. The browser has to present a fresh `id_token`, and the server requires all of the following:
  - it verifies against the issuer JWKS (signature, issuer, audience);
  - `typ == "ID"`;
  - `auth_time` is within the window and not in the future;
  - it matches the caller's `sid` (or `sub`).

  Its `jti`, or a token digest, is the proof id.
- **Local break-glass.** The current `typ=access` token self-proves via `auth_time` or `iat`, since there is no refresh path. In production, break-glass login is not enabled.

**Durable store:** `mail_security_confirmations` (Alembic 020, additive, reversible). Its indexes are:
- unique `confirmation_id`;
- `(account_scope, status)`;
- unique `step_up_proof_id`, so a proof can't approve two confirmations.

**API** (`backend/api/endpoints/mail_security_confirmations.py`):

| Route | Behavior |
|---|---|
| `POST /confirmations` | Re-validates the proposal. Then re-reads the assessment in the caller's own scope and requires it to be SAFE_TO_PROCESS with no verification advice, recomputed from the stored findings (`409 not_safe`). The client's claim is never trusted. |
| `POST /confirmations/{id}/approve` | `401 step_up_required`, `409 sender_verification_required` / `not_pending` / `step_up_reused`. |
| `POST /confirmations/{id}/reject` | Coded reason only. |
| `GET /confirmations` | The caller's confirmations. |

All bodies use `extra=forbid`. Device credentials get 403, and other accounts get 404. No execution endpoint exists.

## Verification

| Check | Result |
|---|---|
| `tests/backend/test_mail_security_confirmations.py` (new) | 39 passed |
| Full suite `uv run pytest tests/ -m "not integration" -q` | 8427 passed, 1 failed (known date-dependent `test_routines`), 3 skipped |
| Migration head tests (`020_mail_confirmations`), the 32-character revision guard, and schema registry | pass |
| `ruff` | clean on new files |

**Done criterion — a malicious instruction cannot escalate to approval:**
- A low-risk approval can't be used to pay. Forcing the action field gets `approval_rejected` from the PDP binding.
- Tampering with the amount breaks the payload hash.
- Out-of-set actions are refused: `resource.pay:*`, access grants, deletes, and send on a bill proposal.
- Machines can't request, approve or execute.
- The API refuses bodies carrying `approved` / `capability` / `note` (422) and a tampered `requested_capability` (`invalid_proposal`).
- A RESTRICTED assessment, or a SAFE one with impersonation advice, gives `not_safe`.
- A reused step-up proof gives `step_up_reused`.

**Payments, sending and disclosure stay locked:**
- All six actions are refused without a confirmation, and the PDP default-denies each execution action.
- A high-risk action fails without a step-up, with a stale step-up (30 minutes), or without sender attestation, and passes only with a fresh step-up plus attestation.
- Expiry, the not-pending state, consumed-is-final, and rejected-is-final all hold, and the audit sequence is checked.

**Step-up verifier:**
- A fresh, matching ID token passes.
- It fails when the token is stale, has no `auth_time`, is an access token, belongs to another session, has nothing to bind to, has a future `auth_time`, or fails signature verification.
- An SSO access token alone never proves step-up.
- A local token proves step-up only when it is recent.

**Clock skew:** a test caught `approve` rejecting an `auth_time` slightly ahead of the server clock while the verifier allowed 60 seconds. They now share `STEP_UP_CLOCK_SKEW`.

## Not verified / limits

- **No UI and no live step-up yet.** Nothing produces proposals, and no screen requests or approves them. The SSO step-up path (re-running OIDC with `max_age` and posting the `id_token`) was tested with a mocked JWKS verifier, not against live Keycloak. Whether Keycloak issues `auth_time` in the ID token on a `max_age` request is standard OIDC behavior but has not been confirmed live.
- **No executor.** Calendar, bill, payment and send actions are not implemented. 7.6 proves they are locked; `consume` is ready for an executor.
- **The sender attestation is a human checkbox.** It records the claim; it cannot verify the out-of-band contact itself.
