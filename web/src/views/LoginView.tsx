import { useEffect, useRef, useState } from 'react'
import { useAuth } from '../auth/AuthContext'
import { ShieldCheckIcon } from '../components/icons'

/**
 * PKCE session keys — shared with the auth transport in
 * `src/api/client.ts:29–30` (nc.pkce.codeVerifier / nc.pkce.state) so the
 * /auth/callback handler (S102) finds the in-flight exchange values.
 */
const PKCE_VERIFIER_KEY = 'nc.pkce.codeVerifier'
const PKCE_STATE_KEY = 'nc.pkce.state'

/**
 * Discriminates the login rejection shape. The shared contract (auth
 * round, client.ts) rejects with an `ApiError { status, detail }` instance;
 * duck-typed here so this view compiles independently of that change.
 */
interface LoginFailure {
  status?: number
  detail?: string
}

/** Base64url encoding (RFC 7636 §4.2) without padding. */
function base64Url(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '')
}

/**
 * RFC 7636 §4.2 PKCE S256 verifier + challenge for the authorization URL.
 */
async function pkceChallenge(): Promise<{ verifier: string; challenge: string }> {
  // RFC 7636: challenge is BASE64URL(SHA256(ASCII(code_verifier))), i.e. the
  // digest of the verifier STRING's bytes, not the raw random bytes. (Hashing
  // random directly produced a challenge that never matched the verifier sent
  // at token exchange, and Keycloak rejected it: PKCE code mismatch.)
  const random = crypto.getRandomValues(new Uint8Array(32))
  const verifier = base64Url(random)
  const digest = await crypto.subtle.digest(
    'SHA-256',
    new TextEncoder().encode(verifier),
  )
  return {
    verifier,
    challenge: base64Url(new Uint8Array(digest)),
  }
}

/**
 * NerveCenter sign-in (gap-closure LG-1…LG-4, CC-3).
 *
 * - No default-credential hint anywhere in the DOM (LG-1/LG-2, auditor D-2).
 * - Failure messaging is associated with the failing field: 401 →
 *   `aria-describedby` on both inputs; 403/other → general submit-error area.
 *   Every error region is an `aria-live` container (LG-3, auditor D-3).
 * - The submit button is disabled with a visible pending label while
 *   `login()` is in flight; focus stays on the button after a failed submit.
 * - SSO: `GET /api/auth/sso/config` on mount — when `enabled`, the SSO
 *   button is the primary sign-in ("Sign in with <issuer display name>",
 *   contract INFER §Web: the issuer display name comes from the config's
 *   `issuer_display_name` when present, else "SSO"); the username/password
 *   form is moved under a collapsed "Local admin" disclosure (native
 *   `<details>`) as the break-glass secondary path. The button is only
 *   rendered when `enabled` (never a dead button); fetch failure renders
 *   nothing extra (LG-4) and the form stays primary.
 * - LG-1…LG-4 invariants are preserved: no default-credential hint,
 *   aria-live errors, pending submit button, and the same field ids /
 *   error ids / submit button behaviour.
 */
