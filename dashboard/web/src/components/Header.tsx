import { useSyncExternalStore } from 'react';
import { BasketChip } from './BasketChip';
import { DoctorChip } from './DoctorChip';
import { SyncChip } from './SyncChip';
import { UpdateBadge } from './UpdateBadge';
import { ViewSwitcher } from '../conversations/ViewSwitcher';
import { useSnapshot } from '../hooks/useSnapshot';
import { fmt } from '../lib/fmt';
import { triggerSync } from '../store/sync';
import { getState, subscribeStore } from '../store/store';

// Chrome bar (#248 §1) — slimmed to GLOBAL chrome only. The five dashboard
// `.stat` blocks (Week / Used / $/1% / Forecast) and the "vs last week" delta
// moved to the new dashboard-only `HeroStrip` (the at-a-glance hero). What
// stays here is identical across the dashboard AND conversations views:
// topbar-brand (brand + version + UpdateBadge), the ViewSwitcher, and the
// Doctor/Basket/settings/help/sync action cluster.
//
// The action buttons synthesize the same KeyboardEvents the existing footer
// buttons fire, reusing the registered keymap actions; this avoids a parallel
// state path.
function dispatchKey(key: string): void {
  document.dispatchEvent(new KeyboardEvent('keydown', { key }));
}

export function Header() {
  // Update slice — drives the badge visibility and the version label beside the
  // brand. The badge itself self-gates on `update.state.available`; we still
  // pull current_version here so a user with a current cctally (no update
  // available) still sees the version next to "cctally". Falls back to "—" when
  // state is null (pre-bootstrap) or current_version is missing.
  const update = useSyncExternalStore(subscribeStore, () => getState().update);
  const currentVersion = update.state?.current_version ?? null;
  // #248 §6 — the mobile-only, dashboard-only condensed hero readout. Gated on
  // view + heroScrolled (the HeroStrip IO flips the flag once the hero scrolls
  // past); CSS keeps it desktop-hidden. It reads the same Used% + reset the hero
  // shows — this is the ONLY dashboard datum the slimmed Header reads, NOT the
  // H2 full-stat duplication.
  const view = useSyncExternalStore(subscribeStore, () => getState().view);
  const heroScrolled = useSyncExternalStore(subscribeStore, () => getState().heroScrolled);
  const env = useSnapshot();
  const header = env?.header;
  const cw = env?.current_week ?? null;
  const showCondensed = view === 'dashboard' && heroScrolled;
  return (
    <header className={`topbar${showCondensed ? ' is-scrolled' : ''}`} data-view={view}>
      {/* Heading outline root (A3) — visually hidden so the topbar design
          is untouched, but it anchors the page's h1 → h2 (panel) outline. */}
      <h1 className="sr-only">cctally dashboard</h1>
      <div className="stat topbar-brand">
        <span className="brand-name">cctally</span>
        {currentVersion ? (
          <span className="brand-version">v{currentVersion}</span>
        ) : null}
        <UpdateBadge />
        {/* Preview-channel marker (maintainer-local). Renders only when the
            envelope carries channel === 'preview' — set exclusively by the
            `cctally-preview` wrapper (CCTALLY_CHANNEL=preview) — so a normal
            prod dashboard is byte-identical (no pill). Makes a preview
            dashboard, running against a data snapshot, unmistakable next to
            the live prod one. */}
        {env?.channel === 'preview' ? (
          <span
            className="preview-badge"
            title="Preview channel — running against a real data snapshot, not your live prod dashboard"
          >
            PREVIEW
          </span>
        ) : null}
      </div>
      {/* Conversation viewer (spec §4) — segmented Dashboard｜Conversations
          workspace switcher. Self-hides until an envelope confirms
          transcripts are enabled for this request, so the dashboard
          chrome is identical for users without the feature. */}
      <ViewSwitcher />
      {/* condensed readout: Task 7 — a mobile-only, dashboard-only condensed
          Used% / resets-in line, gated on view==='dashboard' && heroScrolled.
          CSS keeps it desktop-hidden; on mobile it keeps the sticky bar one row
          ≤64px and the hero number glanceable while scrolling. */}
      {showCondensed && (
        <div className="topbar-condensed" data-testid="topbar-condensed">
          <span className="topbar-condensed-pct">{fmt.pct1(header?.used_pct)}</span>
          <span className="topbar-condensed-sep" aria-hidden="true"> · </span>
          <span className="topbar-condensed-reset">resets {fmt.ddhh(cw?.reset_in_sec)}</span>
        </div>
      )}
      <div className="topbar-actions">
        {/* Doctor aggregate chip (spec §6.1). Renders nothing until
            the first SSE tick carrying snap.doctor lands. Click +
            `d` keymap both open the DoctorModal. Placed before the
            basket chip so it sits adjacent to the brand/version
            block when both are present. */}
        <DoctorChip />
        {/* Share-report basket chip (spec §7.5). DOM-removed when
            count = 0 — placed first in topbar-actions so it groups
            with the action icon trio when present. */}
        <BasketChip />
        <button
          type="button"
          className="topbar-icon-btn topbar-settings"
          aria-label="Open settings"
          onClick={() => dispatchKey('s')}
        >
          <svg className="icon" aria-hidden="true">
            <use href="/static/icons.svg#settings" />
          </svg>
        </button>
        <button
          type="button"
          className="topbar-icon-btn topbar-help"
          aria-label="Open help"
          onClick={() => dispatchKey('?')}
        >
          <svg className="icon" aria-hidden="true">
            <use href="/static/icons.svg#help-circle" />
          </svg>
        </button>
        <button
          type="button"
          className="topbar-sync"
          onClick={() => triggerSync()}
          title="Sync now (r)"
          aria-label="Sync now"
        >
          <svg className="icon" aria-hidden="true">
            <use href="/static/icons.svg#refresh" />
          </svg>
          <SyncChip />
        </button>
      </div>
    </header>
  );
}
