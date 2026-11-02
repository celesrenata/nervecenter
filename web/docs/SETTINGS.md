# NerveCenter Settings

The Settings route (`/settings`) exposes browser-local preferences grouped by the feature set they shape. The panel uses a top-centered category tab strip; only the active category's panel is rendered.

## Scope and storage

- Preferences persist in `localStorage` under the key `nerve-settings`.
- They are browser-local and are not synced to the server or to another device.
- Appearance values are applied to `<html>` before first paint; workflow values are read by their feature screens when those screens initialize.
- "Reset to defaults" restores the defaults below and persists them.
- Corrupt or missing values fall back per key rather than discarding the whole record.

## Categories and keys

| Category | Setting | Key values | Default | Consumed by |
| --- | --- | --- | --- | --- |
| Appearance | Theme | `system`, `light`, `dark` | `system` | `ThemeContext` applies `data-theme` on `<html>`. |
| Appearance | Accent color | `blue`, `emerald`, `violet`, `amber`, `rose` | `blue` | `SettingsContext` sets `--accent`, `--accent-hover`, `--accent-soft`, `--accent-contrast`, and `--ring`. `blue` clears overrides and uses the theme accent. |
| Appearance | Text size | `sm`, `md`, `lg` | `md` | `SettingsContext` sets the root font size (14.4 / 16 / 17.6 px). |
| Appearance | Motion | `system`, `reduce`, `full` | `system` | `SettingsContext` sets `data-reduced-motion`; `index.css` suppresses animation and transitions. |
| Workflow | Default briefing | `morning`, `midday`, `evening` | `morning` | `BriefingView` seeds its kind selector from this value. |
| Workflow | New incident severity | `low`, `medium`, `high`, `critical` | `high` | `IncidentsView` seeds and resets the report form with this value. |
| Workflow | Home view | `today`, `overview`, `now` | `today` | `HomeView` in `App.tsx` renders the selected feature at `/` without changing the URL. |
| Data & privacy | Wellness trend window | `7`, `30`, `90` days | `30` | `WellnessView` seeds its trends window and re-fetches on change. |
| Inference | Ask default model | `""` (service default) or a model id from `GET /api/inference/available` | `""` | `AskView` prefills its advanced model picker from this value and syncs changes back. |
| Inference | Ask default provider | `""` (service default) or a provider id from `GET /api/inference/available` | `""` | `AskView` prefills its advanced provider picker from this value and syncs changes back. |

Theme is owned by `ThemeContext`; the other keys are owned by `SettingsContext`. Both providers are mounted in `main.tsx` before the app renders.

## Feature-owned and server-owned controls

These controls are intentionally not duplicated in Settings because their state lives with the feature or the server:

| Feature set | Controls | Surface |
| --- | --- | --- |
| Today / attention | Low Energy Mode and attention actions | `/` (Today) and `/now` |
| Communications | Mail-connector registration/verification, relay capture, message filters | `/connections`, `/email`, `/messages`, `/voicemail` |
| Calendar and routines | Calendar sync/import and routine schedules | `/calendar`, `/routines` |
| People and commitments | VIP contacts, relationship cadence, waiting-on follow-ups | `/contacts`, `/people`, `/waiting-on` |
| Money | Bill recurrence, confirmation, and calendar | `/bills` |
| Wellness | Collection enable/pause, clinician export, delete-all | `/wellness` |
| Work and agents | Goals, task DAG, agents, inference history | `/goals`, `/tasks`, `/agents`, `/inference` |
| Systems and incidents | System health and append-only incident records | `/systems`, `/incidents` |
| Connections and apps | Connector registry, macOS relay, iOS/watchOS download and sideload | `/connections`, `/downloads` |

A server-synchronized account-preferences API does not exist yet. Adding one would move these browser-local defaults behind an authenticated preferences endpoint; until then, presenting them as account data would be inaccurate.

## Validation

From `web/`:

```sh
npm run build
npm run e2e -- tests/e2e/settings.spec.ts
npm run e2e
```

The settings e2e test checks the auth gate, the five category tabs, per-category panels, live application of appearance attributes to `<html>`, and persistence across reload. The full e2e suite covers the feature screens that consume the workflow defaults. The Inference tab lazily fetches `GET /api/inference/available` only when active, so other categories never trigger that request.
