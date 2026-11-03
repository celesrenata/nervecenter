import { ReactNode } from 'react'
import { Navigate, Outlet } from 'react-router-dom'
import { useAuth } from './AuthContext'

/**
 * Route gate (S102).
 *
 * Renders `children` (or the nested `<Outlet/>` when used as a layout-route
 * wrapper) only once the session is authenticated; unauthenticated users are
 * sent to `/login`.
 *
 * Timing: `AuthProvider` withholds `children` until its mount introspection
 * has settled (`loading` is false — a dead/absent token has been cleared
 * here), so by the time this component mounts `isAuthenticated` already
 * reflects the final session state. There is no transient "false then true"
 * window to react to, so the gate never bounces an in-flight login back to
 * `/login`.
 *
 * A live SSO session is already authenticated at mount (AuthContext stores
 * the resolved identity before clearing `loading`), so the gate admits it
 * without any extra round trip.
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { isAuthenticated } = useAuth()
  if (!isAuthenticated) {
    return <Navigate to="/login" replace />
  }
  return <>{children ?? <Outlet />}</>
}
