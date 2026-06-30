import { useSyncExternalStore } from 'react';
import { BasketChip } from './BasketChip';
import { DoctorChip } from './DoctorChip';
import { SyncChip } from './SyncChip';
import { UpdateBadge } from './UpdateBadge';
import { ViewSwitcher } from '../conversations/ViewSwitcher';
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
  return (
    <header className="topbar">
      {/* Heading outline root (A3) — visually hidden so the topbar design
          is untouched, but it anchors the page's h1 → h2 (panel) outline. */}
      <h1 className="sr-only">cctally dashboard</h1>
      <div className="stat topbar-brand" data-mobile-keep="primary" data-stat="brand">
        <span className="brand-name">cctally</span>
        {currentVersion ? (
          <span className="brand-version">v{currentVersion}</span>
        ) : null}
        <UpdateBadge />
      </div>
      {/* Conversation viewer (spec §4) — segmented Dashboard｜Conversations
          workspace switcher. Self-hides until an envelope confirms
          transcripts are enabled for this request, so the dashboard
          chrome is identical for users without the feature. */}
      <ViewSwitcher />
      {/* condensed readout: Task 7 — a mobile-only, dashboard-only condensed
          Used% / resets-in line gated on heroScrolled lands here. */}
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
