import { useEffect, useMemo, useRef, useState } from 'react'
import { buildEmailSrcdoc, sanitizeEmailHtml } from '../lib/emailHtml'

/**
 * EmailBodyView: one message body.
 *
 * HTML is sanitised (DOMPurify + remote-content blocking) and rendered only
 * inside a sandboxed iframe (no scripts, no same-origin, no forms) whose
 * srcdoc carries a strict CSP. Remote images are blocked until the owner
 * clicks "Load images" for this message (not persisted). Without an HTML
 * alternative, or when "View as plain text" is chosen, the readable text is
 * shown as text (never as markup).
 *
 * Auto-height: a sandboxed frame without allow-same-origin cannot be measured
 * by the parent, so an identical, script-less, CSP-locked throwaway frame
 * (allow-same-origin only, removed right after) is used purely to read the
 * content height; the visible frame never gets allow-same-origin.
 */

const VISIBLE_SANDBOX = 'allow-popups allow-popups-to-escape-sandbox'
const MEASURE_SANDBOX = 'allow-same-origin'
const MIN_HEIGHT = 40
const MAX_HEIGHT = 20000

export function EmailBodyView({
  text,
  html,
  tone = 'received',
}: {
  text: string
  html?: string | null
  tone?: 'received' | 'sent'
}) {
  const [plain, setPlain] = useState(false)
  const [allowRemote, setAllowRemote] = useState(false)
  const hasHtml = typeof html === 'string' && html.trim() !== ''

  const prepared = useMemo(() => {
    if (!hasHtml) return null
    const cleaned = sanitizeEmailHtml(html as string, allowRemote)
    return {
      blockedRemote: cleaned.blockedRemote,
      srcdoc: buildEmailSrcdoc(cleaned.html, allowRemote),
    }
  }, [hasHtml, html, allowRemote])

  // Count of remote resources is only knowable from the blocked pass.
  const blockedCount = useMemo(
    () => (hasHtml && !allowRemote ? (prepared?.blockedRemote ?? 0) : 0),
    [hasHtml, allowRemote, prepared],
  )

  const wrapRef = useRef<HTMLDivElement | null>(null)
  const [height, setHeight] = useState(120)
  const [width, setWidth] = useState(0)

  useEffect(() => {
    const el = wrapRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver((entries) => {
      const w = Math.round(entries[0]?.contentRect.width ?? 0)
      setWidth((prev) => (Math.abs(prev - w) > 1 ? w : prev))
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [plain, hasHtml])

  const srcdoc = prepared?.srcdoc ?? null
  useEffect(() => {
    if (!srcdoc || plain || width <= 0) return
    const probe = document.createElement('iframe')
    probe.setAttribute('sandbox', MEASURE_SANDBOX)
    probe.setAttribute('aria-hidden', 'true')
    probe.tabIndex = -1
    probe.style.cssText = `position:fixed;left:-99999px;top:0;visibility:hidden;border:0;width:${width}px;height:10px`
    let cancelled = false
    probe.addEventListener('load', () => {
      if (cancelled) return
      try {
        const h = probe.contentDocument?.documentElement.scrollHeight ?? 0
        if (h > 0) setHeight(Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, h)))
      } finally {
        probe.remove()
      }
    })
    probe.srcdoc = srcdoc
    document.body.appendChild(probe)
    return () => {
      cancelled = true
      probe.remove()
    }
  }, [srcdoc, width, plain])

  const textView = (
    <p
      data-testid="email-body-text"
      className="whitespace-pre-wrap break-words [overflow-wrap:anywhere]"
    >
      {text}
    </p>
  )

  if (!hasHtml) return textView

  const sent = tone === 'sent'
  const btn = sent
    ? 'rounded border border-blue-200 px-2 py-0.5 text-xs text-blue-50 hover:bg-blue-500'
    : 'rounded border border-gray-300 dark:border-gray-600 px-2 py-0.5 text-xs text-gray-700 dark:text-gray-200 hover:bg-gray-200 dark:hover:bg-gray-700'

  return (
    <div className="space-y-2" data-testid="email-body">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          className={btn}
          aria-pressed={plain}
          onClick={() => setPlain((v) => !v)}
        >
          {plain ? 'View formatted' : 'View as plain text'}
        </button>
      </div>
      {!plain && blockedCount > 0 && (
        <div
          role="status"
          data-testid="remote-content-banner"
          className="flex flex-wrap items-center gap-2 rounded border border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/20 px-2 py-1 text-xs text-amber-800 dark:text-amber-200"
        >
          <span>Remote content blocked. Loading it tells the sender you read this mail.</span>
          <button
            type="button"
            className="rounded border border-amber-400 px-2 py-0.5 font-medium hover:bg-amber-100 dark:hover:bg-amber-800/40"
            onClick={() => setAllowRemote(true)}
          >
            Load images
          </button>
        </div>
      )}
      {plain ? (
        textView
      ) : (
        <div ref={wrapRef} className="w-full max-w-full overflow-hidden rounded border border-gray-300 dark:border-gray-600 bg-white">
          <iframe
            title="Email content"
            data-testid="email-html-frame"
            sandbox={VISIBLE_SANDBOX}
            referrerPolicy="no-referrer"
            srcDoc={srcdoc ?? ''}
            scrolling="no"
            style={{ width: '100%', height, border: 0, display: 'block', background: '#ffffff' }}
          />
        </div>
      )}
    </div>
  )
}