export function LoginView() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [credentialError, setCredentialError] = useState('')
  const [submitError, setSubmitError] = useState('')
  const [ssoConfig, setSsoConfig] = useState<{
    enabled?: boolean
    issuer_display_name?: string
    authorization_endpoint?: string
    client_id?: string
  } | null>(null)

  const { login } = useAuth()
  const submitRef = useRef<HTMLButtonElement>(null)

  const ssoEnabled = ssoConfig?.enabled === true
  const ssoLabel =
    ssoConfig?.issuer_display_name?.trim() || 'SSO'

  useEffect(() => {
    let cancelled = false
    // Fetch failure (network, 404, bad shape) → keep the button hidden;
    // nothing extra is rendered.
    fetch('/api/auth/sso/config')
      .then((res) => (res.ok ? res.json() : null))
      .then((cfg) => {
        if (!cancelled && cfg && typeof cfg === 'object') {
          setSsoConfig(cfg)
        }
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setCredentialError('')
    setSubmitError('')
    setLoading(true)

    try {
      await login(username, password)
      // Success: AuthContext navigates to '/' itself.
    } catch (err) {
      const failure = err as LoginFailure
      if (failure?.status === 401) {
        // Bad credentials — a stable, user-facing message adjacent to the
        // failing fields. The raw backend detail (e.g. "invalid credentials")
        // is intentionally NOT surfaced: it is server-specific and may
        // disclose implementation detail, so the field error stays canonical.
        setCredentialError('Invalid credentials.')
      } else {
        // 403 (e.g. local-admin disabled) or anything unexpected — surface
        // the real detail instead of masking it with a generic message.
        setSubmitError(failure?.detail?.trim() || 'Sign-in could not be completed. Please try again.')
      }
      // Focus stays on the submit control after a failed submit; the error
      // is announced through the aria-live regions below.
      submitRef.current?.focus()
    } finally {
      setLoading(false)
    }
  }

  const handleSso = async () => {
    // Re-read the config on click so the button never trusts a stale
    // snapshot; on any failure the click is a no-op (never a dead button).
    let cfg: { authorization_endpoint?: string; client_id?: string } | null =
      null
    try {
      const res = await fetch('/api/auth/sso/config')
      cfg = res.ok ? ((await res.json()) ?? null) : null
    } catch {
      cfg = null
    }
    if (!cfg?.authorization_endpoint || !cfg?.client_id) return
    const { verifier, challenge } = await pkceChallenge()
    const state = crypto
      .getRandomValues(new Uint32Array(8))
      .reduce((acc, n) => acc + n.toString(36), '')
    try {
      window.sessionStorage.setItem(PKCE_VERIFIER_KEY, verifier)
      window.sessionStorage.setItem(PKCE_STATE_KEY, state)
    } catch {
      // sessionStorage unavailable — the callback cannot complete the
      // exchange, so do not send the user to an IdP that will bounce back
      // with a code we cannot verify.
      setSubmitError('Sign-in could not be completed: session storage is unavailable.')
      return
    }
    const redirectUri = window.location.origin + '/auth/callback'
    const url = new URL(cfg.authorization_endpoint)
    url.searchParams.set('response_type', 'code')
    url.searchParams.set('client_id', cfg.client_id)
    url.searchParams.set('redirect_uri', redirectUri)
    url.searchParams.set('scope', 'openid profile email')
    url.searchParams.set('state', state)
    url.searchParams.set('code_challenge', challenge)
    url.searchParams.set('code_challenge_method', 'S256')
    window.location.assign(url.toString())
  }

  const describedBy = credentialError ? 'login-credential-error' : undefined

  // The username/password form, shared by both layouts: primary when SSO is
  // off, collapsed behind the "Local admin" disclosure when it is on.
  // autoFocus is suppressed under SSO because the form is not the primary
  // path then (focusing a hidden input is a no-op anyway).
  const credentialForm = (
    <form onSubmit={handleSubmit}>
      <div className="mb-4">
        <label
          htmlFor="username"
          className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2"
        >
          Username
        </label>
        <input
          type="text"
          id="username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          aria-describedby={describedBy}
          aria-invalid={credentialError ? true : undefined}
          className="w-full px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg focus:ring-2 focus:ring-blue-500 dark:bg-gray-700 dark:text-white"
          placeholder="Enter username"
          required
          autoFocus={!ssoEnabled}
        />
        {credentialError && (
          <p
            id="login-credential-error"
            aria-live="polite"
            role="alert"
            className="mt-1 text-sm text-red-600 dark:text-red-400"
          >
            {credentialError}
          </p>
        )}
      </div>

      <div className="mb-6">
        <label
          htmlFor="password"
          className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2"
        >
          Password
        </label>
        <input
          type="password"
          id="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          aria-describedby={describedBy}
          aria-invalid={credentialError ? true : undefined}
          className="w-full px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg focus:ring-2 focus:ring-blue-500 dark:bg-gray-700 dark:text-white"
          placeholder="Enter password"
          required
        />
      </div>

      <button
        type="submit"
        ref={submitRef}
        disabled={loading}
        className="w-full inline-flex items-center justify-center gap-2 bg-blue-500 hover:bg-blue-600 text-white font-medium py-2 px-4 rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
      >
        {loading ? (
          <>
            <span
              aria-hidden="true"
              className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white"
            />
            <span>Signing in…</span>
          </>
        ) : (
          'Sign in'
        )}
      </button>
    </form>
  )

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-100 dark:bg-gray-900">
      <div className="max-w-md w-full bg-elevated rounded-lg shadow-lg p-8">
        <h2 className="text-3xl font-bold text-center text-primary mb-2">
          NerveCenter
        </h2>
        <p className="text-center text-muted mb-8">
          Personal Intelligence Core
        </p>

        {/* General error area: 403/other and pre-field-submit failures.
            aria-live so screen readers announce it when it appears. */}
        <div aria-live="assertive">
          {submitError && (
            <div className="mb-4 p-3 bg-red-100 dark:bg-red-900 text-red-700 dark:text-red-200 rounded">
              {submitError}
            </div>
          )}
        </div>

        {/* SSO-FIRST (contract INFER, web scope): when SSO is enabled it is
            the primary sign-in — labelled with the issuer display name from
            /auth/sso/config (honest: falls back to "SSO" when absent).
            Rendered only when enabled; a failed config fetch shows nothing
            (never a dead button, LG-4). */}
        {ssoEnabled && (
          <button
            type="button"
            data-testid="login-sso"
            onClick={handleSso}
            className="mt-4 w-full inline-flex items-center justify-center gap-2 bg-blue-500 hover:bg-blue-600 text-white font-semibold py-2.5 px-4 rounded-lg transition-colors"
          >
            <ShieldCheckIcon size={18} />
            <span>Sign in with {ssoLabel}</span>
          </button>
        )}

        {/* Local admin: the break-glass username/password path. Under SSO it
            is collapsed behind a clear disclosure (NOT the primary path);
            without SSO it is the primary form and renders unwrapped. */}
        {ssoEnabled ? (
          <details data-testid="login-local" className="mt-6">
            <summary className="cursor-pointer select-none text-sm font-medium text-muted hover:text-primary">
              Local admin (break-glass)
            </summary>
            <div className="mt-4 border border-gray-200 dark:border-gray-600 rounded-lg p-4">
              {credentialForm}
            </div>
          </details>
        ) : (
          credentialForm
        )}
      </div>
    </div>
  )
}
