// The only module that imports Monaco (via monacoSetup). Load with React.lazy (Req 4.2, 4.13).
import { useEffect, useRef } from 'react'
import Editor, { type OnMount } from '@monaco-editor/react'
import { monaco } from './monacoSetup'

// Mirrors backend/workbench/validation.py LANGUAGES / language_for.
const LANGUAGES: Record<string, string> = {
  '.py': 'python', '.js': 'javascript', '.jsx': 'javascript', '.mjs': 'javascript',
  '.cjs': 'javascript', '.ts': 'typescript', '.tsx': 'typescript', '.json': 'json',
  '.md': 'markdown', '.html': 'html', '.htm': 'html', '.css': 'css', '.scss': 'scss',
  '.yaml': 'yaml', '.yml': 'yaml', '.toml': 'toml', '.sh': 'shell', '.bash': 'shell',
  '.zsh': 'shell', '.go': 'go', '.rs': 'rust', '.java': 'java', '.c': 'c', '.h': 'c',
  '.cpp': 'cpp', '.hpp': 'cpp', '.cc': 'cpp', '.rb': 'ruby', '.php': 'php',
  '.sql': 'sql', '.xml': 'xml', '.nix': 'nix', '.lua': 'lua', '.swift': 'swift',
  '.kt': 'kotlin', '.cs': 'csharp', '.dockerfile': 'dockerfile',
}

export function languageFor(path: string): string {
  const name = (path.split('/').pop() ?? '').toLowerCase()
  if (name === 'dockerfile') return 'dockerfile'
  const dot = name.lastIndexOf('.')
  if (dot <= 0) return 'plaintext'
  return LANGUAGES[name.slice(dot)] ?? 'plaintext'
}

export interface CodeEditorProps {
  path: string
  value: string
  onChange: (value: string) => void
  onSave: () => void
  /** Called with the currently selected text (empty string when nothing is selected). */
  onSelectionToOpencode?: (selectedText: string) => void
}

export default function CodeEditor({
  path, value, onChange, onSave, onSelectionToOpencode,
}: CodeEditorProps) {
  const saveRef = useRef(onSave)
  const selRef = useRef(onSelectionToOpencode)
  useEffect(() => {
    saveRef.current = onSave
    selRef.current = onSelectionToOpencode
  })

  const handleMount: OnMount = (editor) => {
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => saveRef.current())
    editor.addAction({
      id: 'workbench.sendSelectionToOpencode',
      label: 'Send selection to opencode',
      contextMenuGroupId: 'navigation',
      run: (ed) => {
        const model = ed.getModel()
        const sel = ed.getSelection()
        selRef.current?.(model && sel ? model.getValueInRange(sel) : '')
      },
    })
  }

  return (
    <Editor
      height="100%"
      path={path}
      language={languageFor(path)}
      value={value}
      theme="vs-dark"
      onChange={(v) => onChange(v ?? '')}
      onMount={handleMount}
      options={{ automaticLayout: true, minimap: { enabled: false } }}
    />
  )
}
