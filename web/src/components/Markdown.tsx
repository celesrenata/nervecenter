import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'

/**
 * GitHub-flavoured Markdown (tables, task lists, strikethrough, autolinks,
 * fenced code). Raw HTML in the source is not rendered, so model output cannot
 * inject markup; links open in a new tab without referrer.
 */
const components: Components = {
  h1: ({ children }) => <h1 className="mt-4 mb-2 text-xl font-bold text-primary">{children}</h1>,
  h2: ({ children }) => <h2 className="mt-4 mb-2 text-lg font-semibold text-primary">{children}</h2>,
  h3: ({ children }) => <h3 className="mt-3 mb-1 text-base font-semibold text-primary">{children}</h3>,
  h4: ({ children }) => <h4 className="mt-3 mb-1 text-sm font-semibold text-primary">{children}</h4>,
  p: ({ children }) => <p className="my-2 leading-relaxed">{children}</p>,
  ul: ({ children, className }) => (
    <ul className={`my-2 space-y-1 pl-5 ${className?.includes('contains-task-list') ? 'list-none pl-1' : 'list-disc'}`}>
      {children}
    </ul>
  ),
  ol: ({ children }) => <ol className="my-2 list-decimal space-y-1 pl-5">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer" className="text-blue-600 underline dark:text-blue-400">
      {children}
    </a>
  ),
  blockquote: ({ children }) => (
    <blockquote className="my-2 border-l-4 border-line pl-3 text-secondary">{children}</blockquote>
  ),
  hr: () => <hr className="my-4 border-line" />,
  table: ({ children }) => (
    <div className="my-3 overflow-x-auto">
      <table className="min-w-full border-collapse text-left text-xs">{children}</table>
    </div>
  ),
  th: ({ children, style }) => (
    <th style={style} className="border border-line bg-hover px-2 py-1 font-semibold">
      {children}
    </th>
  ),
  td: ({ children, style }) => (
    <td style={style} className="border border-line px-2 py-1 align-top">
      {children}
    </td>
  ),
  pre: ({ children }) => (
    <pre className="my-2 overflow-x-auto rounded bg-gray-900 p-3 text-xs text-gray-100">{children}</pre>
  ),
  code: ({ className, children }) =>
    className ? (
      <code className={`${className} font-mono`}>{children}</code>
    ) : (
      <code className="rounded bg-hover px-1 py-0.5 font-mono text-[0.85em]">{children}</code>
    ),
  input: ({ checked, type }) =>
    type === 'checkbox' ? <input type="checkbox" checked={!!checked} readOnly className="mr-1 align-middle" /> : null,
}

export function Markdown({ text, className = '' }: { text: string; className?: string }) {
  return (
    <div data-testid="markdown" className={`text-sm text-primary break-words ${className}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  )
}
