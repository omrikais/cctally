import { useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { fmt } from '../lib/fmt';
import { PANEL_REGISTRY } from '../lib/panelRegistry';
import { PanelGrip } from './PanelGrip';

// Recent alerts panel — compact, last-10, severity color, collapsible.
// Click anywhere on the panel body to open the full-history modal
// (matches the existing panel-as-button idiom). The header chevron
// toggles `prefs.alertsCollapsed`; that click stops propagation so
// the open-modal handler doesn't fire on the same gesture.
//
// Per spec §7 / v2 mockup: there is intentionally NO "Open modal"
// CTA inside the panel body — clicking the panel itself is the
// established open path across sessions/blocks/daily/weekly etc.
export function RecentAlertsPanel(): JSX.Element {
  const allAlerts = useSyncExternalStore(
    subscribeStore,
    () => getState().alerts,
  );
  const collapsed = useSyncExternalStore(
    subscribeStore,
    () => getState().prefs.alertsCollapsed,
  );
  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  // Slice newest-first to last 10 for the panel; the modal renders the
  // full list (up to 100). Panel slice is a UI policy, not a data
  // truncation — `getState().alerts.length` continues to reflect the
  // store's full count for the "N of M shown" footer.
  const alerts = allAlerts.slice(0, 10);
  const total = allAlerts.length;

  // Open-modal handler routes through panelRegistry.alerts.openAction
  // so the keyboard ('9' in T13) and click paths share one source of
  // truth. Filed under registry rather than dispatched inline so any
  // future variation (e.g., context-aware "open at most-recent
  // alert") lives in one place.
  const openModal = (): void => {
    PANEL_REGISTRY.alerts.openAction();
  };

  return (
    <section
      className={'panel accent-amber' + (collapsed ? ' alerts-collapsed' : '')}
      id="panel-alerts"
      tabIndex={0}
      role="region"
      aria-label="Recent alerts panel"
      data-panel-kind="alerts"
      onClick={openModal}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget) return;
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          openModal();
        }
      }}
    >
      <div
        className="panel-header"
        style={{ justifyContent: 'space-between' }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <svg className="icon" style={{ color: 'var(--accent-amber)' }}>
            <use href="/static/icons.svg#bell" />
          </svg>
          <h3 style={{ color: 'var(--accent-amber)' }}>
            Recent alerts <span className="sub">(last 10)</span>
          </h3>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
          <button
            type="button"
            className="panel-collapse-toggle"
            aria-expanded={!collapsed}
            aria-controls="panel-alerts-body"
            aria-label={collapsed ? 'Expand Recent alerts' : 'Collapse Recent alerts'}
            title={collapsed ? 'Expand' : 'Collapse'}
            onClick={(e) => {
              e.stopPropagation();
              dispatch({
                type: 'SAVE_PREFS',
                patch: { alertsCollapsed: !collapsed },
              });
            }}
          >
            <svg className="icon">
              <use
                href={`/static/icons.svg#${collapsed ? 'chevron-down' : 'chevron-up'}`}
              />
            </svg>
          </button>
          <PanelGrip />
        </div>
      </div>
      <div className="panel-body" id="panel-alerts-body">
        {alerts.length === 0 ? (
          <div className="alerts-empty panel-empty">
            No alerts yet. Alerts appear when usage crosses 90% or 95%.
          </div>
        ) : (
          <ul className="alerts-list">
            {alerts.map((a) => {
              const severity = a.threshold >= 95 ? 'red' : 'amber';
              return (
                <li key={a.id} className="alert-row">
                  <span
                    className={`alert-threshold severity-${severity} ${severity}`}
                  >
                    {a.threshold}%
                  </span>
                  <span className={`chip chip--${a.axis}`}>
                    {a.axis === 'weekly' ? 'WEEKLY' : '5H-BLOCK'}
                  </span>
                  <span className="alert-when">
                    {fmt.relativeOrAbsolute(a.alerted_at, ctx)}
                  </span>
                </li>
              );
            })}
          </ul>
        )}
      </div>
      {total > 0 && (
        <div className="panel-foot alerts-foot">
          {alerts.length} of {total} shown
        </div>
      )}
    </section>
  );
}
