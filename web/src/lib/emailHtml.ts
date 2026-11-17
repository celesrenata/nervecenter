import DOMPurify from 'dompurify'

/**
 * Email HTML hardening. Email HTML is hostile: it carries tracking pixels,
 * remote images, scripts, forms and CSS. `sanitizeEmailHtml` strips active
 * content and (by default) every remote fetch; `buildEmailSrcdoc` wraps the
 * result in a document with a strict CSP meta tag. The result must only ever
 * be rendered through an `<iframe sandbox srcdoc>` (see EmailBodyView).
 */

export interface SanitizedEmail {
  html: string
  /** Remote resources removed (images, srcset, backgrounds, CSS url()). */
  blockedRemote: number
}

const SAFE_LINK = /^(https?:|mailto:|tel:)/i
const INLINE = /^(data:image\/(png|gif|jpe?g|webp|bmp);|cid:)/i
const CSS_URL = /url\s*\(/i

function stripCssFetches(css: string): string {
  return css
    .replace(/@import[^;]*;?/gi, '')
    .replace(/(expression|behavior|-moz-binding)\s*[:(][^;}]*/gi, '')
}

/** Remove CSS url() references that are not inline data images. */
function dropRemoteCssUrls(css: string): { css: string; removed: number } {
  let removed = 0
  const out = stripCssFetches(css).replace(
    /url\s*\(\s*(['"]?)\s*([^)'"]*)\1\s*\)/gi,
    (_m, _q, target: string) => {
      if (/^data:image\//i.test(target.trim())) return `url(${target.trim()})`
      removed += 1
      return 'none'
    },
  )
  return { css: out, removed }
}

export function sanitizeEmailHtml(raw: string, allowRemote: boolean): SanitizedEmail {
  let blocked = 0
  const purify = DOMPurify(window)

  purify.addHook('afterSanitizeAttributes', (node: Element) => {
    const tag = node.nodeName.toLowerCase()

    if (tag === 'a') {
      const href = node.getAttribute('href')
      if (href !== null && !SAFE_LINK.test(href.trim())) node.removeAttribute('href')
      node.setAttribute('target', '_blank')
      node.setAttribute('rel', 'noopener noreferrer nofollow')
    }

    if (tag === 'img') {
      const src = node.getAttribute('src') ?? ''
      const remote = src !== '' && !INLINE.test(src.trim())
      node.removeAttribute('srcset')
      node.removeAttribute('loading')
      if (remote) {
        const https = /^https:\/\//i.test(src.trim())
        if (allowRemote && https) {
          node.setAttribute('referrerpolicy', 'no-referrer')
        } else {
          blocked += 1
          node.removeAttribute('src')
          node.setAttribute('data-blocked-src', '1')
          if (!node.getAttribute('alt')) node.setAttribute('alt', '[remote image blocked]')
        }
      }
    }

    // Legacy remote-fetching attributes.
    for (const attr of ['background', 'poster', 'ping', 'formaction', 'action']) {
      if (node.hasAttribute(attr)) {
        const value = node.getAttribute(attr) ?? ''
        node.removeAttribute(attr)
        if (attr === 'background' && value && !INLINE.test(value.trim())) blocked += 1
      }
    }

    const style = node.getAttribute('style')
    if (style !== null && (CSS_URL.test(style) || /@import|expression|behavior/i.test(style))) {
      const { css, removed } = allowRemote
        ? { css: stripCssFetches(style), removed: 0 }
        : dropRemoteCssUrls(style)
      blocked += removed
      node.setAttribute('style', css)
    }
  })

  purify.addHook('uponSanitizeElement', (node: Node, data: { tagName: string }) => {
    if (data.tagName === 'style') {
      const el = node as Element
      const text = el.textContent ?? ''
      const { css, removed } = allowRemote
        ? { css: stripCssFetches(text), removed: 0 }
        : dropRemoteCssUrls(text)
      blocked += removed
      el.textContent = css
    }
  })

  const html = purify.sanitize(raw, {
    // Whole document so <head><style> blocks survive; html/head/body wrappers
    // are ignored by the parser once embedded in the srcdoc body.
    WHOLE_DOCUMENT: true,
    ADD_TAGS: ['style'],
    FORBID_TAGS: [
      'script', 'iframe', 'frame', 'frameset', 'object', 'embed', 'applet',
      'form', 'input', 'button', 'textarea', 'select', 'option', 'meta',
      'base', 'link', 'svg', 'math', 'audio', 'video', 'source', 'track',
      'picture', 'template', 'noscript',
    ],
    FORBID_ATTR: ['srcset', 'formaction', 'action', 'ping', 'poster'],
    ALLOW_DATA_ATTR: false,
    ALLOW_UNKNOWN_PROTOCOLS: false,
    KEEP_CONTENT: true,
  }) as string
  purify.removeAllHooks()
  return { html, blockedRemote: blocked }
}

/** Strict CSP for the sandboxed document. Images stay inline-only until allowed. */
export function emailCsp(allowRemote: boolean): string {
  const img = allowRemote ? "img-src data: cid: https:" : 'img-src data: cid:'
  return [
    "default-src 'none'",
    "style-src 'unsafe-inline'",
    img,
    "base-uri 'none'",
    "form-action 'none'",
  ].join('; ')
}

export function buildEmailSrcdoc(sanitizedHtml: string, allowRemote: boolean): string {
  const csp = emailCsp(allowRemote)
  return (
    '<!doctype html><html><head><meta charset="utf-8">' +
    `<meta http-equiv="Content-Security-Policy" content="${csp}">` +
    '<meta name="referrer" content="no-referrer">' +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    '<style>' +
    'html{background:#ffffff}' +
    'body{margin:0;padding:12px;background:#ffffff;color:#1f2937;' +
    'font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;' +
    'overflow-wrap:anywhere;word-break:break-word;max-width:100%;box-sizing:border-box}' +
    'img{max-width:100%;height:auto}' +
    'table{max-width:100%}' +
    'pre{white-space:pre-wrap}' +
    'a{color:#1d4ed8}' +
    'img[data-blocked-src]{display:inline-block;min-width:24px;min-height:16px;' +
    'background:#f3f4f6;border:1px dashed #9ca3af;color:#6b7280;font-size:12px}' +
    'blockquote{border-left:3px solid #d1d5db;margin-left:0;padding-left:12px;color:#4b5563}' +
    '</style></head><body>' +
    sanitizedHtml +
    '</body></html>'
  )
}
