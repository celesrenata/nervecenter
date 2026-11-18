import { describe, it, expect } from 'vitest'
import * as fc from 'fast-check'
import { handoffRepoSlug, shouldShowBrowserIdeLink, type ProjectRef } from '../handoffLink'

// Feature: opencode-menagerie-workbench, Property 27: Handoff selection and the Browser IDE link
// **Validates: Requirements 10.3, 10.4**

const slugArb = fc.stringMatching(/^[a-z0-9][a-z0-9-]{0,15}$/)
const kindArb = fc.constantFrom('menagerie', 'opencode')
const projectArb: fc.Arbitrary<ProjectRef> = fc.record({ kind: kindArb, slug: slugArb })
const projectsArb = fc.array(projectArb, { maxLength: 8 })
const targetArb = fc.constantFrom('menagerie', 'opencode')
const handoffArb = fc.record({
  target: targetArb,
  repo: fc.option(slugArb.map((s) => `https://example.com/org/${s}.git`), { nil: null }),
  project_slug: fc.option(slugArb, { nil: null }),
})

const opts = { numRuns: 100 }

describe('Property 27: Browser IDE link predicate', () => {
  it('shown iff target is menagerie and a menagerie Project has the handoff repo slug', () => {
    fc.assert(
      fc.property(handoffArb, projectsArb, (h, projects) => {
        const slug = handoffRepoSlug(h)
        const expected =
          h.target === 'menagerie' &&
          slug !== null &&
          projects.some((p) => p.kind === 'menagerie' && p.slug === slug)
        expect(shouldShowBrowserIdeLink(h, projects)).toBe(expected)
      }),
      opts,
    )
  })

  it('never shown for opencode targets', () => {
    fc.assert(
      fc.property(handoffArb, projectsArb, (h, projects) => {
        const all = [...projects, ...(handoffRepoSlug(h) ? [{ kind: 'menagerie', slug: handoffRepoSlug(h)! }] : [])]
        expect(shouldShowBrowserIdeLink({ ...h, target: 'opencode' }, all)).toBe(false)
      }),
      opts,
    )
  })

  it('never shown when only a project of the other kind has the slug', () => {
    fc.assert(
      fc.property(slugArb, projectsArb, (slug, others) => {
        const projects = [
          ...others.filter((p) => !(p.kind === 'menagerie' && p.slug === slug)),
          { kind: 'opencode', slug },
        ]
        expect(shouldShowBrowserIdeLink({ target: 'menagerie', repo: null, project_slug: slug }, projects)).toBe(false)
      }),
      opts,
    )
  })

  it('shown when a matching menagerie Project exists for a menagerie handoff', () => {
    fc.assert(
      fc.property(slugArb, projectsArb, (slug, others) => {
        const projects = [...others, { kind: 'menagerie', slug }]
        expect(shouldShowBrowserIdeLink({ target: 'menagerie', repo: null, project_slug: slug }, projects)).toBe(true)
      }),
      opts,
    )
  })
})
