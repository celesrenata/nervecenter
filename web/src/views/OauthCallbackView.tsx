/**
 * Provider-OAuth authorization-code callback (Google / Microsoft sign-in).
 *
 * The backend's :meth:`ConnectionControlPlane.start_oauth` emits the provider's
 * authorize URL with ``redirect_uri`` set to this route (deploy env
 * ``NERVE_OAUTH_CALLBACK_URL``). After the user signs in at the provider, the
 * provider bounces the browser back here as
 * ``/connections/oauth/callback?code=…&state=…``. The ``state`` nonce carries
 * the connection id (base64url) plus a CSRF token, so this view can bind the
 * exchange to the right connection without any cross-tab session state.
 *
 * Flow:
 *   1. read ``code`` + ``state`` from the search params,
 *   2. decode the connection id out of ``state`` (``b64url(id) + '.' + token``),
 *   3. call ``startAuth(id, { code, state })`` to complete the token exchange
 *      (the bearer session rides along from the same tab's sessionStorage),
 *   4. on success, transition to ``/connections`` where the row shows
 *      ``authenticated``.
 *
 * On failure it shows a short, actionable message and a link back to
 * ``/connections`` — it never leaves the user stranded. The exchange is
 * idempotent at the backend (CSRF ``state`` binding is enforced server-side,
 * so a forged/duplicated ``state`` is rejected), so replaying the callback URL
 * after a success is a harmless no-op that the user can dismiss.
 *
 * Rendered inside ``Layout`` + ``RequireAuth`` (registered in App.tsx) so the
 * session is guaranteed present when the API call is made and the result is
 * shown in the familiar app chrome.
 */

import { useEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { connectionsApi, isDegradedWrite } from '../api/connections'

type Phase = 'exchanging' | 'done' | 'error'

/** Decode ``b64url`` → UTF-8 string. ``atob`` needs standard base64, so we
 *  translate the url-safe alphabet first. */
function decodeConnectionId(state: string): string | null {
  // The state nonce is ``b64url(connection_id) + '.' + csrf_token``.
  const dot = state.indexOf('.')
  if (dot <= 0) return null
  const b64 = state.slice(0, dot).replace(/-/g, '+').replace(/_/g, '/')
  // Pad to a multiple of 4 for atob.
  const padded = b64.padEnd(Math.ceil(b64.length / 4) * 4, '=')
  try {
    const bin = atob(padded)
    // UTF-8 decode (connection ids are ascii, but be robust).
    const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0))
    return new TextDecoder().decode(bytes)
  } catch {
    return null
  }
}

export function OauthCallbackView() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const [phase, setPhase] = useState<Phase>('exchanging')
  const [error, setError] = useState<string | null>(null)
  const started = useRef(false)

  useEffect(() => {
    if (started.current) return
    started.current = true

    const code = searchParams.get('code') ?? ''
    const state = searchParams.get('state') ?? ''
    const errParam = searchParams.get('error')

    const finish = async () => {
      if (!code) {
        setPhase('error')
        setError(
          errParam
            ? `The provider reported an error: ${errParam}`
            : 'No authorization code in the callback URL — the provider did not complete the exchange.',
        )
        return
      }
      const connectionId = decodeConnectionId(state)
      if (!connectionId) {
        setPhase('error')
        setError(
          'Could not read the connection id from the state nonce — the callback is not bound to a known connection. Start the sign-in again from the connection wizard.',
        )
        return
      }
      try {
        const res = await connectionsApi.startAuth(connectionId, { code, state })
        if (isDegradedWrite(res)) {
          setPhase('error')
          setError(res.detail)
          return
        }
        // 'id' in res → the row was refreshed with the new auth status.
        setPhase('done')
        // Short beat so the "connected" state is visible before the transition.
        window.setTimeout(() => navigate('/connections'), 600)
      } catch (e) {
        setPhase('error')
        setError(e instanceof Error ? e.message : 'The token exchange failed.')
      }
    }
    void finish()
  }, [searchParams, navigate])

  const title =
    phase === 'done'
      ? 'Connection signed in'
      : phase === 'error'
        ? 'Sign-in did not finish'
        : 'Completing sign-in…'

  return (
    <div className="mx-auto flex max-w-md flex-col items-center gap-4 py-16 text-center">
      {phase === 'exchanging' && (
        <>
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-accent border-t-transparent" aria-hidden />
          <p className="text-sm text-secondary">{title}</p>
        </>
      )}
      {phase === 'done' && (
        <>
          <div className="text-3xl" aria-hidden>
            ✅
          </div>
          <p className="text-sm text-secondary">
            This account is connected. Returning to <strong>Connections</strong>…
          </p>
          <button
            type="button"
            onClick={() => navigate('/connections')}
            className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-contrast hover:bg-accent-hover"
          >
            Go to Connections
          </button>
        </>
      )}
      {phase === 'error' && (
        <>
          <div className="text-3xl" aria-hidden>
            ⚠️
          </div>
          <p className="text-sm text-secondary">{error}</p>
          <button
            type="button"
            onClick={() => navigate('/connections')}
            className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-contrast hover:bg-accent-hover"
          >
            Back to Connections
          </button>
        </>
      )}
    </div>
  )
}
