import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { ApiError, clearSessionTokens } from '../api/client'
import { tokensApi, type CreatedPersonalToken, type PersonalToken } from '../api/tokens'
import { useAuth } from '../auth/AuthContext'

/**
 * Settings → Access. Personal API tokens (NS-ADR-124): named, optionally
 * expiring, hashed at rest, shown once at creation, revocable. Also "sign out
 * everywhere", which revokes every session and refresh cookie of this account.
 */
const EXPIRY_OPTIONS: Array<{ label: string; days: number | null }> = [
  { label: '30 days', days: 30 },
  { label: '90 days', days: 90 },
  { label: '365 days', days: 365 },
  { label: 'Never', days: null },
]

const errText = (e: unknown): string =>
  e instanceof ApiError ? `${e.status} — ${e.detail}` : 'Request failed (network error)'

const when = (iso: string | null): string => (iso ? new Date(iso).toLocaleString() : '—')

function statusOf(t: PersonalToken): string {
  if (t.revoked_at) return 'Revoked'
  if (t.expires_at && Date.parse(t.expires_at) <= Date.now()) return 'Expired'
  return 'Active'
}

export function AccessTokensPanel() {
  const { logout } = useAuth()
  const [tokens, setTokens] = useState<PersonalToken[] | null>(null)
  const [name, setName] = useState('')
  const [days, setDays] = useState<number | null>(90)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [created, setCreated] = useState<CreatedPersonalToken | null>(null)
  const [copied, setCopied] = useState(false)

  const load = useCallback(async () => {
    try {
      setTokens(await tokensApi.list())
    } catch (e) {
      setError(errText(e))
      setTokens([])
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const create = async (e: FormEvent) => {
    e.preventDefault()
    if (!name.trim()) return
    setBusy(true)
    setError(null)
    setCopied(false)
    try {
      setCreated(await tokensApi.create(name.trim(), days))
      setName('')
      await load()
    } catch (err) {
      setError(errText(err))
    } finally {
      setBusy(false)
    }
  }

  const revoke = async (id: string) => {
    setError(null)
    try {
      await tokensApi.revoke(id)
      await load()
    } catch (err) {
      setError(errText(err))
    }
  }

  const copy = async () => {
    if (!created) return
    try {
      await navigator.clipboard.writeText(created.token)
      setCopied(true)
    } catch {
      setError('Copy failed. Select the token and copy it manually.')
    }
  }

  const signOutEverywhere = async () => {
    setError(null)
    try {
      await tokensApi.signOutEverywhere()
      clearSessionTokens()
      logout()
    } catch (err) {
      setError(errText(err))
    }
  }

  return (
    <section
      id="settings-panel-access"
      role="tabpanel"
      data-testid="access-tokens-panel"
      className="space-y-8"
      aria-labelledby="settings-access"
    >
      <div className="bg-elevated rounded-lg shadow-lg p-6 space-y-4">
        <div>
          <h2 id="settings-access" className="text-xl font-semibold text-primary">
            Personal API tokens
          </h2>
          <p className="text-sm text-muted">
            A token lets a script or the nerve CLI call the API as you (set it as NERVE_TOKEN).
            Each token is shown once, stored only as a hash, and can be revoked at any time.
            Tokens cannot create or revoke other tokens.
          </p>
        </div>

        <form onSubmit={create} className="flex flex-wrap items-end gap-3">
          <div className="flex-1 min-w-[14rem]">
            <label
              htmlFor="access-token-name"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
            >
              Token name
            </label>
            <input
              id="access-token-name"
              data-testid="access-token-name"
              type="text"
              value={name}
              maxLength={120}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. laptop CLI"
              className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
            />
          </div>
          <div>
            <label
              htmlFor="access-token-expiry"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
            >
              Expires
            </label>
            <select
              id="access-token-expiry"
              data-testid="access-token-expiry"
              value={days === null ? 'never' : String(days)}
              onChange={(e) => setDays(e.target.value === 'never' ? null : Number(e.target.value))}
              className="px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
            >
              {EXPIRY_OPTIONS.map((o) => (
                <option key={o.label} value={o.days === null ? 'never' : String(o.days)}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
          <button
            type="submit"
            data-testid="access-token-create"
            disabled={busy || !name.trim()}
            className="px-4 py-2 rounded-lg bg-accent-soft text-accent-contrast font-medium disabled:opacity-50"
          >
            Create token
          </button>
        </form>

        {error && (
          <p data-testid="access-token-error" role="alert" className="text-sm text-red-600">
            {error}
          </p>
        )}

        {created && (
          <div
            data-testid="access-token-reveal"
            className="rounded-lg border border-amber-400 p-4 space-y-2"
          >
            <p className="text-sm font-medium text-primary">
              Copy this token now. It will not be shown again.
            </p>
            <code
              data-testid="access-token-value"
              className="block break-all rounded bg-gray-100 dark:bg-gray-800 p-2 text-sm"
            >
              {created.token}
            </code>
            <div className="flex gap-3">
              <button
                type="button"
                data-testid="access-token-copy"
                onClick={() => void copy()}
                className="px-3 py-1.5 rounded-lg border border-line text-sm"
              >
                {copied ? 'Copied' : 'Copy'}
              </button>
              <button
                type="button"
                data-testid="access-token-dismiss"
                onClick={() => setCreated(null)}
                className="px-3 py-1.5 rounded-lg border border-line text-sm"
              >
                Done
              </button>
            </div>
          </div>
        )}

        <div className="overflow-x-auto">
          <table className="w-full text-sm" data-testid="access-token-table">
            <thead>
              <tr className="text-left text-muted">
                <th className="py-2 pr-4">Name</th>
                <th className="py-2 pr-4">Prefix</th>
                <th className="py-2 pr-4">Created</th>
                <th className="py-2 pr-4">Expires</th>
                <th className="py-2 pr-4">Last used</th>
                <th className="py-2 pr-4">Status</th>
                <th className="py-2" />
              </tr>
            </thead>
            <tbody>
              {(tokens ?? []).map((t) => (
                <tr key={t.id} data-testid={`access-token-row-${t.id}`} className="border-t border-line">
                  <td className="py-2 pr-4 text-primary">{t.name}</td>
                  <td className="py-2 pr-4 font-mono">{t.prefix}…</td>
                  <td className="py-2 pr-4">{when(t.created_at)}</td>
                  <td className="py-2 pr-4">{t.expires_at ? when(t.expires_at) : 'Never'}</td>
                  <td className="py-2 pr-4">{when(t.last_used_at)}</td>
                  <td className="py-2 pr-4" data-testid={`access-token-status-${t.id}`}>
                    {statusOf(t)}
                  </td>
                  <td className="py-2 text-right">
                    {statusOf(t) === 'Active' && (
                      <button
                        type="button"
                        data-testid={`access-token-revoke-${t.id}`}
                        onClick={() => void revoke(t.id)}
                        className="px-3 py-1 rounded-lg border border-line text-red-600"
                      >
                        Revoke
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {tokens !== null && tokens.length === 0 && (
            <p data-testid="access-token-empty" className="py-3 text-sm text-muted">
              No personal tokens yet.
            </p>
          )}
        </div>
      </div>

      <div className="bg-elevated rounded-lg shadow-lg p-6 space-y-3">
        <h3 className="text-lg font-semibold text-primary">Sessions</h3>
        <p className="text-sm text-muted">
          You stay signed in while you use the app. A session ends after 24 hours without
          activity, or 30 days in total. Signing out everywhere revokes every browser session
          for this account.
        </p>
        <button
          type="button"
          data-testid="access-sign-out-everywhere"
          onClick={() => void signOutEverywhere()}
          className="px-4 py-2 rounded-lg border border-line text-red-600"
        >
          Sign out everywhere
        </button>
      </div>
    </section>
  )
}
