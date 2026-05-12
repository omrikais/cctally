// Composer modal — multi-section editor (spec §8).
//
// Two-pane layout:
//   - left: section list (ComposerSectionList) with dnd-kit reorder +
//     per-section kebab (preview-only / refresh / remove).
//   - right: composite knob bar above + sandboxed live-preview iframe.
//
// Recompose pipeline (single 200ms-debounced useEffect): re-POSTs
// /api/share/compose whenever (basket.items, title, theme, format,
// anonOnExport, noBranding) changes. AbortController cancels the
// in-flight request when the deps change again — guards against
// out-of-order resolves. Mirrors PreviewPane's pattern (M1.14).
//
// Per-section "Refresh from current data" re-POSTs /api/share/render
// for that section's recipe (with the composite reveal_projects so the
// refreshed digest matches what compose will see), then dispatches a
// BASKET_REMOVE + BASKET_ADD pair at the same id; the items-array
// identity change retriggers the recompose effect.
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import {
  buildComposeRequest, composeShare, type ComposeResponse,
} from './composerApi';
import { ComposerSectionList } from './ComposerSectionList';
import { ShareApiError, renderShare } from './api';
import { dispatch, getState, subscribeStore } from '../store/store';
import { closeComposer } from '../store/shareSlice';
import { makeBasketItem } from '../store/basketSlice';
import { bannerVisible, effectiveReveal } from './anonFormula';
import { useIsMobile } from '../hooks/useIsMobile';
import { useKeymap } from '../hooks/useKeymap';
import type { ShareFormat, ShareTheme } from './types';

const COMPOSE_DEBOUNCE_MS = 200;

// useSyncExternalStore: separate single-slice subscriptions for the
// composer slot and the basket. Each selector returns the slice's own
// identity (stable across unrelated dispatches), avoiding the
// getSnapshot-returns-a-new-object infinite-loop trap. Mirrors the
// BasketChip pattern.
function selectComposerModal() { return getState().composerModal; }
function selectBasket() { return getState().basket; }

// Stable id for aria-labelledby — spec §12.4 requires the modal's
// dialog role be named by a referenced element, not by inline
// aria-label, so screen-reader output matches the visible header
// (and so headers added later automatically participate).
const COMPOSER_MODAL_TITLE_ID = 'composer-modal-title';

