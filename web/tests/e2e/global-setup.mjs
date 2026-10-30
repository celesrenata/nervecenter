/**
 * e2e global setup (T-WEBE2E / AC005).
 *
 * Boots the dependency-free mock backend on :8000:
 *
 *   NERVE_MOCK_INFER=1 .venv/bin/python -m uvicorn backend.api.app:app --port 8000
 *
 * (boots without DB/NATS; /api/health reports "degraded" but stays HTTP 200).
 * The Vite dev server on :3000 is started separately by the Playwright
 * `webServer` hook with VITE_API_BASE=/api, so the app exercises this real
 * backend instead of its in-browser mock fixtures.
 *
 * The spawned PID is recorded in a temp file so the global teardown (which
 * runs in a separate process) can stop the backend when the run finishes.
 */
import { spawn } from 'node:child_process'
import {
  existsSync,
  readFileSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
// HERE is <repo>/web/tests/e2e → repo root is three levels up.
const REPO_ROOT = path.resolve(HERE, '..', '..', '..')
const PYTHON = path.join(REPO_ROOT, '.venv', 'bin', 'python')
const BACKEND_URL = 'http://127.0.0.1:8000'
const PID_FILE = path.join(os.tmpdir(), 'nerve-e2e-backend.pid')
const HEALTH_TIMEOUT_MS = 60_000

async function waitForBackend(deadline) {
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${BACKEND_URL}/api/health`)
      if (res.ok) return
    } catch {
      // backend not up yet
    }
    await new Promise((r) => setTimeout(r, 500))
  }
  throw new Error(
    `mock backend did not become healthy at ${BACKEND_URL} within ${HEALTH_TIMEOUT_MS}ms`,
  )
}

export default async function main() {
  if (!existsSync(PYTHON)) {
    throw new Error(`repo venv python not found at ${PYTHON} (expected a project .venv)`)
  }

  // A previous crashed run may have left its backend behind; stop it before
  // reusing the port.
  if (existsSync(PID_FILE)) {
    const stalePid = Number(readFileSync(PID_FILE, 'utf8'))
    if (Number.isInteger(stalePid) && stalePid > 0) {
      try {
        process.kill(stalePid, 'SIGTERM')
      } catch {
        // already gone
      }
    }
    unlinkSync(PID_FILE)
    await new Promise((r) => setTimeout(r, 1_000))
  }

  // `detached: true` gives uvicorn its own session/process group so it does NOT
  // get SIGHUP'd and die when Playwright's short-lived globalSetup process
  // exits right after this function returns. `unref()` lets that process exit
  // without waiting on the child. The PID file + global teardown then own the
  // backend's lifetime.
  // `stdio: 'ignore'` so uvicorn's stdout/stderr pipes are not held open by the
  // now-dead globalSetup process, which would otherwise get EPIPE/SIGPIPE and die.
  const child = spawn(
    PYTHON,
    ['-m', 'uvicorn', 'backend.api.app:app', '--port', '8000', '--log-level', 'warning'],
    {
      cwd: REPO_ROOT,
      env: {
        ...process.env,
        NERVE_MOCK_INFER: '1',
        // Enable the break-glass local-admin login (backend/api/auth.py
        // POST /api/auth/login) with a fixed e2e password. The auth-gated
        // routes (all of /api/connections — backend/api/endpoints/
        // connections.py `Depends(current_access)`) require a real
        // locally-minted access token, so the specs establish a session
        // through this endpoint instead of seeding a fake bearer string.
        // No default password exists (AC-S03); this one is test-only.
        NERVE_LOCAL_ADMIN: '1',
        NERVE_LOCAL_ADMIN_PASSWORD: 'e2e-breakglass',
      },
      stdio: 'ignore',
      detached: true,
    },
  )
  child.unref()
  let stderr = ''
  let spawnError = null
  child.stderr?.on('data', (chunk) => {
    stderr += chunk.toString()
  })
  child.on('error', (err) => {
    spawnError = err
    child.kill('SIGKILL')
  })

  try {
    await waitForBackend(Date.now() + HEALTH_TIMEOUT_MS)
  } catch (err) {
    child.kill('SIGKILL')
    throw new Error(
      `failed to start mock backend: ${spawnError ? `spawn: ${spawnError.message}` : String(err)}\n--- backend stderr ---\n${stderr.slice(-4000)}`,
    )
  }

  writeFileSync(PID_FILE, String(child.pid))
  console.log(`[e2e setup] mock backend up on :8000 (pid ${child.pid})`)
}
