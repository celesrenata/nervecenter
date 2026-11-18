import { lazy, Suspense, useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  workbenchApi,
  type WorkbenchFileEntry,
  type WorkbenchFsOperation,
  type WorkbenchSession,
} from '../api/workbench'
import { dirtyPaths, editorReducer, hasDirtyTabs, initialEditorState } from '../components/workbench/editorState'
import SessionPanel from '../components/workbench/SessionPanel'

/**
 * WorkbenchEditorView: file tree, tabs, Code_Editor and Session panel for one
 * opencode Project (Req 4.1 to 4.11). Route wiring lives in App.tsx (task 13.8).
 */
const CodeEditor = lazy(() => import('../components/workbench/CodeEditor'))

export const SELECTION_MAX = 100_000
export const INSTRUCTION_MAX = 10_000
const POLL_MS = 3000
const btn = 'rounded border border-border px-2 py-0.5 text-xs disabled:opacity-50'

const errMsg = (e: unknown, fallback: string) => (e instanceof Error && e.message ? e.message : fallback)
const statusOf = (e: unknown) => (e && typeof e === 'object' ? (e as { status?: number }).status : undefined)
const detailOf = (e: unknown) => {
  const d = e && typeof e === 'object' ? (e as { detail?: unknown }).detail : undefined
  return typeof d === 'string' ? d : ''
}

/** Directories first, then names ascending (same ordering as the server listing, Req 4.1). */
export function sortTree(entries: WorkbenchFileEntry[]): WorkbenchFileEntry[] {
  const rank = (e: WorkbenchFileEntry) => (e.type === 'dir' ? 0 : 1)
  return [...entries].sort((a, b) => {
    if (rank(a) !== rank(b)) return rank(a) - rank(b)
    const an = a.name.toLowerCase()
    const bn = b.name.toLowerCase()
    if (an !== bn) return an < bn ? -1 : 1
    return a.name < b.name ? -1 : a.name > b.name ? 1 : 0
  })
}

export type SelectionError = 'no_session' | 'selection_empty' | 'selection_too_long' | 'instruction_empty' | 'instruction_too_long' | null

export function validateSelectionAction(
  sessionId: string | null, selection: string, instruction: string,
): SelectionError {
  if (!sessionId) return 'no_session'
  if (selection.length < 1) return 'selection_empty'
  if (selection.length > SELECTION_MAX) return 'selection_too_long'
  if (instruction.length < 1) return 'instruction_empty'
  if (instruction.length > INSTRUCTION_MAX) return 'instruction_too_long'
  return null
}

const SELECTION_ERRORS: Record<Exclude<SelectionError, null>, string> = {
  no_session: 'No active Session. Start or select a Session first.',
  selection_empty: 'Select some text first.',
  selection_too_long: `The selection is longer than ${SELECTION_MAX} characters.`,
  instruction_empty: 'The instruction cannot be empty.',
  instruction_too_long: `The instruction is longer than ${INSTRUCTION_MAX} characters.`,
}

const parentOf = (p: string) => (p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '')
const joinPath = (dir: string, name: string) => (dir ? `${dir}/${name}` : name)

