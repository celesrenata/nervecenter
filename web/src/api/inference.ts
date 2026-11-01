/**
 * Inference provider registry API client.
 *
 * Typed client for the `backend.api.endpoints.inference` route:
 *
 *   GET /api/inference/providers
 *     → { data: ProviderInfo[], meta: { ... } }
 *
 * Each entry is the sanitized projection of a
 * `NERVE_INFERENCE_PROVIDERS` config entry — the key value itself is
 * never returned (only the `key_configured` flag), mirroring the
 * backend's honest-degradation promise: an unset env var yields an
 * empty `data` list, never fabricated providers.
 */

import { apiFetch, apiErrorFromResponse, useMockMode } from './client'

/** Provider names the backend can instantiate adapters for. */
export type InferenceProvider = 'openai' | 'anthropic' | 'huggingface' | 'local'

/** A sanitized provider entry (mirrors the backend projection). */
export interface ProviderInfo {
  /** Provider name, e.g. `openai` or `omniroute` for the live default. */
  provider: string
  /** Model identifier used for this provider. */
  model_id: string
  /** Optional base URL override (`null` → the adapter default). */
  base_url: string | null
  /**
   * Whether the key named by `api_key_env` was present at build time.
   * The key value itself is never part of this response.
   */
  key_configured: boolean
  /** Declared capabilities, e.g. `["generation", "embeddings"]`. */
  capabilities: string[]
  /** `true` only on the appended live OmniRoute default entry. */
  default?: boolean
}

/** Envelope returned by `GET /api/inference/providers`. */
export interface ProvidersResponse {
  data: ProviderInfo[]
  meta: {
    version: string
    request_id: string
    timestamp: string
    pagination?: Record<string, number>
  }
}

// ─── Real API implementation ─────────────────────────────────────────────────

export const realInferenceApi = {
  listProviders: async (): Promise<ProvidersResponse> => {
    const res = await apiFetch('/api/inference/providers')
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as ProvidersResponse
  },
}

// ─── Mock implementation (honest degradation — no fabricated providers) ──────

export const mockInferenceApi = {
  listProviders: async (): Promise<ProvidersResponse> => ({
    data: [],
    meta: {
      version: '1.0.0',
      request_id: 'mock',
      timestamp: new Date().toISOString(),
    },
  }),
}

export const inferenceApi = useMockMode ? mockInferenceApi : realInferenceApi
