import { useContext, createContext, useState, useEffect, ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  apiErrorFromResponse,
  apiFetch,
  clearSessionTokens,
  getAccessToken,
  getAuthorizationUrl,
  getSsoIdentity,
  setAccessToken,
} from '../api/client'

/**
 * Unified auth context (S102).
 *
 * Tokens live in the sessionStorage transport owned by `web/src/api/client.ts`
 * (keys `nc.accessToken` / `nc.refreshToken`) — NOT in localStorage. The old
 * localStorage `authToken` break-glass was stale: it was written by login
 * but never read by the fetch layer, so no request was ever authorized.
 *
 *   - login()  → POST /api/auth/login (break-glass), stores the returned
 *     access+refresh tokens in sessionStorage, resolves the user info,
 *     navigates to `/` (the app root) on success, and REJECTS with ApiError
 *     (status 401/403 + backend detail) on failure so LoginView can map
 *     credential errors to actionable messages.
 *   - logout() → clears the sessionStorage token keys and drops the session.
 *   - mount    → if a token is present, introspect /api/auth/me for the user;
 *     an expired/invalid token 401s, which clears the session (apiFetch).
 *
 * The App is gated: every feature route renders under <RequireAuth> (App.tsx),
 * which redirects unauthenticated users to /login. Because this provider
 * withholds its children until the mount introspection settles, RequireAuth
 * never sees a transient "not authenticated" state for a live session — the
 * gate is safe to wrap the whole Layout.
 */

interface User {
  username: string
  roles: string[]
}

interface AuthContextType {
  user: User | null
  isAuthenticated: boolean
  login: (username: string, password: string) => Promise<User>
  logout: () => void
  /** Mark the session active (used by the SSO callback after the token
   *  exchange has already persisted nc.accessToken/nc.refreshToken). */
  setUser: (user: User | null) => void
}

const AuthContext = createContext<AuthContextType | undefined>(undefined)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUserState] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)
  const navigate = useNavigate()

  useEffect(() => {
    const token = getAccessToken()
    if (!token) {
      setLoading(false)
      return
    }

    // SSO sessions store the resolved identity (verified id_token) at
    // exchange time — the realm *access* token carries no display claims,
    // so /api/auth/me alone would show an anonymous placeholder.  Prefer
    // the stored identity; only fall back to /api/auth/me for break-glass
    // (HS256) tokens, which do carry a sub claim.
    const stored = getSsoIdentity()
    if (stored?.username || stored?.sub || stored?.email) {
      setUserState({
        username:
          stored.username ??
          stored.sub ??
          (stored.email ? stored.email.split('@')[0] : 'sso-user'),
        roles: [],
      })
      setLoading(false)
      return
    }

    apiFetch('/auth/me')
      .then((res) => {
        if (!res.ok) return
        return res.json() as Promise<{
          sub?: string
          username?: string
          roles?: string[]
        }>
      })
      .then((data) => {
        // The backend resolves the display username (sub for break-glass).
        // Prefer it over the raw sub.
        if (data && (data.username || data.sub)) {
          setUserState({
            username: data.username ?? data.sub ?? 'sso-user',
            roles: data.roles ?? [],
          })
        }
      })
      .catch(() => {
        /* network failure — user stays unset, token left as-is */
      })
      .finally(() => setLoading(false))
  }, [])

  /**
   * SSO session bootstrap (gap-closure SSO-GAP).
   *
   * The cluster's SSO gate (oauth2-proxy) authenticates the browser against
   * the issuer but deliberately does NOT forward that identity token to the
   * API (no `--pass-access-token`) — the API requires a token minted through
   * the app's own SSO flow, and `apiFetch` only attaches one when
   * `nc.accessToken` is in sessionStorage. Until now that mint only happened
   * via the *manual* SSO button on `/login`, so a gate-authenticated user
   * landed in the app with no app session: every auth-gated mutation
   * (e.g. `POST /api/inference/durable/providers`) failed with 401
   * "missing bearer token".
   *
   * Bridge the gate session into the app session: when SSO is enabled, there
   * is no token yet, and we are not already on the callback, redirect to the
   * issuer's authorization endpoint (PKCE). The issuer short-circuits the
   * redirect when its session cookie is valid, then the provider lands on
   * `/auth/callback`, which exchanges the code for tokens and returns the
   * user to `/`. The `nc.sso.bootstrapAt` sessionStorage marker guarantees
   * the redirect happens at most once per tab session, so a broken callback
   * cannot turn into a redirect loop.
   */
  useEffect(() => {
    if (getAccessToken()) return
    // Never auto-redirect from the sign-in screen or the callback itself:
    // /login is the explicit break-glass path the user chose (the bootstrap
    // would bounce them straight back into SSO and remove the escape hatch),
    // and /auth/callback is where the redirect *lands* (auto-bouncing there
    // would be a redirect loop).
    if (window.location.pathname === '/auth/callback') return
    if (window.location.pathname === '/login') return
    if (window.sessionStorage.getItem('nc.sso.bootstrapAt')) return
    getAuthorizationUrl()
      .then((url) => {
        if (!url) return // SSO not enabled (dev / mock) — stay on the page
        try {
          window.sessionStorage.setItem('nc.sso.bootstrapAt', new Date().toISOString())
        } catch {
          /* sessionStorage unavailable — the guard is best-effort */
        }
        window.location.assign(url)
      })
      .catch(() => {
        /* config fetch failed — user stays on the page, token unset */
      })
  }, [])

  const login = async (username: string, password: string): Promise<User> => {
    const res = await apiFetch('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    })

    // Non-2xx → reject with ApiError (status 401/403 + FastAPI detail) so
    // LoginView can surface actionable credential errors.
    if (!res.ok) throw await apiErrorFromResponse(res)

    // backend/api/auth.py login response:
    // { token_type, access_token, expires_at, refresh_token,
    //   refresh_expires_at, sub, roles }
    const data = (await res.json()) as {
      access_token?: string
      refresh_token?: string | null
      sub?: string
      roles?: string[]
      user?: { username?: string; roles?: string[] }
    }
    if (!data.access_token) {
      throw new Error('Login response did not include an access token')
    }
    setAccessToken(data.access_token, data.refresh_token ?? null)
    const authedUser: User = {
      username: data.sub ?? data.user?.username ?? username,
      roles: Array.isArray(data.roles)
        ? data.roles
        : data.user?.roles ?? [],
    }
    setUserState(authedUser)
    // Successful break-glass login lands the user in the app: navigate away
    // from /login (replace so browser-back does not re-open the sign-in
    // form). Mirrors the SSO callback's post-exchange redirect (AuthCallback
    // → navigate('/')), so both entry points now reach the app the same way.
    navigate('/', { replace: true })
    return authedUser
  }

  const logout = () => {
    clearSessionTokens()
    // Drop the one-shot SSO bootstrap guard too, so signing back in from this
    // tab can re-run the gate→session bootstrap (see the bootstrap effect).
    try {
      window.sessionStorage.removeItem('nc.sso.bootstrapAt')
    } catch {
      /* sessionStorage unavailable — ignore */
    }
    setUserState(null)
    navigate('/login')
  }

  const setUser = (nextUser: User | null) => setUserState(nextUser)

  const value: AuthContextType = {
    user,
    isAuthenticated: !!user,
    login,
    logout,
    setUser,
  }

  if (loading) {
    return <div>Loading...</div>
  }

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider')
  }
  return context
}
