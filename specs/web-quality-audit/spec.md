# Spec: Web UI Quality Audit + SSO Surface

**Feature ID:** web-quality-audit
**Created:** 2026-09-30
**Status:** Active (PARTIAL — W101 semantic token system + SVG icon set + emoji-navigation removal landed; W102 login surface partly (break-glass copy removed, SSO entry pending S102); W103 new responsive/focus/login e2e (AC-W06/W07) still to be verified. 2026-10-26)
**Branch target:** main (repo root: nervcenter)
**Upstream:** [prompot.md](../../prompot.md) §48–51 (views), §71 (auth), new rules `.roo/rules/20-web-design.md` + `.roo/rules/20-web-quality.md`
**Companion:** [specs/sso-integration/spec.md](../sso-integration/spec.md) (AC-S06 login surface)

## Problem

The deployed web UI passes AC005 (10 views render, 0 console errors) but fails
the new web-quality rules on concrete, verified points:

- **Emoji navigation** (`Layout.tsx` `icon: '📊'` etc.) — rule §5: emoji are
  not a professional navigation icon system.
- **Fixed `w-64` sidebar**, no mobile collapse: at 360–390 px the sidebar
  consumes the viewport (rule §8: adapt navigation on small screens).
- **No visible focus indicators** anywhere; keyboard navigation unverified.
- **Default-gray Tailwind palette** with ad-hoc dark: variants — no semantic
  tokens (canvas/surface/accent/status); surfaces not measured for contrast.
- **`LoginView` advertises "Default credentials: admin / password"** — both a
  security defect (S101 removes the default user) and a design defect.
- **Dependency inconsistency:** `web/package.json` mixes `tailwindcss ^3.4.7`
  with `@tailwindcss/postcss ^4.3.3` — a version conflict that can silently
  break class generation (rule §7: verify build integration).
- Placeholder text used as examples; error/pending states exist but the
  login error is a single non-field-associated message.

## Goals

1. The web UI complies with the new rules: deliberate direction, semantic
   token system, real icon system, responsive 320–1440 behavior, visible
   focus, working states — without changing routes, data contracts, or the
   10-view IA that AC005 gates on.
2. The login surface becomes the SSO entry point (per sso-integration AC-S06)
   and meets the form rules (persistent labels, connected error text,
   pending state, no duplicated loud buttons).
3. The audit is evidence-backed: screenshots at 360/768/1440 px, console
   logs, computed-style checks for contrast/focus — never self-awarded.

## Scope

**In scope:** `web/src/**` (tokens, Layout shell, LoginView/SSO surface,
views' shared components), `web/package.json` (dependency reconciliation),
Playwright e2e additions (responsive + focus + login flow), defect fixes.

**Out of scope:** backend changes (S101), `web/Dockerfile` (T206 owner),
route/IA changes, framework replacement (React+Vite+Tailwind is sound).

## Constraints

- Keep the 10 views and their routes byte-compatible with AC005 gates
  (`web/tests/e2e/views.spec.ts` must keep passing).
- Mock mode (`VITE_API_BASE` unset) must keep working for the e2e harness.
- Tailwind version: resolve to one major (3.x) — remove the stray v4
  postcss plugin; verify class generation after the change.
- Contrast: WCAG AA (4.5:1 normal text, 3:1 large/control boundaries).

## Acceptance Criteria

- AC-W01: Semantic token system exists in `web/src/index.css` (canvas,
  surface, elevated, primary/secondary text, divider, control border,
  accent, text-on-accent, focus, status colors) and Layout + Login + views
  consume it; no default-gray ad-hoc pairs remain in `Layout.tsx`.
- AC-W02: Navigation icons come from a consistent icon set (inline SVG,
  coherent size/stroke); emoji removed; decorative icons `aria-hidden`,
  functional ones named.
- AC-W03: Responsive shell: at 360–390 px the sidebar collapses to a
  compact header + accessible nav trigger; no page-level horizontal
  overflow at 320 px; verified by Playwright screenshots at 360/768/1440.
- AC-W04: Keyboard: visible focus rings on all controls, logical order,
  no traps; Playwright `Tab` walk captures focus states.
- AC-W05: Login/SSO surface per sso-integration AC-S06 with form rules:
  persistent labels, error text programmatically associated, pending state,
  single primary action, break-glass demoted; no default-credential copy.
- AC-W06: `npm run build` green after dependency reconciliation (tsc + vite,
  class generation verified); e2e suite (`npm run e2e`) green including new
  responsive/focus/login tests; console errors 0 on all views.
- AC-W07: Evidence recorded in `docs/implementation-status.yaml` and
  [tasks.md](./tasks.md) verification log: screenshots (paths), console
  logs, contrast measurements, and any unverified gaps stated explicitly.

## Verification

Each AC maps to a task in [tasks.md](../sso-integration/tasks.md) (W101–W103
merged into the sso-integration board) with exact commands and Playwright
steps; screenshots stored under `web/e2e-artifacts/`.
