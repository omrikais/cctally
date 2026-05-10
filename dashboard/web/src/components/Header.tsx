import { useSyncExternalStore } from 'react';
import { SyncChip } from './SyncChip';
import { UpdateBadge } from './UpdateBadge';
import { useSnapshot } from '../hooks/useSnapshot';
import { fmt } from '../lib/fmt';
import { resolveVerdict } from '../lib/verdict';
import { triggerSync } from '../store/sync';
import { getState, subscribeStore } from '../store/store';

// Ported from dashboard/static/dashboard.html lines 11-39 (.topbar).
// On mobile the secondary stats (5h percent, "vs last week") are
// hidden via CSS and the action icon trio (⚙/?/⟳) is exposed inline.
// The action buttons synthesize the same KeyboardEvents the existing
// footer buttons fire, reusing the registered keymap actions; this
// avoids a parallel state path.
function dispatchKey(key: string): void {
  document.dispatchEvent(new KeyboardEvent('keydown', { key }));
}

export function Header() {
  const env = useSnapshot();
  const h = env?.header;
  const verdict = resolveVerdict(h?.forecast_verdict ?? null);
  const showWarnPill = verdict?.warn ?? false;
  // Update slice — drives the badge visibility and the version label
  // beside the brand. The badge itself self-gates on
  // `update.state.available`; we still pull current_version here so a
  // user with a current cctally (no update available) still sees the
  // version next to "cctally". Falls back to "—" when state is null
  // (pre-bootstrap) or current_version is missing.
  const update = useSyncExternalStore(subscribeStore, () => getState().update);
  const currentVersion = update.state?.current_version ?? null;
  return (
    <div className="topbar">
      <div className="stat topbar-brand" data-mobile-keep="primary" data-stat="brand">
        <span className="brand-name">cctally</span>
        {currentVersion ? (
          <span className="brand-version">v{currentVersion}</span>
        ) : null}
        <UpdateBadge />
      </div>
      <div className="stat" data-mobile-keep="primary" data-stat="week">
        <svg className="icon">
          <use href="/static/icons.svg#calendar" />
        </svg>
        <span className="k">Week</span>
        <span className="v">{h?.week_label ?? '—'}</span>
      </div>
      <div className="stat" data-mobile-keep="primary" data-stat="used">
        <span className="k">Used</span>
        <span className="hi-green">{fmt.pct1(h?.used_pct)}</span>
        <span className="mute" data-mobile-keep="secondary" data-stat="five-hour">
          (5h <span>{fmt.pct0(h?.five_hour_pct)}</span>)
        </span>
      </div>
      <div className="stat" data-mobile-keep="primary" data-stat="dollar-per-pct">
        <span className="k">$/1%</span>
        <span className="hi-cyan">{fmt.usd2(h?.dollar_per_pct)}</span>
      </div>
      <div className="stat" data-mobile-keep="primary" data-stat="forecast">
        <span className="k">Fcst</span>
        <span className="hi-amber">{fmt.pct0(h?.forecast_pct)}</span>
        {showWarnPill ? (
          <span className="pill-warn">{verdict?.label ?? 'WARN'}</span>
        ) : null}
      </div>
      <div className="stat" data-mobile-keep="secondary" data-stat="vs-last-week">
        <svg className="icon" style={{ color: 'var(--accent-green)' }}>
          <use href="/static/icons.svg#trending-up" />
        </svg>
        <span className="mute">vs last week</span>
      </div>
      <div className="topbar-actions">
        <button
          type="button"
          className="topbar-icon-btn topbar-settings"
          aria-label="Open settings"
          onClick={() => dispatchKey('s')}
        >
          <svg className="icon">
            <use href="/static/icons.svg#settings" />
          </svg>
        </button>
        <button
          type="button"
          className="topbar-icon-btn topbar-help"
          aria-label="Open help"
          onClick={() => dispatchKey('?')}
        >
          <svg className="icon">
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
          <svg className="icon">
            <use href="/static/icons.svg#refresh" />
          </svg>
          <SyncChip />
        </button>
      </div>
    </div>
  );
}
