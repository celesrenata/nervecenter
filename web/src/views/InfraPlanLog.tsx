import { useEffect, useRef, useState } from 'react'
import type { InfraPlan, InfraStep } from '../api/infra'
import { MaximizeOverlay } from '../components/MaximizeOverlay'

function since(iso: string, now: number): string {
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000))
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
}

/**
 * One scrollable log for a plan: every finished step's output, then the
 * running step's output as it arrives. Follows the end unless you scroll up.
 */
export function InfraPlanLog({ p, describe }: { p: InfraPlan; describe: (s: InfraStep) => string }) {
  const [maximized, setMaximized] = useState(false)
  const [now, setNow] = useState(() => Date.now())
  const scroller = useRef<HTMLDivElement | null>(null)
  const follow = useRef(true)
  const live = p.live ?? null
  const active = p.status === 'approved' || p.status === 'running'

  useEffect(() => {
    if (!live) return
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [live])

  const results = [...p.results].sort((a, b) => a.index - b.index)
  const lines = results.map((r) => ({
    index: r.index,
    head: `▸ ${r.index + 1}/${p.steps.length} [${p.steps[r.index]?.connector}] ${describe(p.steps[r.index] ?? ({} as InfraStep))}`,
    state: r.skipped ? 'skipped' : r.ok ? 'ok' : `failed (exit ${r.exit ?? '-'})`,
    ok: r.ok,
    body: r.output,
  }))
  const text = lines.map((l) => l.body).join('') + (live?.output ?? '')

  useEffect(() => {
    const el = scroller.current
    if (el && follow.current) el.scrollTop = el.scrollHeight
  }, [text, maximized])

  if (results.length === 0 && !live && !active) return null

  const body = (
    <div
      ref={scroller}
      data-testid={`infra-plan-log-${p.id}`}
      onScroll={(e) => {
        const el = e.currentTarget
        follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
      }}
      className={`overflow-auto rounded bg-gray-900 p-3 font-mono text-xs text-gray-100 ${
        maximized ? 'h-full' : 'max-h-96'
      }`}
    >
      {lines.map((l) => (
        <div key={l.index} className="mb-2">
          <div className={l.ok ? 'text-green-400' : 'text-red-400'}>
            {l.head} — {l.state}
          </div>
          <pre className="whitespace-pre-wrap">{l.body}</pre>
        </div>
      ))}
      {live && (
        <div data-testid={`infra-plan-live-${p.id}`}>
          <div className="text-blue-300">
            <span className="mr-1 inline-block h-2 w-2 animate-pulse rounded-full bg-blue-400" />
            ▸ {live.index + 1}/{p.steps.length} [{p.steps[live.index]?.connector}]{' '}
            {describe(p.steps[live.index] ?? ({} as InfraStep))} — running {since(live.started_at, now)}
          </div>
          <pre className="whitespace-pre-wrap">{live.output || '(no output yet)'}</pre>
        </div>
      )}
      {!live && active && <div className="text-muted">Waiting for the next step…</div>}
    </div>
  )

  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2 text-xs text-muted">
        <span className="font-semibold uppercase tracking-wide">Live log</span>
        {active && <span className="animate-pulse text-blue-500">updating</span>}
        {!maximized && (
          <button
            type="button"
            data-testid={`infra-plan-log-maximize-${p.id}`}
            onClick={() => setMaximized(true)}
            className="ml-auto rounded border border-line px-2 py-0.5 text-primary hover:bg-hover"
          >
            Maximize
          </button>
        )}
      </div>
      {maximized ? (
        <MaximizeOverlay title={`${p.title} · ${p.status}`} onClose={() => setMaximized(false)}>
          {body}
        </MaximizeOverlay>
      ) : (
        body
      )}
    </div>
  )
}
