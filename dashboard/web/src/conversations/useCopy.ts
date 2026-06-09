import { useCallback, useEffect, useRef, useState } from 'react';

// Clipboard hook with a non-secure-context fallback (G2 §5a). On the happy
// path — loopback 127.0.0.1 / localhost is a secure context — it uses
// navigator.clipboard.writeText. When the dashboard is exposed over the LAN
// (`--host 0.0.0.0` → http://192.168.x.x), the browser leaves
// navigator.clipboard undefined; we then fall back to a hidden-<textarea> +
// document.execCommand('copy'). `copied` flips true ~1.1s then reverts; the
// timeout is cleared on unmount (no setState on an unmounted component).
export function useCopy(): { copied: boolean; copy: (text: string) => void } {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (timer.current != null) window.clearTimeout(timer.current);
    },
    [],
  );

  const copy = useCallback((text: string) => {
    const done = () => {
      setCopied(true);
      if (timer.current != null) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => {
        setCopied(false);
        timer.current = null;
      }, 1100);
    };
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(() => {});
      return;
    }
    // Non-secure-context (LAN http) fallback.
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      done();
    } catch {
      /* clipboard unavailable */
    }
  }, []);

  return { copied, copy };
}
