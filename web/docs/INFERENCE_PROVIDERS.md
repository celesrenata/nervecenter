# Inference provider registry

The inference layer is configured with a single optional environment
variable, `NERVE_INFERENCE_PROVIDERS` — a JSON array of provider entries.
The backend exposes the configured providers read-only at
`GET /api/inference/providers`, which the **Inference** view renders in a
compact **Providers** section.

## Environment variable shape

```json
[
  {
    "provider": "openai",
    "model_id": "gpt-4o-mini",
    "base_url": null,
    "api_key_env": "OPENAI_API_KEY",
    "capabilities": ["generation", "embeddings"]
  },
  {
    "provider": "local",
    "model_id": "llama3.1-8b",
    "base_url": "http://localhost:11434",
    "api_key_env": null,
    "capabilities": ["generation"]
  }
]
```

| Field            | Type       | Meaning                                                                    |
| ---------------- | ---------- | -------------------------------------------------------------------------- |
| `provider`       | string     | One of `openai`, `anthropic`, `huggingface`, `local`.                      |
| `model_id`       | string     | Model identifier used by the adapter.                                     |
| `base_url`       | string\|null | Optional endpoint override; `null` uses the adapter's default.          |
| `api_key_env`    | string\|null | **Name** of the env var holding the key (e.g. `OPENAI_API_KEY`). The key stays in the environment (the `nervecenter-env` Secret carries it) — never on disk, never in the DB, never in logs. |
| `capabilities`   | string[]   | Declared capabilities, e.g. `generation`, `embeddings`.                   |

## Endpoint

`GET /api/inference/providers` (also registered as `/`):

```json
{
  "data": [
    {
      "provider": "openai",
      "model_id": "gpt-4o-mini",
      "base_url": null,
      "key_configured": true,
      "capabilities": ["generation", "embeddings"]
    },
    {
      "provider": "omniroute",
      "model_id": "omni-default",
      "base_url": null,
      "key_configured": true,
      "capabilities": ["generation"],
      "default": true
    }
  ],
  "meta": { "version": "1.0.0", "request_id": "...", "timestamp": "...", "pagination": { "total": 2, "page": 1, "per_page": 2 } }
}
```

When the live OmniRoute transport is configured (`NERVE_OMNIROUTE_URL` set
or `NERVE_OMNIROUTE_FIXTURE` enabled), an extra `omniroute` entry flagged
`"default": true` is appended after the env-configured providers. Its
`model_id` comes from the optional `NERVE_OMNIROUTE_MODEL` (absent →
`null`). The transport URL and key are deployment plumbing and are never
echoed.

## Honest degradation

The registry never invents state:

- **Unset / empty / invalid JSON** → `data` is an empty list (the view
  shows its "No providers configured" empty state, which points at
  `NERVE_INFERENCE_PROVIDERS`).
- **Named key env var absent** → the provider is still listed, but with
  `key_configured: false` (the view renders a "no key" badge).
- **Unknown provider name or malformed entry** → skipped with a
  `log.warning`; the remaining valid entries still build. A bad config
  entry never takes the app down and never raises at startup.
- **Key values are sanitized** — `key_configured` is a boolean and the
  key itself never appears in the response, in logs, or anywhere else.

## Backend wiring

- Registry: `backend/inference/providers.py` — `build_provider_pool()`
  (returns `(AdapterPool, list[dict])`), `get_default_model()`,
  `omniroute_default_info()`.
- Endpoint: `backend/api/endpoints/inference.py` (router registered in
  `backend/api/app.py`).
- Web client: `web/src/api/inference.ts`; view section in
  `web/src/views/InferenceView.tsx` (`data-testid="inference-providers"`).
- Tests: `tests/backend/test_inference_providers.py`.
