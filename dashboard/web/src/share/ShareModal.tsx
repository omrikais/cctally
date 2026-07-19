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
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import type { SharePanelId, ShareOptions, ShareTemplate } from './types';
import { SELECTION_LABEL } from './types';
import type { DashboardSelection } from '../types/envelope';
import { fetchTemplates, ShareApiError } from './api';
import { TemplateGallery } from './TemplateGallery';
import { Knobs } from './Knobs';
import { PreviewPane } from './PreviewPane';
import { ActionBar } from './ActionBar';
import { PresetDropdown } from './PresetDropdown';
import { ManagePresetsModal } from './ManagePresetsModal';
import { sharePanelLabel } from './panelLabels';
import { useKeymap } from '../hooks/useKeymap';
import { useModalFocus } from '../hooks/useModalFocus';
import { useScrollLock } from '../hooks/useScrollLock';
import { useIsMobile } from '../hooks/useIsMobile';
import {
  getState,
  subscribeStore,
  topmostStoreFocusLayer,
} from '../store/store';
import { ModalHeader } from '../modals/ModalHeader';
import { ModalCloseButton } from '../modals/ModalCloseButton';

interface Props {
  panel: SharePanelId;
  // #294 S5 §7 — the source the flow was captured under (from shareModal.source).
  // Optional with a 'claude' default: production always supplies it via
  // ShareModalRoot; the default keeps the compatibility path for older callers.
  source?: DashboardSelection;
  onClose: () => void;
  // Opaque per-panel params forwarded from the store's `shareModal.params`
  // slot (set by the opener via `dispatch(openShareModal(..., params))`).
  // Currently only the Projects modal supplies `windowWeeks`; merged into
  // the initial options so /api/share/render fetches carry the correct
  // window, instead of silently defaulting to the server's `1w`.
  initialParams?: { windowWeeks?: 1 | 4 | 8 | 12 };
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

export function ShareModal({ panel, source = 'claude', onClose, initialParams }: Props) {
  const cardRef = useRef<HTMLDivElement>(null);
  const panelLabel = sharePanelLabel(panel);
  const [templates, setTemplates] = useState<ShareTemplate[] | null>(null);
  const [templatesError, setTemplatesError] = useState<string | null>(null);
  const [selectedTemplateId, setSelectedTemplateId] = useState<string | null>(null);
  // Seed options with the caller-provided `windowWeeks` (Projects modal's
  // active pill — 1 / 4 / 8 / 12). Empty/undefined leaves the options
  // shape untouched so the server falls back to `1` per spec. Template
  // default_options can still override via the post-mount merge below,
  // but the merge is shallow — and `windowWeeks` is not one of the
  // template-controlled knobs — so the caller's value sticks.
  const [options, setOptions] = useState<ShareOptions>(() => {
    const base = defaultShareOptions();
    if (initialParams?.windowWeeks != null) {
      return { ...base, windowWeeks: initialParams.windowWeeks };
    }
    return base;
  });
  // Whether the user has interacted with the Knobs / Format radio. Once
  // true, we stop re-applying template default_options when the
  // selectedTemplateId changes — the user's preferences win.
  const userTouchedOptionsRef = useRef(false);
  // Manage-presets modal — opened from the PresetDropdown footer.
  const [manageOpen, setManageOpen] = useState(false);
  // Title id for aria-labelledby. Stable across renders.
  const titleId = 'share-modal-title';

  // M1-1: lock background page scroll. ShareModal mounts only when
  // ShareModalRoot's slot is non-null, so it's always "open" when mounted.
  useScrollLock(true);

  // Share is a store-tracked layer above panel/source-detail modals. Move focus
  // to its heading on mount and own Tab only while it remains topmost (the
  // composer can layer above it). ShareModalRoot retains trigger restoration.
  const trapEnabled = useSyncExternalStore(
    subscribeStore,
    () => topmostStoreFocusLayer(getState()) === 'share',
  );
  useModalFocus(cardRef, {
    active: true,
    trapEnabled,
    initialFocus: 'heading',
  });

  // SHARE-1 (#293 S4): on phone the Live preview leads the body so editing any
  // top-of-stack knob gives immediate feedback. `.share-preview-col` is nested
  // inside `.share-main-section` — a separate flex parent from the sibling
  // gallery — so a CSS `order` cannot hoist it; a render reorder is required.
  // Desktop keeps the two-pane (knobs | preview) layout byte-identical.
  const isMobile = useIsMobile();

  // Esc-to-close at overlay scope. Overlay > modal in SCOPE_ORDER so
  // Esc closes the share modal first when layered atop a panel modal.
  //
  // BUT: when <ManagePresetsModal> is open (nested inside this share
  // modal), suppress this overlay-scope binding so its `modal`-scope Esc
  // can fire and close just the nested manage modal. The keymap
  // dispatcher (store/keymap.ts) iterates registered bindings in
  // SCOPE_ORDER and fires the FIRST match — it does NOT consider DOM
  // focus. Without this `when:` gate, Esc inside the manage modal would
  // close the entire share modal instead of just the nested overlay.
  //
  // Same shape for the composer: when <ComposerModal> is layered above
  // this share modal (the "Customize…" / `B` path), both register Esc
  // at overlay scope. The composer's `when:` already gates on
  // `composerModal !== null`, so without this matching gate here the
  // dispatcher would fire both handlers on a single Escape press (the
  // composer first by registration order, then us). Gate ourselves out
  // whenever the composer slot is non-null. `getState()` is read at
  // fire time, not closure-captured, so we don't need to thread it
  // into the deps array.
  const bindings = useMemo(
    () => [{
      key: 'Escape',
      scope: 'overlay' as const,
      // Documentary (#159): mirrors `z-index: 200`. The when() guard already
      // gates this out whenever the composer is layered on top, so the layer
      // is never consulted today; it preserves the order if that guard is
      // ever removed. (`!manageOpen` is a separate cross-scope correction.)
      layer: 200,
      when: () => !manageOpen && getState().composerModal === null,
      action: onClose,
    }],
    [onClose, manageOpen],
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
      ref={cardRef}
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
      <ModalHeader
        title={`Share ${panelLabel} report`}
        titleId={titleId}
        className="share-modal-header"
      />
      {/* #294 S5 §7 — the flow's captured source, so the modal reads apart under
          Codex/All. The artifact itself carries the label chrome; this echoes it
          in-modal (the picker + every request are stamped with this source). */}
      <div className="share-modal-source" aria-label={`Source: ${SELECTION_LABEL[source]}`}>
        <span className={`source-chip source-chip--${source}`}>{SELECTION_LABEL[source]}</span>
      </div>

      <div className="share-modal-body">
        {isMobile && (
          <div className="share-preview-col share-preview-col--lead" aria-label="Live preview">
            <PreviewPane
              panel={panel}
              source={source}
              templateId={selectedTemplateId}
              options={options}
            />
          </div>
        )}
        <section className="share-section share-gallery-section" aria-label="Template gallery">
          <div className="share-gallery-header">
            <PresetDropdown
              panel={panel}
              onPick={(tid, opts) => {
                setSelectedTemplateId(tid);
                // Picking a preset is an explicit user choice — stop
                // re-applying template defaults from here on.
                userTouchedOptionsRef.current = true;
                setOptions((prev) => ({ ...prev, ...opts }));
              }}
              onManage={() => setManageOpen(true)}
            />
          </div>
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
          {!isMobile && (
            <div className="share-preview-col" aria-label="Live preview">
              <PreviewPane
                panel={panel}
                source={source}
                templateId={selectedTemplateId}
                options={options}
              />
            </div>
          )}
        </section>
      </div>

      <footer className="share-modal-footer">
        <ActionBar
          panel={panel}
          source={source}
          templateId={selectedTemplateId}
          options={options}
          onOptionsChange={handleOptionsChange}
        />
      </footer>

      {/* Close button — last in DOM, positioned absolutely into the
          header slot via CSS so tab order is correct without altering
          the visual layout. Esc remains the universal backstop. Rendered
          via the shared <ModalCloseButton> (single close-glyph source,
          #210) but kept OUT of the <ModalHeader> above precisely so it
          stays last in the DOM for spec §12.2 tab order. */}
      <ModalCloseButton
        className="share-modal-close"
        label="Close share modal"
        onClose={onClose}
      />

      <ManagePresetsModal
        open={manageOpen}
        onClose={() => setManageOpen(false)}
      />
    </div>
  );
}
