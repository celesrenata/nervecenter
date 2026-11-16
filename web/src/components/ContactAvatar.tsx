import { useEffect, useState } from 'react'
import { apiFetch } from '../api/client'

/**
 * Contact thumbnail from Mac Contacts (NS-ADR-097), with an initials fallback.
 *
 * Photos are behind the API's bearer auth, so an <img src> cannot load them
 * directly: they are fetched once with apiFetch and shown as blob URLs. A
 * 404 (no photo) is cached too, so each contact costs at most one request.
 */
const cache = new Map<string, Promise<string | null>>()

function photoUrl(key: { handle?: string; personId?: string }): string | null {
  if (key.personId) return `/api/contact-photos/${encodeURIComponent(key.personId)}`
  if (key.handle) return `/api/contact-photos?handle=${encodeURIComponent(key.handle)}`
  return null
}

function load(path: string): Promise<string | null> {
  let hit = cache.get(path)
  if (!hit) {
    hit = apiFetch(path)
      .then(async (res) => (res.ok ? URL.createObjectURL(await res.blob()) : null))
      .catch(() => null)
    cache.set(path, hit)
  }
  return hit
}

function initials(name: string): string {
  const letters = name
    .split(/\s+/)
    .filter((w) => /^\p{L}/u.test(w))
    .slice(0, 2)
    .map((w) => w[0].toUpperCase())
  return letters.join('') || '#'
}

export function ContactAvatar({
  name,
  handle,
  personId,
  size = 32,
}: {
  name: string
  handle?: string
  personId?: string
  size?: number
}) {
  const path = photoUrl({ handle, personId })
  const [src, setSrc] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    setSrc(null)
    if (path) void load(path).then((url) => live && setSrc(url))
    return () => {
      live = false
    }
  }, [path])
  const style = { width: size, height: size }
  return src ? (
    <img src={src} alt="" style={style} data-testid="contact-avatar-photo"
      className="shrink-0 rounded-full object-cover" />
  ) : (
    <span style={style} aria-hidden="true" data-testid="contact-avatar-initials"
      className="flex shrink-0 items-center justify-center rounded-full bg-gray-200 text-xs font-semibold text-gray-600 dark:bg-gray-700 dark:text-gray-200">
      {initials(name)}
    </span>
  )
}
