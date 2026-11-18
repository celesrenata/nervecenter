/** Mirrors the backend local-route check (Req 3.9, 8.5). */
export const LOCAL_ROUTE_PREFIXES = [
  'local/', 'secure/', 'pool/tier1/', 'pool/tier1b/', 'vllm/', 'llama-cpp/', 'ollama-local/',
] as const

export function isLocalRoute(model: string | null | undefined): boolean {
  if (!model) return false
  let m = model.trim().toLowerCase()
  if (m.startsWith('omniroute/')) m = m.slice('omniroute/'.length)
  return LOCAL_ROUTE_PREFIXES.some((p) => m.startsWith(p))
}
