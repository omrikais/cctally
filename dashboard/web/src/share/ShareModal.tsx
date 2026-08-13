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
  // #346 — frozen with source by OPEN_SHARE; null means account-agnostic.
  account?: string | null;
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

// #503 S1 F6 — the two-state export-anonymization status line.
//
// Three correct-in-isolation decisions combine into a modal that cannot answer
// its own question. <PreviewPane> forces `reveal_projects: true` (correct per
// spec §6.3, so you can verify what you are about to share), <ShareModal>
// defaults the option to `false` (correct — anonymize by default), and <Knobs>
// binds a checkbox to it (correct). The result is a checkbox that changes
// nothing visible, next to a preview that always shows real names, on a
// surface documented as the place to review what you are about to share.
//
// The line is ALWAYS present and CHANGES TEXT rather than appearing and
// disappearing. Two reasons. A line that vanishes in the safe state says
// nothing, which is the current defect with extra steps — the composer's
// `composer-anon-banner` is conditional, but it is right to be, because it is
// a call to action carrying an "Anonymize all" button and F6 asks for
// something different. And because the text changes rather than appears, a
// screen-reader user hears the new state announced on toggle, which is the
// feedback that does not exist today.
//
// Severity uses the three-tier vocabulary in
// dashboard/design-system/components/alert-severity.html. Info and warn,
// NEVER critical: revealing project names is a legitimate deliberate choice,
// not an error.
//
// #503 S1 B1 — a THIRD, NEUTRAL state, because the two-state line made a false
// statement on the templates whose artifacts carry no project name at all. The
// real-browser QA gate measured it: a `trend`, `blocks` or `forecast` export
// differs between the two privacy modes only in the `anonymized:` frontmatter
// flag and the timestamp. On those, the reveal state promised real project
// names the artifact does not contain, and the anonymize state claimed the
// preview was showing real names when it showed none.
//
// This is an over-warning rather than a leak, so it cannot cause the defect
// class this session exists to prevent. It matters because a line whose entire
// value is trustworthiness must not be wrong, and a warning a user learns to
// disregard on Forecast is one they may disregard on Projects.
//
// `hasProjectNames` comes from the server's additive `has_project_names`,
// derived from the same snapshot the renderer used. NOT from a hardcoded list:
// the property is finer than a panel — one template inside a panel can carry
// project names while its sibling carries none — and it is not fixed by the
// code at all, because it depends on the data the panel actually holds. A
// `daily` panel that happens to hold no project data is the same case.
//
// The mechanism, worked through: `sessions-visual` normally reports that
// project names are present, because its chart is keyed by project. When the
// user has edited an option before switching archetype, the modal carries
// `show_chart: false` into that template; the render then genuinely has no
// chart and therefore no project name, and the neutral state is truthful. No
// hardcoded list could produce that answer, and no count of "how many
// templates are neutral" survives contact with a second dataset — three
// measurements taken during this session disagreed for exactly that reason.
//
// `null` means not known yet — an older server, or the interval before the
// first render resolves. It keeps the two original states, because claiming
// "no project names" without evidence is the failure this change is removing,
// only in the more dangerous direction.
//
// The neutral sentence is true in BOTH toggle positions, so this state does
// not change when the user ticks the checkbox. That is intended: there is
// nothing for the toggle to change.
function SharePrivacyStatus({
  revealProjects,
  hasProjectNames,
}: {
  revealProjects: boolean;
  hasProjectNames: boolean | null;
}) {
  const neutral = hasProjectNames === false;
  const severity = neutral ? 'neutral' : revealProjects ? 'warn' : 'info';
  // #503 S1 B2 — every string here fits ONE line at 390px.
  //
  // The mobile layout pins this line above a capped preview strip, so each
  // wrapped line comes straight out of the preview. Measured at 390x844
  // against the then-current 128px cap: the previous info wording took two
  // lines and left 37px of preview — about two lines of the artifact, on a
  // control whose documented purpose is letting the user verify what they are
  // about to share. At one line all three states left 58px. #503 S4 §4.2
  // raised that cap to `min(50dvh, 380px)`; the one-line rule stays, because
  // it is what keeps the three states costing the same.
  //
  // The share modal inherits the monospace stack, so the budget is a
  // character count: 47 characters at 12px in the 359px column. (Not every
  // surface in the client is mono — the conversation-viewer prose surfaces in
  // `index.css` set `font-family: var(--font-prose)`, and the bundle ships
  // Newsreader for them. None of them is an ancestor of this modal.) Shortening
  // "Preview shows real project names. Export will be anonymized." (60) to
  // the 47-character form below is what buys the line back. Check any
  // rewording against that limit — 48 characters wraps and costs 17px.
  const text = neutral
    ? 'This export contains no project names.'
    : revealProjects
      ? 'Export will show real project names.'
      : 'Preview shows real names. Export is anonymized.';
  return (
    <p
      className={`share-privacy-status share-privacy-status--${severity}`}
      role="status"
      aria-live="polite"
    >
      {text}
    </p>
  );
}

