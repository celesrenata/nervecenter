/**
 * Thin WebSocket wrapper with observable connection state + auto-reconnect.
 *
 * Exposes a small state machine (connecting / open / closed / error) so views
 * can render *honest* connection state instead of a fake "connected" dot.
 * Connection errors are surfaced through `onStateChange` — deliberately NOT
 * through console.error — so a missing /ws route in the mock backend degrades
 * the UI to a visible "reconnecting" state instead of failing console-error
 * based e2e assertions.
 *
 * Auth (NS-ADR-122): a browser WebSocket cannot send an Authorization header,
 * so the first frame after open is `{"type":"auth","token":<access token>}`.
 * The server closes with 4401 when it is missing or refused; the client then
 * refreshes the session once and reconnects, or stops if the session is gone.
 */

import { getAccessToken, recoverSession } from './client'

/** Close code the server uses when the first-frame auth is missing or invalid. */
const CLOSE_UNAUTHORIZED = 4401

export type WebSocketConnectionState = 'connecting' | 'open' | 'closed' | 'error'

export class WebSocketClient {
  private ws: WebSocket | null = null
  private url: string
  private reconnectInterval: number = 3000
  private reconnectTimeout: number | null = null
  private connected: boolean = false
  private disposed: boolean = false
  private authFailures: number = 0
  private messageHandlers: ((data: any) => void)[] = []
  private stateHandlers: ((state: WebSocketConnectionState) => void)[] = []
  private state: WebSocketConnectionState = 'connecting'

  constructor(url: string) {
    this.url = url
    this.connect()
  }

  private setState(next: WebSocketConnectionState): void {
    if (this.state === next) return
    this.state = next
    for (const handler of this.stateHandlers) handler(next)
  }

  private connect(): void {
    if (this.disposed) return
    this.setState('connecting')

    let socket: WebSocket
    try {
      socket = new WebSocket(this.url)
    } catch (error) {
      // e.g. malformed URL — record the error state and stop trying.
      console.warn('WebSocket failed to construct:', error)
      this.setState('error')
      return
    }
    this.ws = socket

    socket.onopen = () => {
      const token = getAccessToken()
      if (token) socket.send(JSON.stringify({ type: 'auth', token }))
      this.connected = true
      this.setState('open')
    }

    socket.onmessage = (event) => {
      this.authFailures = 0
      try {
        const data = JSON.parse(event.data)
        for (const handler of this.messageHandlers) handler(data)
      } catch (error) {
        console.warn('Error parsing WebSocket message:', error)
      }
    }

    socket.onclose = (event) => {
      this.connected = false
      if (this.disposed) {
        this.setState('closed')
        return
      }
      if (event.code === CLOSE_UNAUTHORIZED && this.authFailures++ < 1) {
        void this.recoverAndReconnect()
        return
      }
      this.reconnect()
    }

    socket.onerror = () => {
      // The socket will (or already has) closed; flag the failure so the UI
      // can show an honest error/reconnecting state.
      this.setState('error')
    }
  }

  /** Refresh the session after a 4401 close, then reconnect with the new token. */
  private async recoverAndReconnect(): Promise<void> {
    this.setState('connecting')
    const recovered = await recoverSession()
    if (this.disposed) return
    if (recovered) {
      this.connect()
    } else {
      // The session is gone (or the refresh failed); stop hammering the server.
      this.setState('closed')
    }
  }

  private reconnect(): void {
    if (this.disposed) return
    if (this.reconnectTimeout) clearTimeout(this.reconnectTimeout)
    this.reconnectTimeout = window.setTimeout(() => this.connect(), this.reconnectInterval)
  }

  /** Subscribe to connection-state transitions. Emits the current state immediately. */
  public onStateChange(callback: (state: WebSocketConnectionState) => void): () => void {
    this.stateHandlers.push(callback)
    callback(this.state)
    return () => {
      this.stateHandlers = this.stateHandlers.filter((h) => h !== callback)
    }
  }

  public getState(): WebSocketConnectionState {
    return this.state
  }

  public onMessage(callback: (data: any) => void): () => void {
    this.messageHandlers.push(callback)
    return () => {
      this.messageHandlers = this.messageHandlers.filter((h) => h !== callback)
    }
  }

  public sendMessage(type: string, payload: unknown): void {
    if (this.ws && this.connected) {
      this.ws.send(JSON.stringify({ type, payload }))
    }
  }

  public disconnect(): void {
    this.disposed = true
    if (this.ws) {
      this.ws.onclose = null
      this.ws.onopen = null
      this.ws.onmessage = null
      this.ws.onerror = null
      this.ws.close()
      this.ws = null
    }
    if (this.reconnectTimeout) {
      clearTimeout(this.reconnectTimeout)
      this.reconnectTimeout = null
    }
    this.connected = false
    this.setState('closed')
  }
}
