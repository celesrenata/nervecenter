import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from '../api/client'
import { agentsApi, type AgentLiveEvent, type AgentLiveRun } from '../api/agents'
import { Markdown } from '../components/Markdown'
import { MaximizeOverlay } from '../components/MaximizeOverlay'

const POLL_RUNNING_MS = 1500
const POLL_IDLE_MS = 5000

const KIND_LABEL: Record<string, string> = {
  run_started: 'Start',
  skills: 'Skills',
  model_call: 'Model',
  model_reply: 'Reply',
  tool_call: 'Tool call',
  tool_result: 'Tool result',
  run_finished: 'Finish',
}

const KIND_TONE: Record<string, string> = {
  run_started: 'text-blue-600 dark:text-blue-400',
  model_call: 'text-muted',
  model_reply: 'text-green-700 dark:text-green-400',
  tool_call: 'text-purple-700 dark:text-purple-300',
  tool_result: 'text-purple-700 dark:text-purple-300',
  run_finished: 'text-blue-600 dark:text-blue-400',
}

function time(at: string): string {
  const d = new Date(at)
  return Number.isNaN(d.getTime()) ? at : d.toLocaleTimeString()
}

function EventRow({ ev }: { ev: AgentLiveEvent }) {
  const data = ev.data ?? {}
  const tokens =
    ev.kind === 'model_reply' && (data.output_tokens != null || data.latency_ms != null)
      ? ` · ${data.output_tokens ?? '?'} tokens${data.latency_ms != null ? ` · ${Math.round(Number(data.latency_ms))} ms` : ''}`
      : ''
  return (
    <li data-testid="agent-live-event" data-kind={ev.kind} className="border-b border-line py-2 last:border-b-0">
      <div className="flex items-baseline gap-2 text-xs">
        <span className="font-mono text-muted">{time(ev.at)}</span>
        <span className={`font-semibold ${KIND_TONE[ev.kind] ?? 'text-primary'}`}>{KIND_LABEL[ev.kind] ?? ev.kind}</span>
        {ev.kind !== 'model_reply' && ev.kind !== 'tool_result' && <span className="text-primary">{ev.text}</span>}
        {tokens && <span className="text-muted">{tokens}</span>}
      </div>
      {ev.kind === 'model_reply' && <Markdown text={ev.text || '(empty reply)'} className="mt-1" />}
      {ev.kind === 'tool_call' && data.args != null && (
        <pre className="mt-1 overflow-x-auto rounded bg-hover p-2 text-xs">{JSON.stringify(data.args, null, 2)}</pre>
      )}
      {ev.kind === 'tool_result' && (
        <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded bg-hover p-2 text-xs">{ev.text}</pre>
      )}
    </li>
  )
}

/**
 * Live progress of an agent's runs, polled from `/api/agents/{id}/live`.
 * Follows the newest run unless an older one is picked; auto-scrolls while
 * the view is at the bottom.
 */