export function ShareModal({ panel, source = 'claude', account = null, onClose, initialParams }: Props) {
  const cardRef = useRef<HTMLDivElement>(null);
  // #503 S1 B1 — whether the rendered export carries any project name.
  // Reported by <PreviewPane>, which owns the only /api/share/render call in
  // this flow; `null` until the first render resolves, and again whenever the
  // panel or template changes.
  const [hasProjectNames, setHasProjectNames] = useState<boolean | null>(null);
  // #503 S3 §5 — did the last preview render fail? Reported by <PreviewPane>
  // and consumed by <ActionBar>, which WARNS without disabling: the export
  // re-fetches, so a failed preview does not imply a failed export, and
  // disabling would remove a capability the user still has.
  const [previewFailed, setPreviewFailed] = useState(false);
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
  const shareIsTopmost = useSyncExternalStore(
    subscribeStore,
    () => topmostStoreFocusLayer(getState()) === 'share',
  );
  useModalFocus(cardRef, {
    active: true,
    // #503 S4 §3.1 — yield the Tab trap to the Manage presets dialog while it
    // is open. That dialog is a DESCENDANT of this card, so without the
    // handoff this trap cycles the whole share modal and Tab walks straight
    // out of the nested dialog.
    trapEnabled: shareIsTopmost && !manageOpen,
    initialFocus: 'heading',
  });

  // SHARE-1 (#293 S4) put the Live preview first on phone with a render
  // reorder, because `.share-preview-col` was nested inside
  // `.share-main-section` — a separate flex parent from the sibling gallery —
  // so a CSS `order` could not hoist it.
  //
  // #503 S4 §4.4 removed that nesting: the gallery, knobs and preview are now
  // direct children of the one flex container `.share-modal-body`, so `order`
  // places the SINGLE `<PreviewPane>` at both breakpoints. The two-instance
  // branch is gone, and with it the remount that discarded the fetched render
  // on every viewport flip (#520 item 2).

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
      // ever removed. #503 S4 §3.1 moved Manage's own Esc to overlay layer
      // 208, so `!manageOpen` is now retained belt-and-suspenders rather than
      // the mechanism — the ordering above us is structural.
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

  // #503 S4 F29 — the accessible name of the preview iframe. Computed here
  // because this component already resolves the selected template object;
  // `null` while the templates fetch is in flight, which PreviewPane renders
  // as a plain "Report preview" rather than a placeholder.
  const artifactLabel = useMemo(() => {
    if (!templates || !selectedTemplateId) return null;
    const tmpl = templates.find((t) => t.id === selectedTemplateId);
    if (!tmpl) return null;
    return `${panelLabel} — ${tmpl.label}`;
  }, [templates, selectedTemplateId, panelLabel]);

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

        <div className="share-knobs-col" aria-label="Render options">
          <Knobs options={options} onChange={handleOptionsChange} />
        </div>

        <div className="share-preview-col" aria-label="Live preview">
          {/* #503 S3 §5 — the privacy line is GATED OFF, not changed.
              With `hasProjectNames === null` on a failed preview it still
              renders a definite claim, including "Preview shows real
              names" — which is false when the preview shows an error. The
              export's privacy state is genuinely unknown here and the line
              has no state for unknown; S1's derivation is untouched. */}
          {previewFailed ? null : (
            <SharePrivacyStatus
              revealProjects={!!options.reveal_projects}
              hasProjectNames={hasProjectNames}
            />
          )}
          <PreviewPane
            panel={panel}
            source={source}
            account={account}
            templateId={selectedTemplateId}
            options={options}
            artifactLabel={artifactLabel}
            onProjectNamesResolved={setHasProjectNames}
            onPreviewFailed={setPreviewFailed}
          />
        </div>
      </div>

      <footer className="share-modal-footer">
        <ActionBar
          panel={panel}
          source={source}
          account={account}
          previewFailed={previewFailed}
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
        shareIsTopmost={shareIsTopmost}
        onClose={() => setManageOpen(false)}
      />
    </div>
  );
}
