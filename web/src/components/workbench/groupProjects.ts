/** Pure grouping of Workbench projects by Workspace_Kind (Req 3.3, 3.4). */
import type { WorkbenchProject } from '../../api/workbench'

export interface ProjectGroup {
  kind: string
  count: number
  projects: WorkbenchProject[]
}

/**
 * Group projects by kind. Archived projects are dropped unless
 * `includeArchived` is true. Groups are sorted by kind; empty groups are omitted.
 * `count` always equals `projects.length`. Input is not mutated.
 */
export function groupProjects(projects: readonly WorkbenchProject[], includeArchived: boolean): ProjectGroup[] {
  const byKind = new Map<string, WorkbenchProject[]>()
  for (const p of projects) {
    if (p.archived && !includeArchived) continue
    const list = byKind.get(p.kind)
    if (list) list.push(p)
    else byKind.set(p.kind, [p])
  }
  return [...byKind.entries()]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([kind, list]) => ({ kind, count: list.length, projects: list }))
}
