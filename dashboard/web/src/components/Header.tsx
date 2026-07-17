import { useSyncExternalStore, type ReactNode } from 'react';
import { BasketChip } from './BasketChip';
import { DoctorChip } from './DoctorChip';
import { SyncChip } from './SyncChip';
import { SourceSwitcher } from './SourceSwitcher';
import { SourceStatusChip } from './SourceStatusChip';
import { UpdateBadge } from './UpdateBadge';
import { ViewSwitcher } from '../conversations/ViewSwitcher';
import { useSnapshot } from '../hooks/useSnapshot';
import { fmt } from '../lib/fmt';
import { joinCodexQuotaLabels } from '../lib/sourceRows';
import { resolveSourceView } from '../store/sourceView';
import { triggerSync } from '../store/sync';
import { getState, subscribeStore } from '../store/store';
import type {
  CodexSourceData,
  CurrentWeekEnvelope,
  DashboardSelection,
  Envelope,
  HeaderEnvelope,
} from '../types/envelope';

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

// #294 S5 §5.4 — build the provider-native condensed readout body. Returns null
// when there is nothing to show (All mode, or Codex with no active quota
// window), which also suppresses the condensed row entirely.
function buildCondensedReadout(
  activeSource: DashboardSelection,
  env: Envelope | null,
  header: HeaderEnvelope | null | undefined,
  cw: CurrentWeekEnvelope | null,
): ReactNode {
  if (activeSource === 'all') return null; // hidden under All
  if (activeSource === 'codex') {
    const view = resolveSourceView(env, 'codex');
    const data = view.entry?.data as CodexSourceData | undefined;
    if (data?.hero == null || data?.quota == null) return null;
    const windows = joinCodexQuotaLabels(data.hero, data.quota);
    if (windows.length === 0) return null;
    const top = windows[0];
    return (
      <>
        <span className="topbar-condensed-pct">{fmt.pct1(top.current.current_percent)}</span>
        <span className="topbar-condensed-sep" aria-hidden="true"> · </span>
        <span className="topbar-condensed-reset">{top.label}</span>
      </>
    );
  }
  // Claude (default) — the legacy Used% / resets-in line, unchanged.
  return (
    <>
      <span className="topbar-condensed-pct">{fmt.pct1(header?.used_pct)}</span>
      <span className="topbar-condensed-sep" aria-hidden="true"> · </span>
      <span className="topbar-condensed-reset">resets {fmt.ddhh(cw?.reset_in_sec)}</span>
    </>
  );
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
  const activeSource = useSyncExternalStore(subscribeStore, () => getState().activeSource);
  const env = useSnapshot();
  const header = env?.header;
  const cw = env?.current_week ?? null;
  // #294 S5 §5.4 — the mobile condensed readout is provider-native. Under Codex
  // it shows the native quota summary via the §6.1 label join; under All it is
  // hidden; under Claude it keeps the legacy Used% / resets-in line. It never
  // shows Claude copy while another source is active.
  const condensedBody = buildCondensedReadout(activeSource, env, header, cw);
  const showCondensed = view === 'dashboard' && heroScrolled && condensedBody != null;
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
      {/* #294 S5 — the global Claude / Codex / All source selector. Self-hides
          outside the dashboard workspace. Sits beside the workspace switcher. */}
      <SourceSwitcher />
      {/* condensed readout: Task 7 — a mobile-only, dashboard-only condensed
          line, gated on view==='dashboard' && heroScrolled. Provider-native
          under Codex (native quota summary); hidden under All; Claude keeps the
          legacy Used% / resets-in line. CSS keeps it desktop-hidden. */}
      {showCondensed && (
        <div className="topbar-condensed" data-testid="topbar-condensed" data-source={activeSource}>
          {condensedBody}
        </div>
      )}
      <div className="topbar-actions">
        {/* #294 S5 — the active-source freshness / warning chip (§6.8). Distinct
            from the global SyncChip; self-hides while hydrating / pre-S4. */}
        <SourceStatusChip />
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