export default function WorkbenchEditorView() {
  const { slug = '' } = useParams<{ slug: string }>()
  const [state, dispatch] = useReducer(editorReducer, initialEditorState)
  const stateRef = useRef(state)
  stateRef.current = state

  const [dirs, setDirs] = useState<Record<string, WorkbenchFileEntry[]>>({})
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [selected, setSelected] = useState<{ path: string; type: string } | null>(null)
  const [notSupported, setNotSupported] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [notices, setNotices] = useState<string[]>([])

  const [sessions, setSessions] = useState<WorkbenchSession[]>([])
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [pending, setPending] = useState<string | null>(null) // selected text awaiting an instruction
  const [instruction, setInstruction] = useState('')
  const [actionError, setActionError] = useState<string | null>(null)

  const loadDir = useCallback(async (path: string) => {
    try {
      const res = await workbenchApi.listFiles(slug, path)
      setDirs((d) => ({ ...d, [path]: sortTree(res.entries) }))
      setError(null)
    } catch (e) {
      if (statusOf(e) === 409 && detailOf(e).includes('editor_not_supported')) setNotSupported(true)
      else setError(errMsg(e, 'Could not load the file tree'))
    }
  }, [slug])

  useEffect(() => {
    setDirs({})
    setExpanded(new Set())
    setNotSupported(false)
    void loadDir('')
    workbenchApi.listSessions(slug).then((r) => {
      setSessions(r.sessions)
      setSessionId((cur) => cur ?? r.sessions.find((s) => s.status === 'running')?.id ?? r.sessions[0]?.id ?? null)
    }).catch(() => { /* Session panel reports opencode availability itself */ })
  }, [slug, loadDir])

  // ---- unsaved-changes prompt (Req 4.7) ----
  const dirty = hasDirtyTabs(state)
  useEffect(() => {
    if (!dirty) return
    const h = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = '' }
    window.addEventListener('beforeunload', h)
    return () => window.removeEventListener('beforeunload', h)
  }, [dirty])
  const dirtySet = useMemo(() => new Set(dirtyPaths(state)), [state])

  // ---- external changes (Req 4.8, 4.9) ----
  const noteChange = useCallback((path: string) => {
    setNotices((n) => (n.includes(path) ? n : [...n, path]))
    dispatch({ type: 'external_change', path })
  }, [])

  const changesToken = useRef<string | undefined>(undefined)
  useEffect(() => {
    let stopped = false
    const tick = async () => {
      try {
        const res = await workbenchApi.changes(slug, changesToken.current)
        if (stopped) return
        changesToken.current = res.token ?? changesToken.current
        res.paths.forEach(noteChange)
      } catch { /* polling is a fallback; ignore transient failures */ }
    }
    const t = window.setInterval(() => void tick(), POLL_MS)
    return () => { stopped = true; window.clearInterval(t) }
  }, [slug, noteChange])

  const reloadFile = async (path: string) => {
    try {
      const f = await workbenchApi.readFile(slug, path)
      if (stateRef.current.tabs.some((t) => t.path === path)) {
        dispatch({ type: 'reload', path, content: f.content ?? '', mtimeNs: f.mtime_ns })
      }
      setNotices((n) => n.filter((p) => p !== path))
      void loadDir(parentOf(path))
    } catch (e) {
      setError(errMsg(e, 'Could not reload the file'))
    }
  }

  // ---- open / save ----
  const openFile = async (path: string) => {
    if (stateRef.current.tabs.some((t) => t.path === path)) {
      dispatch({ type: 'activate', path })
      return
    }
    try {
      const f = await workbenchApi.readFile(slug, path)
      dispatch({ type: 'open', path, content: f.content ?? '', mtimeNs: f.mtime_ns })
      setError(null)
    } catch (e) {
      setError(errMsg(e, 'Could not open the file'))
    }
  }

  const save = async (path: string) => {
    const tab = stateRef.current.tabs.find((t) => t.path === path)
    if (!tab) return
    const snapshot = tab.buffer
    try {
      const res = await workbenchApi.saveFile(slug, { path, content: snapshot, base_mtime_ns: tab.baseMtimeNs })
      dispatch({ type: 'saved', path, content: snapshot, mtimeNs: res.mtime_ns })
      // Keep anything typed while the save was in flight.
      const latest = stateRef.current.tabs.find((t) => t.path === path)
      if (latest && latest.buffer !== snapshot) dispatch({ type: 'edit', path, content: latest.buffer })
      setSaveError(null)
      setNotices((n) => n.filter((p) => p !== path))
    } catch (e) {
      // The buffer is untouched on any failure, including a 409 conflict (Req 4.5).
      setSaveError(
        statusOf(e) === 409
          ? `${path} changed on disk since it was loaded. Your edits are kept; reload the file to see the disk version.`
          : errMsg(e, 'Save failed'),
      )
    }
  }

  const closeTab = (path: string) => {
    const tab = stateRef.current.tabs.find((t) => t.path === path)
    if (tab?.dirty && !window.confirm(`${path} has unsaved changes. Close anyway?`)) return
    dispatch({ type: 'close', path })
  }

  // ---- tree ----
  const toggleDir = (path: string) => {
    setExpanded((s) => {
      const n = new Set(s)
      if (n.has(path)) n.delete(path)
      else n.add(path)
      return n
    })
    if (!dirs[path]) void loadDir(path)
  }

  const targetDir = selected ? (selected.type === 'dir' ? selected.path : parentOf(selected.path)) : ''

  const runOp = async (op: WorkbenchFsOperation, refresh: string[]) => {
    const path = op.path
    const affected = stateRef.current.tabs.filter((t) => t.path === path || t.path.startsWith(`${path}/`))
    if (op.op !== 'create' && affected.some((t) => t.dirty)) {
      setError('Save or close the unsaved files under this path first.')
      return
    }
    try {
      await workbenchApi.fsOperation(slug, op)
      if (op.op !== 'create') affected.forEach((t) => dispatch({ type: 'close', path: t.path }))
      await Promise.all([...new Set(refresh)].map((d) => loadDir(d)))
      setSelected(null)
      setError(null)
    } catch (e) {
      setError(errMsg(e, 'File operation failed'))
    }
  }

  const create = (type: 'file' | 'dir') => {
    const name = window.prompt(`New ${type === 'dir' ? 'folder' : 'file'} name`)?.trim()
    if (!name) return
    const path = joinPath(targetDir, name)
    void runOp({ op: 'create', path, type }, [targetDir])
  }
  const rename = () => {
    if (!selected) return
    const name = window.prompt('New name', selected.path.split('/').pop())?.trim()
    if (!name) return
    void runOp({ op: 'rename', path: selected.path, new_name: name }, [parentOf(selected.path)])
  }
  const move = () => {
    if (!selected) return
    const dest = window.prompt('Move to directory (relative to project root, empty for root)', parentOf(selected.path))
    if (dest === null) return
    const d = dest.trim().replace(/^\/+|\/+$/g, '')
    void runOp({ op: 'move', path: selected.path, dest: d }, [parentOf(selected.path), d])
  }
  const remove = () => {
    if (!selected || !window.confirm(`Delete ${selected.path}?`)) return
    void runOp({ op: 'delete', path: selected.path }, [parentOf(selected.path)])
  }

  // ---- selection to opencode (Req 4.10, 4.11) ----
  const startSelectionAction = (text: string) => {
    setInstruction('')
    if (!sessionId) {
      setPending(null)
      setActionError(SELECTION_ERRORS.no_session)
      return
    }
    const bad = validateSelectionAction(sessionId, text, 'x')
    if (bad) {
      setPending(null)
      setActionError(SELECTION_ERRORS[bad])
      return
    }
    setActionError(null)
    setPending(text)
  }

  const sendSelectionAction = async () => {
    const path = stateRef.current.activePath
    const bad = validateSelectionAction(sessionId, pending ?? '', instruction)
    if (bad || !sessionId || !path || pending === null) {
      setActionError(SELECTION_ERRORS[bad ?? 'selection_empty'])
      return
    }
    try {
      await workbenchApi.selectionAction(sessionId, { path, selection: pending, instruction })
      setPending(null)
      setInstruction('')
      setActionError(null)
    } catch (e) {
      setActionError(errMsg(e, 'Could not send the request to opencode'))
    }
  }

  const newSession = async () => {
    try {
      const s = await workbenchApi.createSession(slug, {})
      setSessions((l) => [s, ...l])
      setSessionId(s.id)
      setError(null)
    } catch (e) {
      setError(errMsg(e, 'Could not start a Session'))
    }
  }

  if (notSupported) {
    return (
      <div className="space-y-2 p-4" data-testid="workbench-editor-view">
        <Link to="/workbench" className="text-sm underline">Back to Workbench</Link>
        <p role="alert" data-testid="editor-not-supported" className="text-sm text-danger">
          editor_not_supported: the Code_Editor is only available for opencode Projects. Use the Browser_IDE for menagerie Projects.
        </p>
      </div>
    )
  }

  const renderDir = (path: string, depth: number): JSX.Element[] =>
    (dirs[path] ?? []).flatMap((e) => {
      const isDir = e.type === 'dir'
      const open = isDir && expanded.has(e.path)
      const isSel = selected?.path === e.path
      const row = (
        <li key={e.path} role="treeitem" aria-selected={isSel} aria-expanded={isDir ? open : undefined}>
          <button
            type="button"
            className={`flex w-full items-center gap-1 rounded px-1 text-left text-sm hover:bg-white/5 ${isSel ? 'bg-white/10' : ''}`}
            style={{ paddingLeft: depth * 12 + 4 }}
            onClick={() => {
              setSelected({ path: e.path, type: e.type })
              if (isDir) toggleDir(e.path)
              else void openFile(e.path)
            }}
          >
            <span aria-hidden>{isDir ? (open ? '▾' : '▸') : ' '}</span>
            <span>{e.name}</span>
            {dirtySet.has(e.path) && (
              <span aria-label="unsaved" data-testid={`tree-dirty-${e.path}`} className="text-warning">●</span>
            )}
          </button>
        </li>
      )
      return open ? [row, ...renderDir(e.path, depth + 1)] : [row]
    })

  const active = state.tabs.find((t) => t.path === state.activePath) ?? null
  const activeSession = sessions.find((s) => s.id === sessionId)

  return (
    <div className="flex h-[calc(100vh-4rem)] flex-col gap-2 p-2" data-testid="workbench-editor-view">
      <div className="flex items-center gap-2">
        <Link to={`/workbench`} className="text-sm underline">Back to Workbench</Link>
        <h1 className="text-sm font-semibold">{slug}</h1>
      </div>
      {error && <p role="alert" className="text-sm text-danger">{error}</p>}
      {saveError && <p role="alert" data-testid="save-error" className="text-sm text-danger">{saveError}</p>}
      {notices.map((p) => (
        <div key={p} role="status" data-testid="file-changed-notice" className="flex items-center gap-2 text-sm">
          <span>{p} was changed by a Session.</span>
          <button type="button" className={btn} onClick={() => void reloadFile(p)}>Reload</button>
          <button type="button" className={btn} onClick={() => setNotices((n) => n.filter((x) => x !== p))}>Dismiss</button>
        </div>
      ))}

      <div className="flex min-h-0 flex-1 gap-2">
        <aside className="flex w-64 shrink-0 flex-col gap-1 overflow-hidden rounded border border-border p-1" aria-label="Files">
          <div className="flex flex-wrap gap-1">
            <button type="button" className={btn} onClick={() => create('file')}>New file</button>
            <button type="button" className={btn} onClick={() => create('dir')}>New folder</button>
            <button type="button" className={btn} disabled={!selected} onClick={rename}>Rename</button>
            <button type="button" className={btn} disabled={!selected} onClick={move}>Move</button>
            <button type="button" className={btn} disabled={!selected} onClick={remove}>Delete</button>
          </div>
          <ul role="tree" className="min-h-0 flex-1 overflow-auto">{renderDir('', 0)}</ul>
        </aside>

        <main className="flex min-w-0 flex-1 flex-col rounded border border-border">
          <div role="tablist" className="flex overflow-x-auto border-b border-border">
            {state.tabs.map((t) => (
              <div key={t.path} className={`flex items-center gap-1 px-2 py-1 text-sm ${t.path === state.activePath ? 'bg-white/10' : ''}`}>
                <button type="button" role="tab" aria-selected={t.path === state.activePath}
                  onClick={() => dispatch({ type: 'activate', path: t.path })}>
                  {t.path.split('/').pop()}
                  {t.dirty && <span aria-label="unsaved" data-testid={`tab-dirty-${t.path}`} className="ml-1 text-warning">●</span>}
                </button>
                <button type="button" aria-label={`Close ${t.path}`} onClick={() => closeTab(t.path)}>×</button>
              </div>
            ))}
          </div>
          <div className="min-h-0 flex-1">
            {active ? (
              <Suspense fallback={<p className="p-2 text-sm">Loading editor…</p>}>
                <CodeEditor
                  path={active.path}
                  value={active.buffer}
                  onChange={(v) => dispatch({ type: 'edit', path: active.path, content: v })}
                  onSave={() => void save(active.path)}
                  onSelectionToOpencode={startSelectionAction}
                />
              </Suspense>
            ) : (
              <p className="p-2 text-sm text-secondary">Select a file to edit.</p>
            )}
          </div>
          {(pending !== null || actionError) && (
            <div className="space-y-1 border-t border-border p-2 text-sm">
              {actionError && <p role="alert" data-testid="selection-error" className="text-danger">{actionError}</p>}
              {pending !== null && (
                <form onSubmit={(e) => { e.preventDefault(); void sendSelectionAction() }} className="space-y-1">
                  <label className="block">
                    Instruction for opencode ({pending.length} characters selected)
                    <textarea className="mt-1 w-full rounded border border-border bg-transparent p-1"
                      value={instruction} onChange={(e) => setInstruction(e.target.value)} />
                  </label>
                  <div className="flex gap-2">
                    <button type="submit" className={btn}>Send to opencode</button>
                    <button type="button" className={btn} onClick={() => { setPending(null); setActionError(null) }}>Cancel</button>
                  </div>
                </form>
              )}
            </div>
          )}
        </main>

        <aside className="flex w-80 shrink-0 flex-col gap-1 rounded border border-border p-1" aria-label="Sessions">
          <div className="flex items-center gap-1">
            <select aria-label="Active Session" className="min-w-0 flex-1 bg-transparent text-sm"
              value={sessionId ?? ''} onChange={(e) => setSessionId(e.target.value || null)}>
              <option value="">No Session</option>
              {sessions.map((s) => <option key={s.id} value={s.id}>{s.id}{s.status ? ` (${s.status})` : ''}</option>)}
            </select>
            <button type="button" className={btn} onClick={() => void newSession()}>New</button>
          </div>
          <div className="min-h-0 flex-1">
            {sessionId && (
              <SessionPanel
                key={sessionId}
                sessionId={sessionId}
                running={activeSession?.status === 'running'}
                model={activeSession?.model ?? ''}
                onFileChanged={noteChange}
              />
            )}
          </div>
        </aside>
      </div>
    </div>
  )
}
