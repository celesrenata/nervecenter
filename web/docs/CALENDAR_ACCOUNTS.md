# Calendar accounts

The Calendar view (`/calendar`) shows two things: the **scheduling tools**
(availability guard, free-slot search, §43 conflict-aware suggestion — read the
read-only event copies it holds) and, at the top, the **connected accounts**
section where you connect a calendar to keep those copies fresh.

NerveCenter holds only read-only copies (§42). Connecting an account stores the
credential in the secrets vault only — it is **never** written to the account
row and **never** echoed back. The account row carries at most an opaque
`secret_ref` pointer; the UI shows only a "credential configured" indicator from
that.

## Providers

| Provider | Credential | Extra config |
| --- | --- | --- |
| `google` | OAuth token (vault) | `config.calendarIds` — comma-separated calendar ids (optional) |
| `caldav` | Basic-auth secret (vault) | `config.server_url` — the CalDAV server |
| `apple` | Relay token (optional, vault) | none — events arrive via the authorized relay; no outbound network |

## Backend contract

All routes are under `/api/calendar/accounts`. When the durable registry is not
configured the backend is **honest**, not silent:

| Route | No-durable-store behavior |
| --- | --- |
| `GET  /api/calendar/accounts` | `200 { data: [], meta }` — empty, not an error |
| `POST /api/calendar/accounts` | `202 { accepted, executed: false, request_id, detail }` — not persisted |
| `POST /api/calendar/accounts/{id}/sync` | `202 { executed: false, detail }` — no sync run |

The UI renders `executed: false` as an explanatory note ("calendar accounts are
not configured on this deployment"), **never** as a success and never as a
fabricated row.

### `POST /api/calendar/accounts`

```jsonc
{
  "provider": "google",            // google | caldav | apple
  "label": "Work calendar",        // optional
  "account_ref": "me@example.com", // optional
  "timezone": "America/Los_Angeles",
  "config": { "calendarIds": ["primary"] },
  "credential": "<sent to the vault only>",
  "enabled": true
}
```

→ `201` with the account row (`secret_ref` set when a credential was stored).
`422` for an unknown `provider`.

### Account row shape

```jsonc
{
  "id": "…", "provider": "google", "label": "Work calendar",
  "account_ref": "…", "secret_ref": "…or null", "timezone": "UTC",
  "config": {}, "enabled": true, "operational_status": "…or null",
  "last_success_at": "…or null", "last_attempt_at": "…or null",
  "last_error_code": "…or null", "last_error_message": "…or null",
  "last_sync_at": "…or null", "created_at": "…", "updated_at": "…"
}
```

Per-account actions in the UI: **Sync now** (`POST …/{id}/sync`) and
**Disconnect** (`DELETE …/{id}`).

## Tests

`web/tests/e2e/calendar-accounts.spec.ts` covers the honest empty state and the
honest degraded connect (no fake success, no fabricated row) against the
dependency-free backend booted by `tests/e2e/global-setup.mjs`.