export function AgentLiveLog({ agentId, agentName }: { agentId: string; agentName: string }) {
  const [runs, setRuns] = useState<AgentLiveRun[]>([])
  const [run, setRun] = useState<AgentLiveRun | null>(null)
  const [events, setEvents] = useState<AgentLiveEvent[]>([])
  const [pinnedRun, setPinnedRun] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [maximized, setMaximized] = useState(false)
  const lastSeq = useRef(0)
  const runKey = useRef<number | null>(null)
  const scroller = useRef<HTMLDivElement | null>(null)
  const atBottom = useRef(true)

  const poll = useCallback(async (): Promise<boolean> => {
    try {
      const view = await agentsApi.live(agentId, lastSeq.current, pinnedRun ?? undefined)
      setError(null)
      setRuns(view.runs)
      const shown = view.run
      if (shown && shown.run_key !== runKey.current) {
        // A different run (a new one started, or another was picked): reload it whole.
        runKey.current = shown.run_key
        lastSeq.current = 0
        const full = await agentsApi.live(agentId, 0, shown.run_key)
        setEvents(full.events)
        lastSeq.current = full.events.at(-1)?.seq ?? 0
        setRun(full.run)
        return full.running
      }
      if (view.events.length > 0) {
        setEvents((prev) => [...prev, ...view.events])
        lastSeq.current = view.events.at(-1)!.seq
      }
      setRun(shown)
      return view.running
    } catch (e) {
      setError(e instanceof ApiError ? `${e.status} — ${e.detail}` : 'Could not load the live log (network error)')
      return false
    }
  }, [agentId, pinnedRun])

  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | null = null
    const loop = async () => {
      const running = await poll()
      if (!stopped) timer = setTimeout(loop, running ? POLL_RUNNING_MS : POLL_IDLE_MS)
    }
    void loop()
    return () => {
      stopped = true
      if (timer) clearTimeout(timer)
    }
  }, [poll])

  useEffect(() => {
    const el = scroller.current
    if (el && atBottom.current) el.scrollTop = el.scrollHeight
  }, [events, maximized])

  const pick = (key: number | null) => {
    runKey.current = null
    lastSeq.current = 0
    setEvents([])
    setPinnedRun(key)
  }

  const body = (
    <div
      ref={scroller}
      onScroll={(e) => {
        const el = e.currentTarget
        atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
      }}
      className={maximized ? 'h-full overflow-auto' : 'max-h-96 overflow-auto'}
    >
      {events.length === 0 ? (
        <p className="py-2 text-sm text-muted">
          {runs.length === 0
            ? 'No runs since the API last started. Press Run now, or wait for the schedule.'
            : 'Waiting for events…'}
        </p>
      ) : (
        <ul data-testid="agent-live-events">
          {events.map((ev) => (
            <EventRow key={ev.seq} ev={ev} />
          ))}
        </ul>
      )}
    </div>
  )

  const header = (
    <div className="mb-2 flex flex-wrap items-center gap-2 text-sm">
      <span className="font-medium text-primary">Live log</span>
      {run && (
        <span
          data-testid="agent-live-status"
          className={`rounded px-1.5 py-0.5 text-xs ${
            run.status === 'running'
              ? 'animate-pulse bg-blue-100 text-blue-800 dark:bg-blue-900/50 dark:text-blue-200'
              : run.status === 'completed'
                ? 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-200'
                : 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-200'
          }`}
        >
          {run.status}
        </span>
      )}
      {runs.length > 0 && (
        <select
          data-testid="agent-live-run-select"
          aria-label="Run"
          value={pinnedRun ?? ''}
          onChange={(e) => pick(e.target.value ? Number(e.target.value) : null)}
          className="rounded border border-line bg-transparent px-1 py-0.5 text-xs text-primary"
        >
          <option value="">Latest run</option>
          {runs.map((r) => (
            <option key={r.run_key} value={r.run_key}>
              {time(r.started_at)} · {r.trigger} · {r.status}
            </option>
          ))}
        </select>
      )}
      {!maximized && (
        <button
          type="button"
          data-testid="agent-live-maximize"
          onClick={() => setMaximized(true)}
          className="ml-auto rounded border border-line px-2 py-0.5 text-xs text-primary hover:bg-hover"
        >
          Maximize
        </button>
      )}
    </div>
  )

  return (
    <div data-testid="agent-live-log" data-no-card-toggle className="mt-4 rounded border border-line p-3 cursor-auto">
      {error && (
        <p role="alert" className="mb-2 text-xs text-red-600 dark:text-red-400">
          {error}
        </p>
      )}
      {maximized ? (
        <>
          <p className="text-sm text-muted">Shown full screen.</p>
          <MaximizeOverlay title={`${agentName} · live log`} onClose={() => setMaximized(false)}>
            <div className="flex h-full flex-col">
              {header}
              <div className="min-h-0 flex-1">{body}</div>
            </div>
          </MaximizeOverlay>
        </>
      ) : (
        <>
          {header}
          {body}
        </>
      )}
    </div>
  )
}
