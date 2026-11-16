import { useState, useEffect, useCallback, type FormEvent } from 'react'
import { messagesApi, ApiError } from '../api/client'
import { ContactAvatar } from '../components/ContactAvatar'

/**
 * MessagesView — the Messages plane (prompt2 Phase P1).
 *
 * Shows reconstructed message threads ingested by the macOS Personal Relay
 * (NerveCenter never touches source message storage — prompt2 §47). Each
 * thread can produce an AttentionItem, so unread conversations also appear
 * on the Now screen (§76). This view is the human side of that link:
 * read the thread, mark it read (which resolves its AttentionItem), and
 * generate a suggested reply.
 *
 * Drafts are draft-only by design (§13 "AI drafts, human sends"): the view
 * never transmits a message — it only fetches a suggested reply to copy.
 *
 * API failures surface as an `role="alert"` banner (OV-1 contract) rather
 * than a silent empty state; the empty-inbox case is an honest empty state.
 */

interface Message {
  message_id: string
  conversation_id: string
  direction: 'INBOUND' | 'OUTBOUND'
  peer_identifier: string
  peer_name: string | null
  body: string
  channel: string
  timestamp: string
  source?: string
}

interface Conversation {
  conversation_id: string
  peer_identifier: string
  peer_name: string | null
  channel: string
  message_count: number
  unread: number
  last_body: string | null
  last_direction: 'INBOUND' | 'OUTBOUND' | null
  last_timestamp: string | null
  attention_id: string | null
  created_at?: string | null
  updated_at?: string | null
}

interface ThreadData {
  conversation: Conversation
  messages: Message[]
}

