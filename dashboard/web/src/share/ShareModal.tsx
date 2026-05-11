// The share modal shell (spec §6.2 anatomy, plan §M1.11).
//
// Renders inside <ShareModalRoot>. Owns the modal's local state machine
// (template fetch, selected template id, the current `ShareOptions`
// recipe) and threads it down to the four child components:
//
//   <TemplateGallery>  → controls selectedTemplateId
//   <Knobs>            → controls `options` (period/theme/top-n/...)
//   <PreviewPane>      → reads {panel, templateId, options} and renders
//                        the iframe/<pre> preview via /api/share/render
//   <ActionBar>        → reads {panel, templateId, options} for export
//                        actions (Copy / Download / Open / disabled M4 stubs)
//
// Keyboard: an overlay-scoped Esc binding closes the modal. Overlay
// sits above modal in SCOPE_ORDER, so when the share modal is layered
// on top of a panel modal (which also registers Esc at `modal` scope)
// Esc closes the share modal first — preserving the spec §12.1
// "topmost overlay" invariant. Other modal shortcuts are handled by
// their own child components.
//
// a11y (spec §12.4): role="dialog" aria-modal="true"
// aria-labelledby="share-modal-title". The close button is reachable via
// Esc as a backstop even if the user has tabbed past it.
import { useEffect, useMemo, useRef, useState } from 'react';
import type { SharePanelId, ShareOptions, ShareTemplate } from './types';
import { fetchTemplates, ShareApiError } from './api';
import { TemplateGallery } from './TemplateGallery';
import { Knobs } from './Knobs';
import { PreviewPane } from './PreviewPane';
import { ActionBar } from './ActionBar';
import { sharePanelLabel } from './panelLabels';
import { useKeymap } from '../hooks/useKeymap';

interface Props {
  panel: SharePanelId;
  onClose: () => void;
}

// Fallback defaults — used when template `default_options` are missing
// or only partially override. `reveal_projects: false` is the
// spec-Q7/§6.3 "anon by default on export" contract; safe to apply at
// this layer because <PreviewPane> forces `reveal_projects: true` on
// its own fetch (the preview always reveals).
function defaultShareOptions(): ShareOptions {
  return {
    format: 'md',
    theme: 'light',
    reveal_projects: false,
    no_branding: false,
    top_n: 5,
    period: { kind: 'current' },
    project_allowlist: null,
    show_chart: true,
    show_table: true,
  };
}

function mergeOptions(base: ShareOptions, override: Partial<ShareOptions> | undefined): ShareOptions {
  if (!override) return base;
  // Shallow-merge with `period` deep-merged since SharePeriod has nested
  // start/end fields the template may want to set.
  const next: ShareOptions = { ...base, ...override };
  if (override.period) {
    next.period = { ...base.period, ...override.period };
  }
  return next;
}

export function ShareModal({ panel, onClose }: Props) {
  const panelLabel = sharePanelLabel(panel);
  const [templates, setTemplates] = useState<ShareTemplate[] | null>(null);
  const [templatesError, setTemplatesError] = useState<string | null>(null);
  const [selectedTemplateId, setSelectedTemplateId] = useState<string | null>(null);
  const [options, setOptions] = useState<ShareOptions>(() => defaultShareOptions());
  // Whether the user has interacted with the Knobs / Format radio. Once
  // true, we stop re-applying template default_options when the
  // selectedTemplateId changes — the user's preferences win.
  const userTouchedOptionsRef = useRef(false);
  // Title id for aria-labelledby. Stable across renders.
  const titleId = 'share-modal-title';

  // Esc-to-close at overlay scope. Overlay > modal in SCOPE_ORDER so
  // Esc closes the share modal first when layered atop a panel modal.
  const bindings = useMemo(
    () => [{ key: 'Escape', scope: 'overlay' as const, action: onClose }],
    [onClose],
  );
  useKeymap(bindings);

  // Fetch templates for this panel on mount. Errors are surfaced inside
  // <TemplateGallery>; the rest of the modal (Knobs/Preview/Actions)
  // still mounts so the user can hit Esc.
  useEffect(() => {
    let cancelled = false;
    setTemplates(null);
    setTemplatesError(null);
    fetchTemplates(panel)
      .then((resp) => {
        if (cancelled) return;
        setTemplates(resp.templates);
        // Default to the first template (in M1 each panel has exactly
        // one Recap entry; M2 expands to 3 per panel).
        const first = resp.templates[0];
        if (first) {
          setSelectedTemplateId(first.id);
          // Seed options from the template's default_options — but only
          // if the user has not yet touched the form.
          if (!userTouchedOptionsRef.current) {
            setOptions((prev) => mergeOptions(prev, first.default_options));
          }
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const msg =
          err instanceof ShareApiError
            ? `Couldn't load templates: ${err.message ?? `HTTP ${err.status}`}`
            : `Couldn't load templates: ${(err as Error).message}`;
        setTemplatesError(msg);
      });
    return () => {
      cancelled = true;
    };
  }, [panel]);

  // Re-seed options when the selected template changes (unless the user
  // has already interacted with the form — their values are intentional).
  useEffect(() => {
    if (!templates || !selectedTemplateId) return;
    if (userTouchedOptionsRef.current) return;
    const tmpl = templates.find((t) => t.id === selectedTemplateId);
    if (!tmpl) return;
    setOptions((prev) => mergeOptions(prev, tmpl.default_options));
  }, [selectedTemplateId, templates]);

  const handleOptionsChange = (next: ShareOptions) => {
    userTouchedOptionsRef.current = true;
    setOptions(next);
  };

  return (
    <div
      className="share-modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      // Clicks inside the card do not propagate to the overlay's
      // click-outside handler.
      onClick={(e) => e.stopPropagation()}
    >
      {/* Header renders the title only. The close button is appended
          to the modal's tail (after the footer) so the natural tab
          order matches spec §12.2 (tiles → knobs → format → actions →
          save preset → close). The close button is then re-positioned
          to its visual top-right slot via CSS
          `.share-modal-close { position: absolute; top: 12px; right: 18px }`. */}
      <header className="share-modal-header">
        <h2 id={titleId}>Share {panelLabel} report</h2>
      </header>

      <div className="share-modal-body">
        <section className="share-section share-gallery-section" aria-label="Template gallery">
          <TemplateGallery
            panel={panel}
            templates={templates}
            error={templatesError}
            selectedTemplateId={selectedTemplateId}
            onSelect={(id) => setSelectedTemplateId(id)}
          />
        </section>

        <section className="share-section share-main-section">
          <div className="share-knobs-col" aria-label="Render options">
            <Knobs options={options} onChange={handleOptionsChange} />
          </div>
          <div className="share-preview-col" aria-label="Live preview">
            <PreviewPane
              panel={panel}
              templateId={selectedTemplateId}
              options={options}
            />
          </div>
        </section>
      </div>

      <footer className="share-modal-footer">
        <ActionBar
          panel={panel}
          templateId={selectedTemplateId}
          options={options}
          onOptionsChange={handleOptionsChange}
        />
      </footer>

      {/* Close button — last in DOM, positioned absolutely into the
          header slot via CSS so tab order is correct without altering
          the visual layout. Esc remains the universal backstop. */}
      <button
        type="button"
        className="share-modal-close"
        aria-label="Close share modal"
        onClick={onClose}
      >
        ⤬
      </button>
    </div>
  );
}
