# Spec: Celestium SSO + Mobile Auth Integration

**Feature ID:** sso-integration
**Created:** 2026-09-30
**Status:** Active (PARTIAL — S101 backend auth done: signed JWT + device pairing + approval binding in `backend/api/auth.py`, test-verified. S102 web SSO wiring, S103 Apple OIDC RP (PKCE) + Keychain + biometric gate (still `InMemoryCredentialStore` only — `apple/Shared/Sources/NerveAuth/CredentialStore.swift:16`), S104 Authentik provider + deploy env open. 2026-10-26)
**Branch target:** main (repo root: nervcenter)
**Upstream spec:** [prompot.md](../../prompot.md) §70–72 (Local Connectivity, Authentication, Sensitive Approval), §48–51 (Web UI), §95 (workstreams)

## Problem

All three client planes (Web, iPhone, Watch) currently authenticate against a toy auth
surface that cannot ship:

- `backend/api/auth.py` is an in-memory token store with a hardcoded
  `admin/password` user, opaque `secrets.token_urlsafe` tokens (not JWT, not
  signed), and state that is wiped on every restart. The web `LoginView`
  advertises those default credentials.
- The user's new rules require all work to sit behind `sso.celestium.life`.
  That host is an **Authentik** instance (verified live 2026-09-30: `GET /` →
  302 to `/admin/`, authentik-style JSON 404s, per-application OIDC discovery
  paths). **No `nervecenter` OIDC provider/application exists there yet** —
  discovery for candidate slugs returns `{"error":"Unable to find matching
  target resource method"}`.
- prompot.md §71 requires device pairing (challenge → credential → Keychain)
  for iPhone/Watch and §72 requires biometric-bound approval of exact
  `PlannedChange`s. `apple/Shared/Sources/NerveAuth` has biometric primitives
  but no OIDC/pairing/Keychain credential flow.
- The web UI never wired the login result into API calls (no Authorization
  header on fetches), so "behind SSO" would silently break every view.

## Goals

1. Every client surface (Web, iPhone, Watch, CLI, MCP) obtains identity from
   `sso.celestium.life` (Authentik OIDC) with a local-admin fallback only for
   break-glass, and the fallback is no longer the default path.
2. The backend issues and validates **signed JWTs** (RS256 via Authentik JWKS
   or HS256 shared secret fallback), persists sessions in PostgreSQL, and
   never loses credentials on restart.
3. Device pairing per §71 works end-to-end: initiate → challenge → confirm →
   device credential → Keychain (via `NerveAuth`), and paired devices can
   biometric-approve an exact `PlannedChange` per §72.
4. The web UI applies the new web-quality rules (design system, responsive
   320px reflow, contrast, focus, working states) — see
   [specs/web-quality-audit/spec.md](../web-quality-audit/spec.md).
5. All created work is audited with concrete evidence (curl matrix, Playwright
   matrix incl. the SSO round-trip, `xcodebuild` gate) and recorded in
   [docs/implementation-status.yaml](../../docs/implementation-status.yaml)
   and [tasks.md](./tasks.md).

## Scope

**In scope:**
- Authentik integration: OIDC RP (PKCE, authorization-code flow) on the web
  backend; `/api/auth/sso/config` discovery endpoint; JWKS-based token
  validation; Authentik provider bootstrap via Authentik admin API when
  credentials are available, otherwise a documented manual step.
- Backend auth rewrite: signed JWT (access + refresh), PostgreSQL-backed
  session/device stores, device pairing endpoints (§71), approval binding
  (§72) wired to the existing `approvals`/`planned_changes` groups.
- Web: SSO login surface (redirect-based, PKCE), session bootstrap, 401
  handling on all fetches, break-glass local login kept but demoted.
- Apple: `NerveAuth` OIDC RP + Keychain credential storage + biometric gate
  for approvals; `NerveModels` regenerated from the live OpenAPI contract.
- Deploy: ingress/traefik wiring so `nervecenter.celestium.life` sits behind
  the SSO realm (forward-auth or app-level redirect), TLS intact.
- Audit: curl + Playwright matrices, mobile build evidence, defect log.

**Out of scope:** real iPhone/Watch device testing (per
[specs/nervecenter-deploy/spec.md](../nervecenter-deploy/spec.md) constraints),
multi-tenant user management, provisioning Authentik users/roles beyond the
`nervecenter` application, cloud inference integration.

## Constraints

