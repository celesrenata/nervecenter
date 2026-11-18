// A link plus a QR code for opening the same URL on a phone. The QR code
// holds only the URL: the phone still signs in through Keycloak SSO.
import { useEffect, useState } from 'react'
import QRCode from 'qrcode'

export default function QrLink({ url, label }: { url: string; label: string }) {
  const [src, setSrc] = useState<string | null>(null)
  const [show, setShow] = useState(false)
  useEffect(() => {
    if (!show) return
    let alive = true
    QRCode.toDataURL(url, { margin: 1, width: 220 })
      .then((d) => alive && setSrc(d))
      .catch(() => alive && setSrc(null))
    return () => {
      alive = false
    }
  }, [url, show])
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-3">
        <a href={url} target="_blank" rel="noopener noreferrer" className="underline">
          {label}
        </a>
        <button type="button" onClick={() => setShow((s) => !s)} aria-expanded={show}>
          {show ? 'Hide QR code' : 'Show QR code'}
        </button>
      </div>
      {show && src && (
        <figure className="inline-block">
          <img src={src} alt={`QR code for ${url}`} width={220} height={220} />
          <figcaption className="text-xs opacity-70">
            Scan to open on another device. You will sign in with SSO there.
          </figcaption>
        </figure>
      )}
    </div>
  )
}
