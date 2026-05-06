import { SyncChip } from './SyncChip';
import { useSnapshot } from '../hooks/useSnapshot';
import { fmt } from '../lib/fmt';
import { resolveVerdict } from '../lib/verdict';
import { triggerSync } from '../store/sync';

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
  return (
    <div className="topbar">
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
