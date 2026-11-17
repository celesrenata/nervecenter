/**
 * Owner resources (NS-ADR-125): files stored encrypted on the object store and
 * linked to agents. Secret files are never linkable and agents never read them.
 */
import { apiFetch, apiErrorFromResponse } from './client'

export type ResourceSensitivity = 'normal' | 'secret'

export interface Resource {
  id: string
  name: string
  content_type: string
  size: number
  sha256: string
  sensitivity: ResourceSensitivity
  uploaded_by: string | null
  created_at: string | null
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw await apiErrorFromResponse(res)
  return (await res.json()) as T
}

export const resourcesApi = {
  list: async () => (await json<{ resources: Resource[] }>(await apiFetch('/api/resources'))).resources,
  upload: async (file: File, sensitivity: ResourceSensitivity) =>
    json<Resource>(
      await apiFetch(
        `/api/resources?name=${encodeURIComponent(file.name)}&sensitivity=${sensitivity}`,
        { method: 'PUT', headers: { 'Content-Type': 'application/octet-stream' }, body: file },
      ),
    ),
  remove: async (id: string) => {
    const res = await apiFetch(`/api/resources/${encodeURIComponent(id)}`, { method: 'DELETE' })
    if (!res.ok) throw await apiErrorFromResponse(res)
  },
  download: async (r: Resource) => {
    const res = await apiFetch(`/api/resources/${encodeURIComponent(r.id)}/download`)
    if (!res.ok) throw await apiErrorFromResponse(res)
    const url = URL.createObjectURL(await res.blob())
    const a = document.createElement('a')
    a.href = url
    a.download = r.name
    a.click()
    URL.revokeObjectURL(url)
  },
  agentLinks: async (agentId: string) =>
    (await json<{ resources: Resource[] }>(await apiFetch(`/api/agents/${encodeURIComponent(agentId)}/resources`)))
      .resources,
  setAgentLinks: async (agentId: string, resourceIds: string[]) =>
    (
      await json<{ resources: Resource[] }>(
        await apiFetch(`/api/agents/${encodeURIComponent(agentId)}/resources`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ resource_ids: resourceIds }),
        }),
      )
    ).resources,
}
