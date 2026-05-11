// Live-preview pane for the share modal (spec §6.4, plan §M1.14).
//
// Renders the result of POST /api/share/render in a sandboxed iframe
// (HTML/SVG) or a <pre> block (MD). The kernel reveals project names in
// the preview ALWAYS — `reveal_projects=true` is forced here regardless
// of the user's "Anon on export" checkbox. The Anon checkbox only
// affects the Copy/Download/Open paths (see ActionBar.tsx).
//
// Debounce: 200ms per spec §6.4. We debounce on every input that drives
// the render — panel/template/options. Any change resets the timer; the
// timer trigger fires the fetch. An AbortController guards against
// out-of-order resolves.
//
// Sandbox policy: iframe gets `sandbox="allow-same-origin"` (no
// allow-scripts). The kernel's HTML/SVG snapshots are static — no
// inline JS, no external requests — so we don't need scripting in the
// preview. `allow-same-origin` keeps blob/data behaviors stable across
// engines without unlocking attack surface.
import { useEffect, useRef, useState } from 'react';
import { renderShare, ShareApiError } from './api';
import type { ShareOptions, SharePanelId } from './types';

interface Props {
  panel: SharePanelId;
  templateId: string | null;
  options: ShareOptions;
}

interface PreviewState {
  status: 'idle' | 'loading' | 'ready' | 'error';
  body: string;
  contentType: string;
  errorMessage: string | null;
  errorField: string | null;
}

const PREVIEW_DEBOUNCE_MS = 200;

const initialPreviewState: PreviewState = {
  status: 'idle',
  body: '',
  contentType: '',
  errorMessage: null,
  errorField: null,
};

export function PreviewPane({ panel, templateId, options }: Props) {
  const [preview, setPreview] = useState<PreviewState>(initialPreviewState);
  // Per-fetch AbortController, set when a fetch starts and aborted when
  // the next fetch starts (or the component unmounts).
  const abortRef = useRef<AbortController | null>(null);
  // Generation counter so a late-resolving promise from a stale fetch
  // cycle cannot stomp a fresher one (belt + suspenders with the
  // AbortController; some environments resolve fetch promises even
  // after abort).
  const genRef = useRef(0);

  useEffect(() => {
    if (!templateId) {
      setPreview(initialPreviewState);
      return;
    }
    setPreview((prev) => ({ ...prev, status: 'loading' }));
    const myGen = ++genRef.current;

    const timeout = setTimeout(() => {
      // Abort prior in-flight fetch if any.
      abortRef.current?.abort();
      const ctl = new AbortController();
      abortRef.current = ctl;

      // Preview ALWAYS reveals project names (spec §6.3 "Preview always
      // reveals; export actions respect [Anon on export]").
      const previewOptions: ShareOptions = { ...options, reveal_projects: true };

      renderShare(
        { panel, template_id: templateId, options: previewOptions },
        { signal: ctl.signal },
      )
        .then((resp) => {
          if (myGen !== genRef.current) return;
          setPreview({
            status: 'ready',
            body: resp.body,
            contentType: resp.content_type,
            errorMessage: null,
            errorField: null,
          });
        })
        .catch((err: unknown) => {
          if (myGen !== genRef.current) return;
          if (
            err &&
            typeof err === 'object' &&
            (err as { name?: string }).name === 'AbortError'
          ) {
            return; // Aborted by next debounce cycle. Stay in loading.
          }
          if (err instanceof ShareApiError) {
            setPreview({
              status: 'error',
              body: '',
              contentType: '',
              errorMessage: err.message ?? `HTTP ${err.status}`,
              errorField: err.field ?? null,
            });
            return;
          }
          setPreview({
            status: 'error',
            body: '',
            contentType: '',
            errorMessage: (err as Error).message ?? 'Unknown error',
            errorField: null,
          });
        });
    }, PREVIEW_DEBOUNCE_MS);

    return () => {
      clearTimeout(timeout);
      // Don't abort here — that would tear down the in-flight fetch of
      // the CURRENT debounce cycle when it's still wanted. The next
      // effect's `setTimeout` aborts old in-flight requests at start.
      // On unmount we abort below in a separate effect.
    };
  }, [panel, templateId, options]);

  // Unmount cleanup: abort any pending request so React doesn't warn
  // about a setState on an unmounted component if a slow fetch resolves
  // after close.
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  if (!templateId) {
    return (
      <div className="share-preview share-preview-empty">
        Select a template to preview.
      </div>
    );
  }

  if (preview.status === 'error') {
    return (
      <div className="share-preview share-preview-error" role="alert">
        <div className="share-preview-error-title">Preview failed</div>
        <div className="share-preview-error-message">
          {preview.errorMessage}
          {preview.errorField ? (
            <span className="share-preview-error-field">
              {' '}(field: {preview.errorField})
            </span>
          ) : null}
        </div>
      </div>
    );
  }

  if (preview.status === 'loading' || preview.status === 'idle') {
    return (
      <div className="share-preview share-preview-loading" aria-busy="true">
        Rendering preview…
      </div>
    );
  }

  // ready
  if (options.format === 'md') {
    return (
      <pre className="share-preview share-preview-md" aria-label="Markdown preview">
        {preview.body}
      </pre>
    );
  }

  // html / svg
  return (
    <iframe
      className="share-preview share-preview-iframe"
      title="Report preview (decorative)"
      tabIndex={-1}
      // Static kernel output — no scripts needed.
      sandbox="allow-same-origin"
      srcDoc={preview.body}
    />
  );
}
