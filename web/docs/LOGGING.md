# NerveCenter Web UI - Live In-Service Logs (§49)

Live, in-service observability. The web **Logs** view (`/logs`) streams the
most recent rows captured *inside the running backend process* — so you can
watch what the service is actually doing, not a stale file on disk.

Unlike the other collection endpoints (which 501 when no durable service is
wired), the Logs view **always returns real data**: the ring buffer is
populated live from both the `structlog` and standard-library log paths, so
there is no "unwired service" branch and no empty-by-design 501.

## What you get

- **Real-time tail** of the backend's own log stream (request handling,
  service calls, errors), newest first.
- **Level filter** (`debug` / `info` / `warning` / `error` / `critical`) as a
  *minimum* severity — picking `warning` shows warning, error and critical.
- **Row cap** so a busy service never floods the view.
- **Secrets are never surfaced**: every record is redacted *before* it is
  stored, and the response carries `secrets_scrubbed: true` as a documented
  guarantee (see "Security" below).

## Architecture

```
            ┌───────────────────────────  backend process  ───────────────────────────┐
            │                                                                          │
 structlog ─┤  structlog processor (capture_event) ──┐                                 │
            │                                         ├──  LogBuffer (in-memory ring,  │
 stdlib ────┤  logging.Handler (_Handler) ───────────┘      capacity 500, thread-safe) │
            │                                                     │                    │
            │                                                     ▼                    │
            │   FastAPI route  GET /api/logs?limit=&level=  ←  get_entries(...)        │
            │            │                                                               │
            └────────────┼───────────────────────────────────────────────────────────────┘
                         │  JSON: { data, secrets_scrubbed, meta }
                         ▼
                  web/src/views/LogsView.tsx  (table, level filter, auto-refresh)
```

- **Capture wiring** (`backend/observability/log_buffer.py`) installs a
  `structlog` pipeline processor **and** a stdlib `logging.Handler` onto the
  root logger. It is installed from the app lifespan (in
  `backend/api/app.py`) *after* uvicorn's own log configuration, so the
  capture survives uvicorn's reconfiguration and is re-anchored per app
  instance.
- **Bounded memory**: the buffer is a fixed-capacity ring (default **500**
  rows). New rows evict the oldest; the process never grows unbounded.
- **Single source of truth**: a process-wide singleton
  (`get_log_buffer()`); `reset_for_tests()` exists so tests can re-install
  cleanly without leaking state.

## API

### List captured logs

```
GET /api/logs
GET /api/logs?limit=100
GET /api/logs?level=error&limit=200
```

Query params:

| Param   | Type  | Default | Notes |
|---------|-------|---------|-------|
| `limit` | int   | `100`   | Max rows, clamped `1..1000`. |
| `level` | string| `""`    | Minimum severity (`debug`…`critical`). Empty → no filter. |

Response envelope:

```json
{
  "data": [
    {
      "level": "info",
      "timestamp": "2026-11-01T11:39:24.123456Z",
      "logger": "backend.api.health",
      "message": "health endpoint handled status=ok"
    }
  ],
  "secrets_scrubbed": true,
  "meta": {
    "version": "1.0.0",
    "request_id": "0e5b1c2a-…",
    "timestamp": "2026-11-01T11:39:24Z"
  }
}
```

- `level` is the normalized level name.
- `timestamp` is ISO-8601 UTC.
- `message` is already scrubbed of credential-looking values.

## Frontend

- `web/src/api/logs.ts` — typed client (`logsApi.getLogs({ limit, level })`)
  with an honest **empty** mock (a live stream can't be faked, so mock mode
  shows the empty state rather than invented rows).
- `web/src/views/LogsView.tsx` — the view: level filter, row cap, optional
  auto-refresh (3 s), per-level summary chips, and a readable table. Uses the
  house design tokens (`text-primary`, `text-muted`, `bg-elevated`) so light
  and dark both stay legible; each level has visible text *and* a colour dot
  (never colour alone).
- Route: `<Route path="/logs" element={<LogsView />} />` in `web/src/App.tsx`.
- Nav: a **Logs** entry (terminal icon) in `web/src/components/Layout.tsx`.

## Security

The buffer is read from the API, so it is only as safe as what it captures.
Every record is redacted at capture time — *before* it is stored — by
`_scrub` (in `log_buffer.py`), which replaces inline credential-looking
patterns with `[redacted]`:

- `Bearer <token>` headers/tokens,
- `sk-<key>`-style API keys,
- long hex/base64 blobs (≥ 32 chars).

In addition, if any structlog key-value pair has a **key** that looks
sensitive (`password`, `api_key`, `token`, `secret`, …), the *whole* record is
redacted: its message is replaced with
`event redacted: contains a sensitive field` so the event (and any sibling
values) never reach the buffer. `secrets_scrubbed: true` is a documented guarantee — the
pipeline always runs — not a per-request state you can switch off.

> Best-effort caveat: this is inline redaction, not an exhaustive audit.
> Anything you log that genuinely looks like a credential should still be
> treated as potentially exposed in the tail — don't log secrets at all.

## Tests

`tests/backend/test_logs_api.py` covers:

- ring-buffer semantics (overflow eviction, level filter, limit, `clear`),
- the inline redaction rules,
- the `GET /api/logs` endpoint against the real `app` (route registered,
  envelope shape, `secrets_scrubbed === true`, `level` and `limit` params,
  and that a live `structlog` record actually appears in the response).

Run: `pytest tests/backend/test_logs_api.py`.
