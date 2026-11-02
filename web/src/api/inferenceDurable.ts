/**
 * Durable inference providers + combos API (providers feature).
 *
 * Companion to :mod:`./inference` (the read-only, env-driven
 * ``GET /api/inference/providers`` surface). This module drives the
 * *persistent* registry the backend serves under
 * ``/api/inference/durable``:
 *
 *   - providers: list / create / patch / delete / test-connection / recall models
 *   - combos:    list / get / bound-agents / create / patch / delete
 *
 * Mock mode degrades honestly to an empty registry (never fabricated rows) and
 * returns stable 2xx so a dev without a backend can still exercise the UI.
 * The API key is stored in-band on the backend and masked on every read, so the
 * UI only ever sees the sentinel ``"••••••••"`` — it is a read-only display,
 * never a write value.
 */

import { apiFetch, apiErrorFromResponse, useMockMode } from './client'

// ─── Types ───────────────────────────────────────────────────────────────────

export type DurableProviderType = 'openai' | 'anthropic' | 'local' | 'huggingface'

export interface DurableProvider {
  id: string
  name: string
  provider_type: DurableProviderType
  base_url: string | null
  /** Masked sentinel when a key is set; ``null`` when unconfigured. */
  api_key: string | null
  key_configured: boolean
  capabilities: string[]
  models: string[]
  model_count: number
  created_at: string
  updated_at: string
}

export interface DurableCombo {
  id: string
  name: string
  provider_id: string
  provider_name: string | null
  model_id: string
  note: string | null
  created_at: string
  updated_at: string
}

export interface BoundAgent {
  id: string
  name: string
  type: string
}

export interface ProviderCreatePayload {
  name: string
  provider_type: DurableProviderType
  base_url?: string | null
  api_key?: string | null
  capabilities?: string[]
  models?: string[]
}

export interface ProviderUpdatePayload {
  name?: string
  provider_type?: DurableProviderType
  base_url?: string | null
  /** A supplied key overwrites; empty string clears; omitted leaves it. */
  api_key?: string | null
  capabilities?: string[]
  models?: string[]
}

export interface ComboCreatePayload {
  name: string
  provider_id: string
  model_id: string
  note?: string | null
}

export interface ComboUpdatePayload {
  name?: string
  provider_id?: string
  model_id?: string
  note?: string | null
}

export interface TestConnectionResult {
  success: boolean
  /** The models recalled during the probe (when the provider exposes them). */
  models: string[]
  error?: string
  /** How long the round-trip took, ms. */
  latency_ms?: number
}

export interface RecallModelsResult {
  models: string[]
  count: number
}

// ─── Real implementation (fetch) ─────────────────────────────────────────────

const P = '/api/inference/durable/providers'
const C = '/api/inference/durable/combos'

export const realDurableInferenceApi = {
  // Providers
  listProviders: async (): Promise<DurableProvider[]> => {
    const res = await apiFetch(P)
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as DurableProvider[]
  },
  getProvider: async (id: string): Promise<DurableProvider> => {
    const res = await apiFetch(`${P}/${id}`)
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as DurableProvider
  },
  createProvider: async (payload: ProviderCreatePayload): Promise<DurableProvider> => {
    const res = await apiFetch(P, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as DurableProvider
  },
  updateProvider: async (
    id: string,
    payload: ProviderUpdatePayload,
  ): Promise<DurableProvider> => {
    const res = await apiFetch(`${P}/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as DurableProvider
  },
  deleteProvider: async (id: string): Promise<void> => {
    const res = await apiFetch(`${P}/${id}`, { method: 'DELETE' })
    if (!res.ok && res.status !== 204) throw await apiErrorFromResponse(res)
  },
  testProvider: async (id: string): Promise<TestConnectionResult> => {
    const res = await apiFetch(`${P}/${id}/test`, { method: 'POST' })
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as TestConnectionResult
  },
  recallProviderModels: async (id: string): Promise<RecallModelsResult> => {
    const res = await apiFetch(`${P}/${id}/models`, { method: 'POST' })
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as RecallModelsResult
  },
  // Combos
  listCombos: async (): Promise<DurableCombo[]> => {
    const res = await apiFetch(C)
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as DurableCombo[]
  },
  getCombo: async (id: string): Promise<DurableCombo> => {
    const res = await apiFetch(`${C}/${id}`)
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as DurableCombo
  },
  listBoundAgents: async (id: string): Promise<BoundAgent[]> => {
    const res = await apiFetch(`${C}/${id}/agents`)
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as BoundAgent[]
  },
  createCombo: async (payload: ComboCreatePayload): Promise<DurableCombo> => {
    const res = await apiFetch(C, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as DurableCombo
  },
  updateCombo: async (
    id: string,
    payload: ComboUpdatePayload,
  ): Promise<DurableCombo> => {
    const res = await apiFetch(`${C}/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!res.ok) throw await apiErrorFromResponse(res)
    return (await res.json()) as DurableCombo
  },
  deleteCombo: async (id: string): Promise<void> => {
    const res = await apiFetch(`${C}/${id}`, { method: 'DELETE' })
    if (!res.ok && res.status !== 204) throw await apiErrorFromResponse(res)
  },
}

// ─── Mock implementation (honest degradation — no fabricated registry) ───────

export const mockDurableInferenceApi = {
  listProviders: async (): Promise<DurableProvider[]> => [],
  getProvider: async (): Promise<DurableProvider> => {
    throw new Error('mock: no durable providers in mock mode')
  },
  createProvider: async (): Promise<DurableProvider> => {
    throw new Error('mock: durable provider writes require a live backend')
  },
  updateProvider: async (): Promise<DurableProvider> => {
    throw new Error('mock: durable provider writes require a live backend')
  },
  deleteProvider: async (): Promise<void> => {
    throw new Error('mock: durable provider writes require a live backend')
  },
  testProvider: async (): Promise<TestConnectionResult> => {
    throw new Error('mock: test-connection requires a live backend')
  },
  recallProviderModels: async (): Promise<RecallModelsResult> => {
    throw new Error('mock: model recall requires a live backend')
  },
  listCombos: async (): Promise<DurableCombo[]> => [],
  getCombo: async (): Promise<DurableCombo> => {
    throw new Error('mock: no durable combos in mock mode')
  },
  listBoundAgents: async (): Promise<BoundAgent[]> => [],
  createCombo: async (): Promise<DurableCombo> => {
    throw new Error('mock: durable combo writes require a live backend')
  },
  updateCombo: async (): Promise<DurableCombo> => {
    throw new Error('mock: durable combo writes require a live backend')
  },
  deleteCombo: async (): Promise<void> => {
    throw new Error('mock: durable combo writes require a live backend')
  },
}

export const durableInferenceApi = useMockMode
  ? mockDurableInferenceApi
  : realDurableInferenceApi

/** Convenience: the stable masked sentinel the backend returns for a set key. */
export const MASKED_KEY = '••••••••'
