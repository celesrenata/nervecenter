import { describe, expect, it } from 'vitest'
import fc from 'fast-check'
import { groupProjects } from '../groupProjects'
import type { WorkbenchProject } from '../../../api/workbench'

// Feature: opencode-menagerie-workbench, Property 5: Project list ordering and grouping
// **Validates: Requirements 2.1, 3.3, 3.4**

const projectArb = fc.record({
  kind: fc.constantFrom('opencode', 'menagerie'),
  slug: fc.string({ minLength: 1, maxLength: 12 }),
  archived: fc.option(fc.boolean(), { nil: undefined }),
}) as fc.Arbitrary<WorkbenchProject>

const listArb = fc.array(projectArb, { maxLength: 40 })

describe('groupProjects (Property 5)', () => {
  it('one group per kind, count equals visible projects, archived only when filter on', () => {
    fc.assert(
      fc.property(listArb, fc.boolean(), (projects, includeArchived) => {
        const snapshot = JSON.stringify(projects)
        const groups = groupProjects(projects, includeArchived)
        const visible = projects.filter((p) => includeArchived || !p.archived)

        expect(JSON.stringify(projects)).toBe(snapshot)
        const kinds = groups.map((g) => g.kind)
        expect(new Set(kinds).size).toBe(kinds.length)
        expect(new Set(kinds)).toEqual(new Set(visible.map((p) => p.kind)))
        for (const g of groups) {
          expect(g.count).toBe(g.projects.length)
          expect(g.count).toBe(visible.filter((p) => p.kind === g.kind).length)
          expect(g.projects.every((p) => p.kind === g.kind)).toBe(true)
          if (!includeArchived) expect(g.projects.some((p) => p.archived)).toBe(false)
        }
        expect(groups.reduce((n, g) => n + g.count, 0)).toBe(visible.length)
      }),
      { numRuns: 200 },
    )
  })

  it('with filter on, archived projects are included', () => {
    const projects: WorkbenchProject[] = [
      { kind: 'opencode', slug: 'a', archived: true },
      { kind: 'opencode', slug: 'b' },
    ]
    expect(groupProjects(projects, true)[0].count).toBe(2)
    expect(groupProjects(projects, false)[0].count).toBe(1)
  })
})