interface DraftData {
  conversation_id: string
  to: string | null
  draft: string | null
  basis: string
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

function formatWhen(iso: string | null): string | null {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  return d.toLocaleString()
}

export function MessagesView() {
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [thread, setThread] = useState<ThreadData | null>(null)
  const [draft, setDraft] = useState<DraftData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<'ack' | 'draft' | null>(null)

  const load = useCallback(async () => {
    try {
      setError(null)
      const res = await messagesApi.list()
      if (res.status >= 400) throw new ApiError(res.status, `HTTP ${res.status}`)
      setConversations(res.data as Conversation[])
    } catch (err) {
      setError(formatApiError(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const selectThread = useCallback(async (conversationId: string) => {
    setSelected(conversationId)
    setDraft(null)
    try {
      const res = await messagesApi.thread(conversationId)
      if (res.status >= 400) throw new ApiError(res.status, `HTTP ${res.status}`)
      setThread(res.data as ThreadData)
    } catch (err) {
      setThread(null)
      setError(formatApiError(err))
    }
  }, [])

  const acknowledge = useCallback(async (e: FormEvent) => {
    e.preventDefault()
    if (!selected) return
    setBusy('ack')
    try {
      const res = await messagesApi.acknowledge(selected)
      if (res.status >= 400) throw new ApiError(res.status, `HTTP ${res.status}`)
      await load()
      await selectThread(selected)
    } catch (err) {
      setError(formatApiError(err))
    } finally {
      setBusy(null)
    }
  }, [selected, load, selectThread])

  const requestDraft = useCallback(async (e: FormEvent) => {
    e.preventDefault()
    if (!selected) return
    setBusy('draft')
    try {
      const res = await messagesApi.draft(selected)
      if (res.status >= 400) throw new ApiError(res.status, `HTTP ${res.status}`)
      setDraft(res.data as DraftData)
    } catch (err) {
      setError(formatApiError(err))
    } finally {
      setBusy(null)
    }
  }, [selected])

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-3xl font-bold text-primary">Messages</h1>
        <p className="text-sm text-muted">
          {loading
            ? 'Loading…'
            : `${conversations.length} thread${conversations.length === 1 ? '' : 's'} · via Personal Relay`}
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

      <div className="grid gap-6 lg:grid-cols-2">
        {/* Thread list */}
        <section className="space-y-2">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-muted">
            Threads
          </h2>
          {loading ? (
            <div className="rounded-lg border border-line p-4 text-sm text-muted">
              Loading threads…
            </div>
          ) : conversations.length === 0 ? (
            <div className="rounded-lg border border-line p-4 text-sm text-muted">
              No conversations yet — messages arrive here from the Personal
              Relay on your Mac.
            </div>
          ) : (
            <ul className="space-y-2">
              {conversations.map((c) => {
                const who = c.peer_name ?? c.peer_identifier
                const active = selected === c.conversation_id
                return (
                  <li key={c.conversation_id}>
                    <button
                      type="button"
                      onClick={() => selectThread(c.conversation_id)}
                      className={`w-full rounded-lg border px-4 py-3 text-left transition-colors ${
                        active
                          ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/30'
                          : 'border-line hover:bg-gray-50 dark:hover:bg-gray-800/60'
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="flex min-w-0 items-center gap-2 font-medium text-gray-800 dark:text-gray-100">
                          <ContactAvatar name={who} handle={c.peer_identifier} />
                          <span className="truncate">{who}</span>
                          <span className="text-xs font-normal text-gray-400">
                            {c.channel}
                          </span>
                        </span>
                        {c.unread > 0 && (
                          <span
                            className="rounded-full bg-blue-600 px-2 py-0.5 text-xs font-semibold text-white"
                            aria-label={`${c.unread} unread`}
                          >
                            {c.unread}
                          </span>
                        )}
                      </div>
                      <p className="mt-1 truncate text-sm text-muted">
                        {c.last_body ?? ''}
                      </p>
                      <p className="mt-1 text-xs text-gray-400 dark:text-gray-500">
                        {formatWhen(c.last_timestamp)}
                      </p>
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
        </section>

        {/* Thread detail */}
        <section className="space-y-3">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-muted">
            Conversation
          </h2>
          {!selected ? (
            <div className="rounded-lg border border-line p-4 text-sm text-muted">
              Select a thread to read it.
            </div>
          ) : !thread ? (
            <div className="rounded-lg border border-line p-4 text-sm text-muted">
              Loading conversation…
            </div>
          ) : (
            <div className="space-y-3">
              <ul className="space-y-2">
                {thread.messages.map((m) => {
                  const outbound = m.direction === 'OUTBOUND'
                  return (
                    <li
                      key={m.message_id}
                      className={`max-w-full rounded-lg px-3 py-2 text-sm ${
                        outbound
                          ? 'ml-auto bg-blue-600 text-white'
                          : 'bg-gray-100 dark:bg-gray-800 text-gray-800 dark:text-gray-100'
                      }`}
                    >
                      <p className="whitespace-pre-wrap break-words">{m.body}</p>
                      <p
                        className={`mt-1 text-xs ${
                          outbound ? 'text-blue-100' : 'text-gray-400'
                        }`}
                      >
                        {outbound ? 'You' : m.peer_name ?? m.peer_identifier} ·{' '}
                        {formatWhen(m.timestamp)}
                      </p>
                    </li>
                  )
                })}
              </ul>

              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={(e) => acknowledge(e)}
                  disabled={busy !== null}
                  className="rounded-lg border border-gray-300 dark:border-gray-600 px-3 py-1.5 text-sm font-medium text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-50"
                >
                  {busy === 'ack' ? 'Marking read…' : 'Mark as read'}
                </button>
                <button
                  type="button"
                  onClick={(e) => requestDraft(e)}
                  disabled={busy !== null}
                  className="rounded-lg border border-gray-300 dark:border-gray-600 px-3 py-1.5 text-sm font-medium text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-50"
                >
                  {busy === 'draft' ? 'Drafting…' : 'Draft reply'}
                </button>
              </div>

              {draft && (
                <div className="rounded-lg border border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/20 p-3 text-sm text-amber-800 dark:text-amber-200">
                  <p className="font-semibold">
                    Suggested reply to {draft.to ?? 'contact'}
                  </p>
                  <p className="mt-1 whitespace-pre-wrap">{draft.draft ?? '—'}</p>
                  <p className="mt-2 text-xs opacity-80">
                    {draft.basis} · draft only — you send it, not the system
                    (§13).
                  </p>
                </div>
              )}
            </div>
          )}
        </section>
      </div>
    </div>
  )
}
