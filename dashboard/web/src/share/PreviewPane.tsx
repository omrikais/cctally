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
import { SELECTION_LABEL } from './types';
import type { DashboardSelection } from '../types/envelope';

interface Props {
  panel: SharePanelId;
  // #294 S5 §7 — the flow's captured source, stamped on the render body and
  // surfaced as label chrome so the preview matches what the artifact says.
  // Optional with a 'claude' default (compatibility path).
  source?: DashboardSelection;
  // #346 — frozen account qualifier for the lifetime of this share flow.
  account?: string | null;
  templateId: string | null;
  options: ShareOptions;
  // #503 S1 B1 — reports the render's `has_project_names` upward so the
  // modal's privacy status line can state what the export actually contains.
  // `null` means "not known yet", which the modal treats as the unchanged
  // two-state behavior rather than claiming there are no project names.
  //
  // Reported from HERE rather than fetched again by the modal, because this
  // component already owns the only `/api/share/render` call in the flow.
  // A second fetch would be a second source of truth and could disagree with
  // the preview the user is looking at.
  onProjectNamesResolved?: (hasProjectNames: boolean | null) => void;
  // #503 S3 §5 — reports whether the preview render FAILED, so the action
  // side can warn before the user clicks an export. Preview failure lived
  // only in this component's local state and never reached `ActionBar`,
  // which meant the first signal a user got was an error banner AFTER the
  // click. Reported from here for the same reason `onProjectNamesResolved`
  // is: this component owns the only `/api/share/render` call in the flow.
  // Lifted through props rather than `shareSlice`, which deliberately holds
  // only modal identity plus the captured source and account.
  onPreviewFailed?: (failed: boolean) => void;
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

export function PreviewPane({ panel, source = 'claude', account = null, templateId, options,
                              onProjectNamesResolved, onPreviewFailed }: Props) {
  const [preview, setPreview] = useState<PreviewState>(initialPreviewState);
  // Per-fetch AbortController, set when a fetch starts and aborted when
  // the next fetch starts (or the component unmounts).
  const abortRef = useRef<AbortController | null>(null);
  // Generation counter so a late-resolving promise from a stale fetch
  // cycle cannot stomp a fresher one (belt + suspenders with the
  // AbortController; some environments resolve fetch promises even
  // after abort).
  const genRef = useRef(0);

  // Hold the reporter in a ref so it can be called from the fetch effect
  // without joining its dependency array. An inline arrow prop would change
  // identity every parent render and re-trigger the debounce cycle, which
  // would make the preview refetch on every keystroke elsewhere in the modal.
  const reportRef = useRef(onProjectNamesResolved);
  reportRef.current = onProjectNamesResolved;
  const failedRef = useRef(onPreviewFailed);
  failedRef.current = onPreviewFailed;

  // Invalidate the reported answer when — and ONLY when — the thing it
  // describes changes. `has_project_names` is a property of (panel, template,
  // data); it does not depend on theme, format or the privacy toggle. Resetting
  // it on every options change would blank the modal's status line for the
  // 200ms debounce each time the user ticks the anonymize checkbox, which is
  // precisely the toggle whose effect the line exists to describe — so the
  // line would flash a statement about project names on a panel that has none.
  useEffect(() => {
    reportRef.current?.(null);
  }, [panel, source, account, templateId]);

  useEffect(() => {
    if (!templateId) {
      setPreview(initialPreviewState);
      failedRef.current?.(false);
      return;
    }
    setPreview((prev) => ({ ...prev, status: 'loading' }));
    // A new render is in flight, so any earlier failure no longer describes
    // what an export would do.
    failedRef.current?.(false);
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
        { panel, template_id: templateId, options: previewOptions, source, account },
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
          failedRef.current?.(false);
          // `undefined` from an older server stays `null` — unknown, not
          // "no project names".
          reportRef.current?.(
            typeof resp.has_project_names === 'boolean'
              ? resp.has_project_names
              : null,
          );
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
            failedRef.current?.(true);
            return;
          }
          setPreview({
            status: 'error',
            body: '',
            contentType: '',
            errorMessage: (err as Error).message ?? 'Unknown error',
            errorField: null,
          });
          failedRef.current?.(true);
        });
    }, PREVIEW_DEBOUNCE_MS);

    return () => {
      clearTimeout(timeout);
      // Don't abort here — that would tear down the in-flight fetch of
      // the CURRENT debounce cycle when it's still wanted. The next
      // effect's `setTimeout` aborts old in-flight requests at start.
      // On unmount we abort below in a separate effect.
    };
  }, [panel, source, account, templateId, options]);

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

  // ready — surface the source label chrome so the preview matches what the
  // artifact says (§7 Artifact chrome).
  const sourceChrome = (
    <div className="share-preview-source" aria-label={`Report source: ${SELECTION_LABEL[source]}`}>
      <span className={`source-chip source-chip--${source}`}>{SELECTION_LABEL[source]}</span>
    </div>
  );
  if (options.format === 'md') {
    return (
      <div className="share-preview-wrap">
        {sourceChrome}
        <pre className="share-preview share-preview-md" aria-label="Markdown preview">
          {preview.body}
        </pre>
      </div>
    );
  }

  // html / svg
  return (
    <div className="share-preview-wrap">
      {sourceChrome}
      <iframe
        className="share-preview share-preview-iframe"
        title="Report preview (decorative)"
        tabIndex={-1}
        // Static kernel output — no scripts needed.
        sandbox="allow-same-origin"
        srcDoc={preview.body}
      />
    </div>
  );
}