- Registry/cluster unchanged: `registry.celestium.life` Harbor, K3s gremlin
  cluster, traefik ingress, `nervecenter.celestium.life` host (LAN-only DNS
  until T207 resolves — SSO must not depend on public reachability).
- Authentik at `sso.celestium.life` is authoritative; we must not weaken its
  configuration. Provider creation requires Authentik admin credentials
  (unknown at write time — see Assumptions).
- Web must keep working in mock mode (`VITE_API_BASE` unset) for the e2e
  harness; SSO code paths must degrade to the local login in mock mode.
- Apple work must compile on the M5 Max via `xcodebuild -scheme NerveShared`
  (AC009 gate already exists).
- New rules: web-quality guidance applies to any new login/SSO surfaces
  (visible labels, actionable errors, pending states, no duplicated loud
  buttons, contrast ≥ 4.5:1, focus visible).

## Assumptions

- Authentik admin credentials can be obtained from the cluster (secret in the
  `authentik`/`sso` namespace) or an existing admin account; if neither is
  available, provider creation is a documented manual step and the backend
  ships with a self-contained OIDC client that works the moment the provider
  exists.
- The Authentik provider slug will be `nervecenter` (matching the namespace
  convention); redirect URI `https://nervecenter.celestium.life/auth/callback`
  (dev: `http://localhost:3000/auth/callback`).
- PKCE (S256) + authorization-code flow; no implicit flow, no tokens in URLs
  beyond the code.
- Keychain access on device credentials is available in the simulator build
  path; biometric code paths are unit-tested, not device-tested.

## Acceptance Criteria

- AC-S01: `GET /api/auth/sso/config` returns 200 JSON with `issuer`,
  `client_id`, `authorization_endpoint`, `token_endpoint`, `userinfo_endpoint`,
  `jwks_uri`, `enabled` — issuer default
  `https://sso.celestium.life/application/o/nervecenter/`; when the Authentik
  provider is live, `enabled` is `true` and the endpoints resolve to real
  Authentik URLs (curl-verified).
- AC-S02: Backend access tokens are signed JWTs (RS256 verified against
  Authentik JWKS, or HS256 with `NERVE_JWT_SECRET`), with `exp`/`iat`/`typ`
  claims; validation rejects expired, wrong-type, and tampered tokens
  (pytest: ≥3 negative-path tests).
- AC-S03: Sessions/devices persist in PostgreSQL (alembic migration); API
  restart does not invalidate a valid JWT; no `admin/password` default user
  remains in code (grep-verified) — break-glass local login requires
  `NERVE_LOCAL_ADMIN` env to be enabled and logs a warning when used.
- AC-S04: Device pairing per §71: `POST /api/auth/pairing/initiate` →
  challenge (nonce, expires_at ≤ 5 min), `POST /api/auth/pairing/confirm`
  with the challenge + device attestation → device credential (JWT bound to
  `device_id`); ≥2 pytest tests; the flow is exercised live via curl.
- AC-S05: Approval binding per §72: an approval is bound to `device_id` +
  `nonce` + `expires_at` and to an exact `planned_change_id`; any modification
  of the bound change invalidates the approval (pytest negative-path test).
- AC-S06: Web login surface: "Sign in with Celestium SSO" (PKCE code flow,
  redirect through Authentik, callback exchange) plus a demoted break-glass
  form; after login every API fetch carries `Authorization: Bearer …` and a
  401 redirects to `/login`; Playwright verifies the login → authorized
  fetch → view-data round-trip with 0 console errors.
- AC-S07: `apple/Shared/Sources/NerveAuth` implements the OIDC RP
  (PKCE, authorization-code, token exchange), Keychain storage of the
  device credential, and a biometric gate guarding approval submission;
  `xcodebuild -scheme NerveShared build` is green on the M5 Max.
- AC-S08: `docs/implementation-status.yaml` gains a `sso-integration`
  workstream with evidence links (commands + results), and
  [tasks.md](./tasks.md) carries the verification log; every claim is backed
  by command output or a screenshot, never by assertion.
- AC-S09: The deployed stack sits behind the SSO realm: unauthenticated
  requests to `nervecenter.celestium.life` are redirected to
  `sso.celestium.life` (or the app-level login), and the TLS chain for both
  hosts is valid (curl-verified with `-D -`).

## Verification

Every AC maps to a task in [tasks.md](./tasks.md) with its exact command or
Playwright step. Evidence lives in `docs/implementation-status.yaml` and the
tasks.md verification log; gaps are stated explicitly, never papered over.
