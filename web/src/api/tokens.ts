/**
 * Personal API token client (NS-ADR-124). The plaintext token is returned once,
 * by `create`; list responses carry only a display prefix.
 */
import { apiFetch, apiErrorFromResponse } from './client'

export interface PersonalToken {
  id: string
  name: string
  prefix: string
  created_at: string | null
  expires_at: string | null
  last_used_at: string | null
  revoked_at: string | null
}

export interface CreatedPersonalToken extends PersonalToken {
  /** Plaintext token; shown once and never retrievable again. */
  token: string
}

const BASE = '/api/auth/tokens'

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw await apiErrorFromResponse(res)
  return (await res.json()) as T
}

export const tokensApi = {
  list: async (): Promise<PersonalToken[]> =>
    (await json<{ tokens: PersonalToken[] }>(await apiFetch(BASE))).tokens,
  create: async (name: string, expiresInDays: number | null): Promise<CreatedPersonalToken> =>
    json(
      await apiFetch(BASE, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, expires_in_days: expiresInDays }),
      }),
    ),
  revoke: async (id: string): Promise<void> => {
    await json(await apiFetch(`${BASE}/${encodeURIComponent(id)}`, { method: 'DELETE' }))
  },
  signOutEverywhere: async (): Promise<void> => {
    await json(await apiFetch('/api/auth/logout-all', { method: 'POST' }))
  },
}
