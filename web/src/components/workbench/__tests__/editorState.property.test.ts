import { describe, it, expect } from 'vitest'
import * as fc from 'fast-check'
import { editorReducer, initialEditorState, type EditorAction, type EditorState } from '../editorState'

// Feature: opencode-menagerie-workbench, Property 11: Unsaved edits are never clobbered
// **Validates: Requirements 4.8, 4.9**

const paths = ['a.ts', 'b.ts', 'c.ts']
const pathArb = fc.constantFrom(...paths)
const contentArb = fc.string({ maxLength: 12 })
const mtimeArb = fc.nat()

const actionArb: fc.Arbitrary<EditorAction> = fc.oneof(
  fc.record({ type: fc.constant('open' as const), path: pathArb, content: contentArb, mtimeNs: mtimeArb }),
  fc.record({ type: fc.constant('close' as const), path: pathArb }),
  fc.record({ type: fc.constant('activate' as const), path: pathArb }),
  fc.record({ type: fc.constant('edit' as const), path: pathArb, content: contentArb }),
  fc.record({ type: fc.constant('saved' as const), path: pathArb, content: contentArb, mtimeNs: mtimeArb }),
  fc.record({ type: fc.constant('external_change' as const), path: pathArb }),
  fc.record({ type: fc.constant('reload' as const), path: pathArb, content: contentArb, mtimeNs: mtimeArb }),
)

const bufferOf = (s: EditorState, p: string) => s.tabs.find((t) => t.path === p)?.buffer

describe('editorState property: unsaved edits are never clobbered', () => {
  it('buffer changes only via edit, reload or saved; external_change never alters any buffer', () => {
    fc.assert(
      fc.property(fc.array(actionArb, { maxLength: 60 }), (actions) => {
        let state = initialEditorState
        for (const action of actions) {
          const next = editorReducer(state, action)
          for (const tab of state.tabs) {
            const before = tab.buffer
            const after = bufferOf(next, tab.path)
            if (after === undefined) continue // closed
            const touched =
              'path' in action && action.path === tab.path &&
              (action.type === 'edit' || action.type === 'reload' || action.type === 'saved')
            if (!touched) expect(after).toBe(before)
            if (action.type === 'external_change') {
              expect(after).toBe(before)
              if (tab.dirty) expect(next.tabs.find((t) => t.path === tab.path)!.dirty).toBe(true)
            }
          }
          state = next
        }
      }),
      { numRuns: 200 },
    )
  })

  it('a dirty buffer survives any number of external_change notices', () => {
    fc.assert(
      fc.property(contentArb, contentArb, fc.integer({ min: 1, max: 10 }), (disk, edit, n) => {
        fc.pre(edit !== disk)
        let s = editorReducer(initialEditorState, { type: 'open', path: 'a.ts', content: disk, mtimeNs: 1 })
        s = editorReducer(s, { type: 'edit', path: 'a.ts', content: edit })
        for (let i = 0; i < n; i++) s = editorReducer(s, { type: 'external_change', path: 'a.ts' })
        expect(s.tabs[0].buffer).toBe(edit)
        expect(s.tabs[0].dirty).toBe(true)
        expect(s.tabs[0].externalChange).toBe(true)
      }),
      { numRuns: 100 },
    )
  })
})
