import { useEffect, type ReactNode } from 'react'

/** Full-screen view of a panel; Esc or the close button returns to the page. */
export function MaximizeOverlay({
  title,
  onClose,
  children,
}: {
  title: string
  onClose: () => void
  children: ReactNode
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    const overflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = overflow
    }
  }, [onClose])

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={title}
      data-testid="maximize-overlay"
      className="fixed inset-0 z-50 flex flex-col bg-surface p-4 sm:p-8"
    >
      <div className="mb-3 flex items-center justify-between gap-4">
        <h2 className="truncate text-lg font-semibold text-primary">{title}</h2>
        <button
          type="button"
          data-testid="maximize-close"
          onClick={onClose}
          className="rounded border border-line px-3 py-1 text-sm text-primary hover:bg-hover"
        >
          Close (Esc)
        </button>
      </div>
      <div className="flex-1 overflow-auto rounded-lg bg-elevated p-4 sm:p-6">{children}</div>
    </div>
  )
}