export function ComposerModal() {
  const composerModal = useSyncExternalStore(subscribeStore, selectComposerModal);
  const basket = useSyncExternalStore(subscribeStore, selectBasket);
  // Mobile layout (spec §8.10). Below 640px: stacked composite knobs /
  // horizontal pill strip / preview / sticky export bar. The class is
  // applied to both the empty-state and the populated path so the
  // stylesheet rules can target either.
  const isMobile = useIsMobile();
  // Focus restoration (spec §12.8 + M4.4): capture the element that
  // had focus when the composer opened (BasketChip click, B keymap, or
  // a future "Customize…" button) and restore focus to it on close.
  // The capture happens the first time `composerModal.open` flips to
  // true; the restore happens when it flips back to null (the slot is
  // wiped by `closeComposer()`). If the captured element has been
  // detached from the DOM by the time we close (panel re-render while
  // the composer was open), fall back to `document.body.focus()` —
  // without the fallback, focus would silently stay on whatever
  // internal control happened to be focused inside the composer, which
  // then itself unmounts → activeElement becomes implicit and screen
  // readers lose the cursor.
  const triggerElementRef = useRef<HTMLElement | null>(null);
  const wasOpenRef = useRef(false);
  const [title, setTitle] = useState('');
  const [theme, setTheme] = useState<ShareTheme>('light');
  const [format, setFormat] = useState<ShareFormat>('html');
  // Composite "Anon on export" — default true (spec §6.3 anon-on-export
  // default). The composite reveal_projects we send to the server is
  // the inverse of this checkbox.
  const [anonOnExport, setAnonOnExport] = useState(true);
  const [noBranding, setNoBranding] = useState(false);
  const [composeResp, setComposeResp] = useState<ComposeResponse | null>(null);
  const [composeErr, setComposeErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const acRef = useRef<AbortController | null>(null);

  // Esc-to-close at overlay scope (spec §12.1). The composer can layer
  // ABOVE the share modal (the "Customize…" path opens the composer
  // while the share modal is still up); both register their Esc at
  // overlay scope, but ShareModal's binding is gated with
  // `when: () => !manageOpen && composerModal === null` so this one
  // wins when both are mounted. The `when:` guard here ensures the
  // binding is inert when the composer slot is null — without it, even
  // a closed-then-reopened ComposerModal would keep stealing Esc from
  // sibling overlays. Binding identity is stable (empty deps); the
  // dispatch + getState reads happen at fire time.
  const bindings = useMemo(
    () => [{
      key: 'Escape',
      scope: 'overlay' as const,
      when: () => getState().composerModal !== null,
      action: () => dispatch(closeComposer()),
    }],
    [],
  );
  useKeymap(bindings);

  // Default title once: "cctally report — <utcdate>" (spec §8.5; UTC
  // matches the CLI filename convention so a shared link's title and
  // the downloaded filename agree across timezones).
  useEffect(() => {
    if (composerModal?.open && title === '') {
      const utc = new Date().toISOString().slice(0, 10);
      setTitle(`cctally report — ${utc}`);
    }
  }, [composerModal?.open, title]);

  // Focus capture + restore (spec §12.8). Mirrors <ShareModalRoot>'s
  // pattern; kept local rather than threading a triggerId through the
  // openComposer() action because (a) `B` keymap fires from no
  // particular element, (b) BasketChip is the most common opener and
  // is always the active element when clicked, (c) future "Customize…"
  // affordances inside the share modal can rely on the same capture
  // path with no new slice plumbing.
  useEffect(() => {
    if (composerModal?.open) {
      if (!wasOpenRef.current) {
        wasOpenRef.current = true;
        triggerElementRef.current =
          document.activeElement as HTMLElement | null;
      }
    } else if (wasOpenRef.current) {
      wasOpenRef.current = false;
      const el = triggerElementRef.current;
      triggerElementRef.current = null;
      if (el && typeof el.focus === 'function' && document.contains(el)) {
        el.focus();
      } else {
        // Detached opener: blur whatever is currently focused so the
        // screen reader doesn't keep announcing the composer's
        // about-to-unmount internal control. Project precedent at
        // <ShareModalRoot>.
        const active = document.activeElement as HTMLElement | null;
        if (active && typeof active.blur === 'function') active.blur();
        document.body.focus();
      }
    }
  }, [composerModal?.open]);

  // Debounced recompose. Triggers: mount, reorder, knob change,
  // per-section refresh (which mutates basket.items via remove+add).
  useEffect(() => {
    if (!composerModal?.open) return;
    if (basket.items.length === 0) return;
    if (title === '') return; // wait for the default-title seed before posting
    const handle = setTimeout(() => {
      acRef.current?.abort();
      const ac = new AbortController();
      acRef.current = ac;
      setBusy(true);
      setComposeErr(null);
      const req = buildComposeRequest(basket.items, {
        title,
        theme,
        format,
        no_branding: noBranding,
        reveal_projects: !anonOnExport,
      });
      composeShare(req, { signal: ac.signal })
        .then((resp) => {
          if (ac.signal.aborted) return;
          setComposeResp(resp);
        })
        .catch((err: unknown) => {
          if ((err as Error)?.name === 'AbortError') return;
          if (ac.signal.aborted) return;
          const msg = err instanceof ShareApiError
            ? (err.message ?? `HTTP ${err.status}`)
            : (err as Error).message;
          setComposeErr(msg);
        })
        .finally(() => {
          if (!ac.signal.aborted) setBusy(false);
        });
    }, COMPOSE_DEBOUNCE_MS);
    return () => clearTimeout(handle);
  }, [composerModal?.open, basket.items, title, theme, format,
      anonOnExport, noBranding]);

  async function handleRefreshSection(id: string) {
    const idx = basket.items.findIndex((it) => it.id === id);
    if (idx < 0) return;
    const it = basket.items[idx];
    try {
      const resp = await renderShare({
        panel: it.panel,
        template_id: it.template_id,
        options: { ...it.options, reveal_projects: !anonOnExport },
      });
      const refreshed = makeBasketItem({
        panel: it.panel,
        template_id: it.template_id,
        options: it.options,
        added_at: new Date().toISOString(),
        data_digest_at_add: resp.snapshot.data_digest,
        kernel_version: resp.snapshot.kernel_version,
        label_hint: it.label_hint,
        id: it.id,
      });
      // Remove + re-add at the same index. The pair mutates
      // basket.items identity, retriggering the recompose effect, and
      // the kebab's owning Row remounts cleanly.
      dispatch({ type: 'BASKET_REMOVE', id });
      dispatch({ type: 'BASKET_ADD', item: refreshed });
      const lastIdx = getState().basket.items.length - 1;
      if (lastIdx !== idx) {
        dispatch({ type: 'BASKET_REORDER', fromIdx: lastIdx, toIdx: idx });
      }
    } catch (err) {
      const msg = err instanceof ShareApiError
        ? (err.message ?? `HTTP ${err.status}`)
        : (err as Error).message;
      setComposeErr(`Refresh failed: ${msg}`);
    }
  }

  // Real-name banner (spec §10.5 + §8.7). Visible iff any section
  // would expose its project names in the export under the current
  // composite reveal_projects (= !anonOnExport) and the section's own
  // recorded reveal_at_add. Click "Anonymize all" → flip composite anon
  // ON; the recompose effect picks it up via the anonOnExport dep.
  const compositeReveal = !anonOnExport;
  const sectionReveals = useMemo(
    () => basket.items.map((it) => it.options.reveal_projects),
    [basket.items],
  );
  const showBanner = bannerVisible(sectionReveals, compositeReveal);
  const revealedCount = useMemo(
    () => basket.items.filter((it) => effectiveReveal(
      it.options.reveal_projects, compositeReveal,
    )).length,
    [basket.items, compositeReveal],
  );

  if (!composerModal?.open) return null;

  if (basket.items.length === 0) {
    return (
      <div
        className={`composer-modal composer-modal-empty${isMobile ? ' composer-modal-mobile' : ''}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={COMPOSER_MODAL_TITLE_ID}
      >
        <header className="composer-modal-header">
          <h2 id={COMPOSER_MODAL_TITLE_ID}>Compose report</h2>
          <button
            type="button"
            className="composer-modal-close"
            onClick={() => dispatch(closeComposer())}
            aria-label="Close"
          >
            ⤬
          </button>
        </header>
        <p className="composer-empty-message">
          Basket is empty. Open any panel&apos;s share menu and pick + Basket to add a section.
        </p>
      </div>
    );
  }

  return (
    <div
      className={`composer-modal${isMobile ? ' composer-modal-mobile' : ''}`}
      role="dialog"
      aria-modal="true"
      aria-labelledby={COMPOSER_MODAL_TITLE_ID}
    >
      <header className="composer-modal-header">
        <h2 id={COMPOSER_MODAL_TITLE_ID}>Compose report</h2>
        <button
          type="button"
          className="composer-modal-close"
          onClick={() => dispatch(closeComposer())}
          aria-label="Close"
        >
          ⤬
        </button>
      </header>
      <div className="composer-knobs">
        <label>
          Title
          <input
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
        </label>
        <label>
          Theme
          <select value={theme} onChange={(e) => setTheme(e.target.value as ShareTheme)}>
            <option value="light">light</option>
            <option value="dark">dark</option>
          </select>
        </label>
        <label>
          Format
          <select value={format} onChange={(e) => setFormat(e.target.value as ShareFormat)}>
            <option value="html">html</option>
            <option value="md">md</option>
            <option value="svg">svg</option>
          </select>
        </label>
        <label>
          <input
            type="checkbox"
            checked={anonOnExport}
            onChange={(e) => setAnonOnExport(e.target.checked)}
          />
          Anon on export
        </label>
        <label>
          <input
            type="checkbox"
            checked={noBranding}
            onChange={(e) => setNoBranding(e.target.checked)}
          />
          No branding
        </label>
      </div>
      {showBanner ? (
        <div className="composer-anon-banner" role="status" aria-live="polite">
          <span>
            {revealedCount} section{revealedCount === 1 ? '' : 's'} contain real project names. They will appear in the export.
          </span>
          <button type="button" onClick={() => setAnonOnExport(true)}>
            Anonymize all
          </button>
        </div>
      ) : null}
      <div className="composer-body">
        <ComposerSectionList
          items={basket.items}
          results={composeResp?.snapshot.section_results ?? []}
          kernelVersion={composeResp?.snapshot.kernel_version ?? 1}
          onRefresh={(id) => { void handleRefreshSection(id); }}
          onRemove={(id) => dispatch({ type: 'BASKET_REMOVE', id })}
          onPreviewOnly={(_id) => { /* M4 niceties; not part of M3.6 */ }}
        />
        <iframe
          className="composer-preview"
          title="Combined preview"
          tabIndex={-1}
          sandbox="allow-same-origin"
          srcDoc={composeResp?.body ?? '<p>Composing&hellip;</p>'}
        />
      </div>
      {busy ? <div className="composer-busy">Composing&hellip;</div> : null}
      {composeErr ? <div className="composer-error" role="alert">{composeErr}</div> : null}
      <footer className="composer-actions">
        <button
          type="button"
          onClick={() => dispatch({ type: 'BASKET_CLEAR' })}
        >
          Clear all
        </button>
      </footer>
    </div>
  );
}
