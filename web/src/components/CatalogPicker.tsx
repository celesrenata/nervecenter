import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react'
import { apiFetch } from '../api/client'

/**
 * Searchable picker over OmniRoute's model/combo catalog (~1000 entries).
 *
 * Searches the server (GET /api/inference/catalog) as you type, shows up to
 * 50 matches with the total, keyboard navigation (arrows, Enter, Escape) and
 * still accepts free text (a model id is always a string). ``localOptions``
 * (e.g. a provider's recalled models) are listed first when they match.
 */
export type CatalogKind = 'all' | 'model' | 'combo'

interface Item {
  id: string
  owned_by: string
  kind: 'model' | 'combo'
}

interface Props {
  id: string
  testId: string
  value: string
  onChange: (value: string) => void
  kind?: CatalogKind
  placeholder?: string
  localOptions?: string[]
  disabled?: boolean
}

const LIMIT = 50

export function CatalogPicker({ id, testId, value, onChange, kind = 'all', placeholder, localOptions = [], disabled }: Props) {
  const listId = useId()
  const rootRef = useRef<HTMLDivElement | null>(null)
  const [open, setOpen] = useState(false)
  const [highlight, setHighlight] = useState(0)
  const [items, setItems] = useState<Item[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(false)

  // Search the server while open (debounced); the typed text is the query.
  useEffect(() => {
    if (!open) return
    let live = true
    setLoading(true)
    const timer = window.setTimeout(async () => {
      try {
        const params = new URLSearchParams({ q: value.trim(), kind, limit: String(LIMIT) })
        const res = await apiFetch(`/api/inference/catalog?${params.toString()}`)
        if (!res.ok) throw new Error(String(res.status))
        const body = (await res.json()) as { items: Item[]; total: number }
        if (!live) return
        setItems(body.items)
        setTotal(body.total)
        setError(false)
      } catch {
        if (live) {
          setItems([])
          setTotal(0)
          setError(true)
        }
      } finally {
        if (live) setLoading(false)
      }
    }, 200)
    return () => {
      live = false
      window.clearTimeout(timer)
    }
  }, [open, value, kind])

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  const q = value.trim().toLowerCase()
  const local = localOptions
    .filter((o) => !q || o.toLowerCase().includes(q))
    .filter((o) => !items.some((i) => i.id === o))
    .slice(0, 20)
    .map((o): Item => ({ id: o, owned_by: 'provider', kind: 'model' }))
  const options = [...local, ...items]

  useEffect(() => {
    if (highlight >= options.length) setHighlight(0)
  }, [options.length, highlight])

  const pick = (v: string) => {
    onChange(v)
    setOpen(false)
  }

  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      if (!open) setOpen(true)
      else setHighlight((h) => Math.min(h + 1, options.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setHighlight((h) => Math.max(h - 1, 0))
    } else if (e.key === 'Enter') {
      if (open && options[highlight]) {
        e.preventDefault()
        pick(options[highlight].id)
      }
    } else if (e.key === 'Escape' && open) {
      e.stopPropagation()
      setOpen(false)
    }
  }

  const activeId = open && options[highlight] ? `${listId}-${highlight}` : undefined
  return (
    <div ref={rootRef} className="relative">
      <input
        id={id}
        data-testid={testId}
        role="combobox"
        aria-expanded={open}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={activeId}
        autoComplete="off"
        value={value}
        disabled={disabled}
        placeholder={placeholder ?? (kind === 'combo' ? 'Search combos…' : 'Search models…')}
        onChange={(e) => {
          onChange(e.target.value)
          setOpen(true)
          setHighlight(0)
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKey}
        className="w-full px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-primary"
      />
      {open && (
        <div className="absolute z-20 mt-1 w-full rounded-lg border border-line bg-elevated shadow-lg">
          <ul id={listId} role="listbox" data-testid={`${testId}-options`} className="max-h-72 overflow-auto py-1 text-sm">
            {options.map((o, i) => (
              <li
                key={`${o.owned_by}:${o.id}`}
                id={`${listId}-${i}`}
                data-testid={`${testId}-option-${i}`}
                role="option"
                aria-selected={i === highlight}
                onMouseDown={(e) => {
                  e.preventDefault()
                  pick(o.id)
                }}
                onMouseEnter={() => setHighlight(i)}
                className={`flex cursor-pointer items-center justify-between gap-2 px-3 py-1.5 ${
                  i === highlight ? 'bg-blue-50 dark:bg-blue-900/40' : ''
                }`}
              >
                <span className="truncate font-mono text-xs">{o.id}</span>
                <span className="shrink-0 text-xs text-muted">{o.kind === 'combo' ? 'combo' : o.owned_by}</span>
              </li>
            ))}
            {!loading && options.length === 0 && (
              <li className="px-3 py-2 text-muted">{error ? 'Could not load the catalog; you can still type a value.' : 'No matches; the typed value is kept.'}</li>
            )}
          </ul>
          <div className="border-t border-line px-3 py-1 text-xs text-muted" data-testid={`${testId}-count`}>
            {loading ? 'Searching…' : total > items.length ? `Showing ${items.length} of ${total} — keep typing to narrow` : `${total} match${total === 1 ? '' : 'es'}`}
          </div>
        </div>
      )}
    </div>
  )
}
