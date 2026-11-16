import { expect, test } from '@playwright/test'

/**
 * T-WS — the realtime endpoint `GET /api/events/ws` (audit gaps U-6 / CC-1).
 *
 * What this proves, honestly:
 *   1. The WebSocket handshake is accepted (no HTTP error, no silent 404).
 *   2. The endpoint speaks the documented frame protocol
 *      `{"type": "status" | "update" | "error", "payload": ...}`.
 *   3. The honesty contract: with the e2e mock backend (no NATS URL set) the
 *      client receives a single `{"type":"error", payload:{message}}` frame and
 *      the socket closes cleanly — the *opposite* of a silent hang / fake
 *      stream.
 *
 * Target: the backend on `:8000` directly. The Vite dev-server proxy forwards
 * WebSocket only for the `/ws/*` prefix (see vite.config.ts), not the `/api/*`
 * REST prefix, so `ws://localhost:3000/api/events/ws` would not reach the
 * handler. Hitting `:8000` exercises the real FastAPI route — the same route a
 * production reverse proxy (nginx) would front.
 *
 * This Playwright version does not expose the `page.newWebSocket` helper, so
 * the connection is opened with the browser's native `WebSocket` inside a
 * self-contained `page.evaluate` function (which Playwright serializes and runs
 * in the page).
 *
 * The page is first navigated to the real app origin (`http://localhost:3000/`)
 * on purpose: a browser WebSocket opened from a `null`-origin document (e.g.
 * `about:blank`) is rejected by the server's upgrade handling — verified
 * live (`CLOSE 1006`). Opening from the app's own origin is the faithful
 * production scenario (the app page at :3000 opens the realtime socket), and
 * it gives the handshake a well-formed `Origin` header so the backend accepts
 * it and streams the honest frame.
 */

type WsFrame = { type: 'status' | 'update' | 'error'; payload: Record<string, unknown> }

interface WsProbeResult {
  frames: WsFrame[]
  closed: boolean
  handshakeOk: boolean
  error: string | null
}

test('GET /api/events/ws accepts the handshake and speaks the frame protocol', async ({
  page,
}) => {
  // Establish a real app origin so the WebSocket upgrade carries a well-formed
  // Origin header. The e2e webServer guarantees :3000 (Vite) is up here.
  await page.goto('http://localhost:3000/', { waitUntil: 'domcontentloaded' })

  // NS-ADR-122: the first frame must authenticate a signed-in person.
  const login = await fetch('http://127.0.0.1:8000/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'local-admin', password: 'e2e-breakglass' }),
  })
  const { access_token } = (await login.json()) as { access_token: string }
  const result = (await page.evaluate(
    ({ url, token }) =>
      new Promise<WsProbeResult>((resolve) => {
        const frames: WsFrame[] = []
        let closed = false
        let handshakeOk = false
        let error: string | null = null
        let settled = false

        const finish = () => {
          if (settled) return
          settled = true
          resolve({ frames, closed, handshakeOk, error })
        }

        // Hard ceiling so a regression (silent hang) fails fast instead of
        // hanging the suite.
        const hardTimeout = setTimeout(finish, 10_000)

        const ws = new WebSocket(url)
        ws.onopen = () => {
          handshakeOk = true
          ws.send(JSON.stringify({ type: 'auth', token }))
        }
        ws.onmessage = (ev: MessageEvent) => {
          const text = typeof ev.data === 'string' ? ev.data : ''
          try {
            frames.push(JSON.parse(text) as WsFrame)
          } catch {
            frames.push({ type: 'error', payload: { message: `non-JSON frame: ${text}` } })
          }
        }
        ws.onclose = () => {
          closed = true
          clearTimeout(hardTimeout)
          finish()
        }
        ws.onerror = (e) => {
          error = error ?? `ws error: ${(e as ErrorEvent).message ?? 'unknown'}`
          // The server signals failure by closing; onclose will settle us.
        }
      }),
    { url: 'ws://localhost:8000/api/events/ws', token: access_token },
  )) as WsProbeResult

  // 1) The handshake must be accepted. A 404 / connection-refused leaves
  //    handshakeOk false.
  expect(result.handshakeOk, `handshake not accepted (error=${result.error})`).toBe(true)

  // 2) At least one frame was sent, and every frame carries the documented
  //    `type` discriminator.
  expect(result.frames.length, 'no frame received from /api/events/ws').toBeGreaterThanOrEqual(1)
  for (const f of result.frames) {
    expect(['status', 'update', 'error'], 'unknown frame type').toContain(f.type)
  }

  // 3) Honesty contract for the mock backend (no NATS): a single `error` frame
  //    explaining why the stream cannot start, then a clean close. If a live
  //    event bus is present (CI with NATS), a `status`/`update` frame is valid
  //    instead and the endpoint still behaved correctly.
  if (result.frames.some((f) => f.type === 'status' || f.type === 'update')) {
    expect(['status', 'update']).toContain(result.frames[0].type)
  } else {
    const errorFrame = result.frames.find((f) => f.type === 'error')
    expect(errorFrame, 'expected an honest `error` frame when the event bus is absent').toBeTruthy()
    expect(String(errorFrame!.payload.message)).toMatch(/event bus/i)
    expect(result.closed, 'socket did not close cleanly after the error frame').toBe(true)
  }
})
