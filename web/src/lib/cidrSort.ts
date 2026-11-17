/**
 * Allowlist ordering for the pentest page: biggest range first. IPv4 comes before IPv6,
 * then a shorter prefix (more addresses) before a longer one, then the network address
 * numerically (not as text). Pure: it never changes what is ticked.
 */
export interface CidrLike {
  cidr: string
}

interface Parsed {
  version: 4 | 6 | 0
  prefix: number
  address: bigint
}

function parseV4(text: string): bigint | null {
  const parts = text.split('.')
  if (parts.length !== 4) return null
  let value = 0n
  for (const p of parts) {
    if (!/^\d{1,3}$/.test(p) || Number(p) > 255) return null
    value = (value << 8n) + BigInt(p)
  }
  return value
}

function parseV6(text: string): bigint | null {
  const halves = text.split('::')
  if (halves.length > 2) return null
  const groups = (s: string) => (s === '' ? [] : s.split(':'))
  const head = groups(halves[0])
  const tail = halves.length === 2 ? groups(halves[1]) : []
  const missing = 8 - head.length - tail.length
  if (halves.length === 1 ? missing !== 0 : missing < 1) return null
  const all = [...head, ...Array<string>(halves.length === 2 ? missing : 0).fill('0'), ...tail]
  let value = 0n
  for (const g of all) {
    if (!/^[0-9a-fA-F]{1,4}$/.test(g)) return null
    value = (value << 16n) + BigInt(`0x${g}`)
  }
  return value
}

export function parseCidr(cidr: string): Parsed {
  const [addr, len] = cidr.trim().split('/')
  const v4 = parseV4(addr)
  if (v4 !== null) {
    const prefix = len === undefined ? 32 : Number(len)
    if (!Number.isInteger(prefix) || prefix < 0 || prefix > 32) return { version: 0, prefix: 0, address: 0n }
    return { version: 4, prefix, address: v4 }
  }
  const v6 = addr.includes(':') ? parseV6(addr) : null
  if (v6 !== null) {
    const prefix = len === undefined ? 128 : Number(len)
    if (!Number.isInteger(prefix) || prefix < 0 || prefix > 128) return { version: 0, prefix: 0, address: 0n }
    return { version: 6, prefix, address: v6 }
  }
  return { version: 0, prefix: 0, address: 0n }
}

/** Number of addresses a CIDR covers (0 when it does not parse). */
export function addressCount(cidr: string): bigint {
  const p = parseCidr(cidr)
  if (p.version === 0) return 0n
  return 1n << BigInt((p.version === 4 ? 32 : 128) - p.prefix)
}

/** "131,072 addresses", "1 address". */
export function formatAddresses(count: bigint): string {
  const text = count.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  return `${text} ${count === 1n ? 'address' : 'addresses'}`
}

export function compareCidr(a: string, b: string): number {
  const pa = parseCidr(a)
  const pb = parseCidr(b)
  const rank = (v: number) => (v === 0 ? 3 : v) // unparseable text last
  if (rank(pa.version) !== rank(pb.version)) return rank(pa.version) - rank(pb.version)
  if (pa.prefix !== pb.prefix) return pa.prefix - pb.prefix
  if (pa.address !== pb.address) return pa.address < pb.address ? -1 : 1
  return a < b ? -1 : a > b ? 1 : 0
}

/** A new array, biggest range first. */
export function sortByCidr<T extends CidrLike>(entries: readonly T[]): T[] {
  return [...entries].sort((x, y) => compareCidr(x.cidr, y.cidr))
}
