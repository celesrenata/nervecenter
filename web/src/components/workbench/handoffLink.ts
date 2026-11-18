/** Pure predicate for the Handoffs "Open in Browser IDE" link (Req 10.3, 10.4). */

export interface HandoffLinkInput {
  target: string
  repo: string | null
  project_slug?: string | null
}

export interface ProjectRef {
  kind: string
  slug: string
}

/** Repo slug of a handoff: the explicit project_slug, else the last path segment of `repo` minus `.git`. */
export function handoffRepoSlug(h: HandoffLinkInput): string | null {
  const explicit = h.project_slug?.trim()
  if (explicit) return explicit
  const repo = h.repo?.trim().replace(/\/+$/, '')
  if (!repo) return null
  const last = repo.split(/[/:]/).pop() ?? ''
  const slug = last.replace(/\.git$/, '')
  return slug || null
}

/** The Project slug to open, or null when the link must not be shown. */
export function browserIdeSlug(h: HandoffLinkInput, projects: readonly ProjectRef[]): string | null {
  if (h.target !== 'menagerie') return null
  const slug = handoffRepoSlug(h)
  if (!slug) return null
  return projects.some((p) => p.kind === 'menagerie' && p.slug === slug) ? slug : null
}

export const shouldShowBrowserIdeLink = (h: HandoffLinkInput, projects: readonly ProjectRef[]): boolean =>
  browserIdeSlug(h, projects) !== null
