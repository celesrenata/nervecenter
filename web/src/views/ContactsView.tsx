import { useState, useEffect, useCallback, useMemo } from 'react'
import { contactsApi, ApiError } from '../api/client'
import { ContactAvatar } from '../components/ContactAvatar'

/**
 * ContactsView — the Contacts plane (prompt2 Phase P4).
 *
 * Shows resolved *people* (one record per human) aggregated by the source-side
 * Personal Relay (NerveCenter never owns the real address book — prompt2 §79;
 * the relay daemon is a separate, out-of-scope deliverable, §46/§47). Each row
 * carries the merged email/phone sets, an optional note, and the VIP flag
 * (prompt2 §12); toggling the star posts to `/api/contacts/{id}/vip` — the
 * attention engine is what weighs VIP contacts heavier (§22/§45), the Contacts
 * service only stores/returns the flag.
 *
 * API failures surface as an `role="alert"` banner (OV-1 contract) rather than
 * a silent empty state; a genuinely empty store is an honest empty state.
 */

interface Contact {
  person_id: string
  display_name: string
  emails: string[]
  phones: string[]
  notes: string | null
  is_vip: boolean
  source: string
  created_at?: string | null
  last_updated_at?: string | null
}

/** Render an API failure as `<status> — <detail>` (OV-1 contract). */
function formatApiError(err: unknown): string {
  const e = err as { status?: number; detail?: string; message?: string } | null
  if (e && typeof e.status === 'number' && typeof e.detail === 'string') {
    return `${e.status} — ${e.detail}`
  }
  if (e && typeof e.message === 'string' && e.message) return e.message
  return 'Request failed'
}

function matchesQuery(c: Contact, q: string): boolean {
  const hay = [
    c.display_name,
    c.person_id,
    c.notes ?? '',
    ...(c.emails ?? []),
    ...(c.phones ?? []),
  ]
    .join(' ')
    .toLowerCase()
  return hay.includes(q)
}

export function ContactsView() {
  const [contacts, setContacts] = useState<Contact[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [vipOnly, setVipOnly] = useState(false)

  const load = useCallback(async () => {
    try {
      setError(null)
      const res = await contactsApi.list()
      if (res.status >= 400) throw new ApiError(res.status, `HTTP ${res.status}`)
      setContacts(res.data as Contact[])
    } catch (err) {
      setError(formatApiError(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const toggleVip = useCallback(
    async (contact: Contact) => {
      try {
        const res = await contactsApi.setVip(contact.person_id, !contact.is_vip)
        if (res.status >= 400) throw new ApiError(res.status, `HTTP ${res.status}`)
        const updated = res.data as { contact?: Contact } | Contact
        const next: Contact = (updated as { contact?: Contact }).contact ?? (updated as Contact)
        setContacts((prev) => prev.map((c) => (c.person_id === contact.person_id ? next : c)))
      } catch (err) {
        setError(formatApiError(err))
      }
    },
    [],
  )

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return contacts.filter(
      (c) => (!vipOnly || c.is_vip) && (q === '' || matchesQuery(c, q)),
    )
  }, [contacts, query, vipOnly])

  const vipCount = contacts.filter((c) => c.is_vip).length

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-3xl font-bold text-primary">Contacts</h1>
        <p className="text-sm text-muted">
          {loading
            ? 'Loading…'
            : `${contacts.length} person${contacts.length === 1 ? '' : 's'} · ${vipCount} VIP · via Personal Relay`}
        </p>
      </div>

      {error && (
        <div
          role="alert"
          className="rounded-lg border border-red-300 dark:border-red-700 bg-red-50 dark:bg-red-900/30 px-4 py-3 text-sm text-red-700 dark:text-red-300"
        >
          {error}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search by name, email, phone, or notes…"
          aria-label="Search contacts"
          className="min-w-0 flex-1 rounded-lg border border-gray-300 dark:border-gray-600 bg-elevated px-3 py-2 text-sm text-gray-800 dark:text-gray-100 placeholder:text-gray-400 focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
        <label className="flex cursor-pointer select-none items-center gap-2 text-sm text-gray-700 dark:text-gray-200">
          <input
            type="checkbox"
            checked={vipOnly}
            onChange={(e) => setVipOnly(e.target.checked)}
            className="h-4 w-4 rounded border-gray-300 dark:border-gray-600"
          />
          VIP only
        </label>
      </div>

      {loading ? (
        <div className="rounded-lg border border-line p-4 text-sm text-muted">
          Loading contacts…
        </div>
      ) : contacts.length === 0 ? (
        <div className="rounded-lg border border-line p-4 text-sm text-muted">
          No people yet — resolved contacts arrive here from the Personal Relay
          on your Mac.
        </div>
      ) : filtered.length === 0 ? (
        <div className="rounded-lg border border-line p-4 text-sm text-muted">
          No contacts match{query.trim() ? ` “${query.trim()}”` : ''}
          {vipOnly ? ' (VIP only)' : ''}.
        </div>
      ) : (
        <ul className="space-y-2">
          {filtered.map((c) => (
            <li
              key={c.person_id}
              className="rounded-lg border border-line bg-elevated/40 px-4 py-3"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <ContactAvatar name={c.display_name} personId={c.person_id} size={36} />
                    <span className="font-medium text-gray-800 dark:text-gray-100">
                      {c.display_name}
                    </span>
                    {c.is_vip && (
                      <span
                        className="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"
                        aria-label="VIP contact"
                      >
                        VIP
                      </span>
                    )}
                    <span className="text-xs text-gray-400 dark:text-gray-500">
                      {c.source}
                    </span>
                  </div>
                  <div className="mt-1.5 flex flex-col gap-0.5 text-sm text-gray-600 dark:text-gray-300">
                    {c.emails.length > 0 ? (
                      <p className="break-words">
                        <span className="mr-1 text-xs uppercase tracking-wide text-gray-400">
                          Email
                        </span>
                        {c.emails.join(', ')}
                      </p>
                    ) : null}
                    {c.phones.length > 0 ? (
                      <p className="break-words">
                        <span className="mr-1 text-xs uppercase tracking-wide text-gray-400">
                          Phone
                        </span>
                        {c.phones.join(', ')}
                      </p>
                    ) : null}
                    {c.notes ? (
                      <p className="text-xs text-muted">{c.notes}</p>
                    ) : null}
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => toggleVip(c)}
                  aria-pressed={c.is_vip}
                  aria-label={
                    c.is_vip ? `Remove ${c.display_name} from VIP contacts` : `Mark ${c.display_name} as VIP`
                  }
                  className={`shrink-0 rounded-lg border px-2.5 py-1.5 text-sm font-medium transition-colors ${
                    c.is_vip
                      ? 'border-amber-300 dark:border-amber-600 bg-amber-50 dark:bg-amber-900/30 text-amber-700 dark:text-amber-300 hover:bg-amber-100 dark:hover:bg-amber-900/50'
                      : 'border-gray-300 dark:border-gray-600 text-gray-500 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800'
                  }`}
                >
                  {c.is_vip ? '★ VIP' : '☆ VIP'}
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
