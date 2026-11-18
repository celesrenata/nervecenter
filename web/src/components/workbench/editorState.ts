/**
 * Pure reducer for Code_Editor tabs, dirty flags and external-change notices
 * (Req 4.7, 4.8, 4.9). Must not import Monaco.
 *
 * Invariant: an external-change notice never alters a tab's `buffer`. The
 * buffer changes only through `edit` (the Owner typing), `reload` (explicit
 * reload action) and `saved` (successful save).
 */

export interface EditorTab {
  path: string
  /** Current editor content, including unsaved edits. */
  buffer: string
  /** Content as last loaded from or saved to disk. */
  saved: string
  /** Modification time (ns) of the on-disk file at last load/save. */
  baseMtimeNs: number
  /** True when `buffer` differs from `saved`. */
  dirty: boolean
  /** Set when the file changed on disk while open; cleared by reload or save. */
  externalChange: boolean
}

export interface EditorState {
  tabs: EditorTab[]
  activePath: string | null
}

export type EditorAction =
  | { type: 'open'; path: string; content: string; mtimeNs: number }
  | { type: 'close'; path: string }
  | { type: 'activate'; path: string }
  | { type: 'edit'; path: string; content: string }
  | { type: 'saved'; path: string; content: string; mtimeNs: number }
  | { type: 'external_change'; path: string }
  /** Explicit reload: `content` is the freshly fetched disk content. */
  | { type: 'reload'; path: string; content: string; mtimeNs: number }

export const initialEditorState: EditorState = { tabs: [], activePath: null }

function updateTab(state: EditorState, path: string, fn: (t: EditorTab) => EditorTab): EditorState {
  let found = false
  const tabs = state.tabs.map((t) => {
    if (t.path !== path) return t
    found = true
    return fn(t)
  })
  return found ? { ...state, tabs } : state
}

export function editorReducer(state: EditorState, action: EditorAction): EditorState {
  switch (action.type) {
    case 'open': {
      if (state.tabs.some((t) => t.path === action.path)) {
        return { ...state, activePath: action.path }
      }
      const tab: EditorTab = {
        path: action.path,
        buffer: action.content,
        saved: action.content,
        baseMtimeNs: action.mtimeNs,
        dirty: false,
        externalChange: false,
      }
      return { tabs: [...state.tabs, tab], activePath: action.path }
    }
    case 'close': {
      const idx = state.tabs.findIndex((t) => t.path === action.path)
      if (idx < 0) return state
      const tabs = state.tabs.filter((t) => t.path !== action.path)
      let activePath = state.activePath
      if (activePath === action.path) {
        activePath = tabs.length ? tabs[Math.min(idx, tabs.length - 1)].path : null
      }
      return { tabs, activePath }
    }
    case 'activate':
      return state.tabs.some((t) => t.path === action.path) ? { ...state, activePath: action.path } : state
    case 'edit':
      return updateTab(state, action.path, (t) => ({
        ...t,
        buffer: action.content,
        dirty: action.content !== t.saved,
      }))
    case 'saved':
      return updateTab(state, action.path, (t) => ({
        ...t,
        buffer: action.content,
        saved: action.content,
        baseMtimeNs: action.mtimeNs,
        dirty: false,
        externalChange: false,
      }))
    case 'external_change':
      // Notice only: the buffer is never touched, dirty or not.
      return updateTab(state, action.path, (t) => (t.externalChange ? t : { ...t, externalChange: true }))
    case 'reload':
      return updateTab(state, action.path, (t) => ({
        ...t,
        buffer: action.content,
        saved: action.content,
        baseMtimeNs: action.mtimeNs,
        dirty: false,
        externalChange: false,
      }))
    default:
      return state
  }
}

/** True while any tab has unsaved changes (drives the `beforeunload` handler, Req 4.7). */
export function hasDirtyTabs(state: EditorState): boolean {
  return state.tabs.some((t) => t.dirty)
}

/** Paths with unsaved changes, for the tab and file-tree indicators. */
export function dirtyPaths(state: EditorState): string[] {
  return state.tabs.filter((t) => t.dirty).map((t) => t.path)
}
