import { useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { useScopedSnapshot } from '../hooks/useScopedSnapshot';
import { fmt } from '../lib/fmt';
import {
  alertAccount,
  alertDisplay,
  filterAlertRowsForAccount,
  selectSourceAlertRows,
  toastAlertId,
} from '../lib/alertIdentity';
import { VENDOR_WIDE_ACCOUNT, type AccountCard, type SourceAlertRow } from '../types/envelope';
import { resolveSourceView } from '../store/sourceView';
import { shortAccountLabel } from '../store/accountFocus';
import { useAccountScope } from '../hooks/useScopedSnapshot';
import { cardRegionClick } from '../lib/cardRegion';
import { PANEL_REGISTRY } from '../lib/panelRegistry';
import { PanelGrip } from './PanelGrip';
import { ExpandButton } from './ExpandButton';
import { AlertsEmptyGauge } from './AlertsEmptyGauge';

// Recent alerts panel — compact, last-10, severity color, collapsible.
// Click anywhere on the panel body to open the full-history modal
// (matches the existing panel-as-button idiom). The header chevron
// toggles `prefs.alertsCollapsed`; that click stops propagation so
// the open-modal handler doesn't fire on the same gesture.
//
// #294 S5 §6.7 — the panel is source-aware. It reads the ACTIVE source's alert
// projection through the seam (`selectSourceAlertRows`), never the legacy
// top-level array. On a pre-S4 envelope (no `sources` bundle) it falls back to
// the legacy `state.alerts` (wrapped as Claude rows) so older servers and unit
// tests keep working; Claude-mode rendering is value-identical either way.
export function RecentAlertsPanel(): JSX.Element {
  const env = useScopedSnapshot();
  const activeSource = useSyncExternalStore(subscribeStore, () => getState().activeSource);
  const legacyAlerts = useSyncExternalStore(subscribeStore, () => getState().alerts);
  const collapsed = useSyncExternalStore(
    subscribeStore,
    () => getState().prefs.alertsCollapsed,
  );
  const hasBundle = env?.sources != null;
  const view = resolveSourceView(env ?? null, activeSource);

  const scope = useAccountScope();
  const claudeLegacyRows: SourceAlertRow[] = legacyAlerts.map((a) => ({
    ...a,
    source: 'claude' as const,
    key: a.id,
  }));
  // #294 S5 §6.7 / §5.2 — Codex + All read the source projection. Claude reads
  // the legacy top-level projection when populated (the §5.2 Claude legacy-
  // compatible view — value-identical to the source projection in production,
  // which mirrors both), else the source projection. A pre-S4 envelope (no
  // bundle) always uses the legacy rows.
  const allRows: SourceAlertRow[] =
    !hasBundle
      ? claudeLegacyRows
      : activeSource === 'claude' && claudeLegacyRows.length > 0
        ? claudeLegacyRows
        : selectSourceAlertRows(view);
  const rowsCarryAccounts = allRows.some((row) => alertAccount(row) != null);
  const accountScoped = scope.accountKey != null
    || (scope.requestedKey != null && rowsCarryAccounts);
  const accountUnfiltered = scope.requestedKey != null
    && !scope.scopesSupported
    && !rowsCarryAccounts;
  const focusedRows = filterAlertRowsForAccount(allRows, scope.requestedKey);
  const sourceAccounts = activeSource === 'all'
    ? []
    : ((env?.sources?.[activeSource]?.data as { accounts?: AccountCard[] } | undefined)?.accounts ?? []);
  const focusedCard = scope.card
    ?? sourceAccounts.find((card) => card.accountKey === scope.requestedKey)
    ?? null;

  // #248 §5 / #264 S1 / #265 A — the Claude empty state reads the current Used %
  // (header) + the configured weekly fire thresholds (default [90, 95]) and
  // renders the shared <AlertsEmptyGauge> (compact) so the panel + modal empty
  // states can't drift; never hardcode 90/95. The gauge routes per active source.
  const alertsConfig = useSyncExternalStore(subscribeStore, () => getState().alertsConfig);
  const codexQuota = env?.sources?.codex?.data?.quota.summary;
  const claudeThresholds = alertsConfig.weekly_thresholds?.length
    ? alertsConfig.weekly_thresholds
    : [90, 95];
  const codexThresholds = env?.sources?.codex?.data?.alerts.actual_thresholds?.length
    ? env.sources.codex.data.alerts.actual_thresholds
    : [90, 95];
  // #416 QA sweep — `quota.summary.latest_percent` is `max(...)` across every
  // account's active window, and this gauge prints it as a big unlabelled "%".
  // A max is still ONE account's number presented as the provider's, which is
  // the same class as the forecast defect reached through an aggregate rather
  // than an index — and the thresholds it is measured against fire per account.
  // Under "All accounts" the gauge abstains (the canonical anatomy already
  // renders an em dash with the fill at zero); focus a chip and the account's
  // own percentage returns.
  const usedPct = activeSource === 'claude'
    ? env?.header?.used_pct ?? null
    : activeSource === 'codex'
      ? (scope.scopesSupported && scope.accountKey == null
        ? null
        : codexQuota?.latest_percent ?? null)
      : null;
  const gaugeThresholds = activeSource === 'claude'
    ? claudeThresholds
    : activeSource === 'codex'
      ? codexThresholds
      : [...new Set([...claudeThresholds, ...codexThresholds])].sort((a, b) => a - b);
  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  // Slice newest-first to last 10 for the panel; the modal renders the
  // full list (up to 100). Panel slice is a UI policy, not a data
  // truncation — the footer's `total` continues to reflect the full count.
  const alerts = focusedRows.slice(0, 10);
  const total = focusedRows.length;

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
      role="region"
      aria-label="Recent alerts panel"
      data-panel-kind="alerts"
      onClick={cardRegionClick(openModal)}
    >
      <div
        className="panel-header"
        style={{ justifyContent: 'space-between' }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <svg className="icon" aria-hidden="true">
            <use href="/static/icons.svg#bell" />
          </svg>
          <h2>
            Recent alerts <span className="sub">(last 10)</span>
          </h2>
          {accountScoped && (
            <span
              className="alerts-account-note"
              data-testid="alerts-account-note"
              title={`Showing only ${focusedCard?.label ?? 'this account'}'s alerts, plus vendor-wide crossings.`}
            >
              {shortAccountLabel(focusedCard?.label ?? 'focused account')} only
            </span>
          )}
          {accountUnfiltered && (
            <span
              className="alerts-unfiltered-note"
              data-testid="alerts-unfiltered-note"
              title="Claude alerts are not filtered by account (showing all accounts)."
            >
              all accounts (unfiltered)
            </span>
          )}
        </div>
        <div className="panel-header-actions">
          <ExpandButton label="Recent alerts" onOpen={openModal} />
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
            <svg className="icon" aria-hidden="true">
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
          <AlertsEmptyGauge
            source={activeSource}
            usedPct={usedPct}
            thresholds={gaugeThresholds}
            compact
          />
        ) : (
          <ul className="alerts-list">
            {alerts.map((row) => {
              const d = alertDisplay(row);
              const account = alertAccount(row);
              return (
                <li key={toastAlertId(row)} className="alert-row">
                  <span
                    className={`alert-threshold severity-${d.severity} ${d.severity}`}
                  >
                    {d.threshold}%
                  </span>
                  <span className={`chip ${d.chipClass}`}>
                    {d.chipLabel}
                  </span>
                  {activeSource === 'all' && (
                    <span className={`source-chip source-chip--${d.source}`}>
                      {d.sourceLabel}
                    </span>
                  )}
                  {account != null && (
                    <span
                      className="alert-account-chip"
                      data-testid={account.key === VENDOR_WIDE_ACCOUNT ? 'alert-vendor-wide' : 'alert-account-chip'}
                      title={account.key === VENDOR_WIDE_ACCOUNT
                        ? 'A vendor-wide crossing — not attributable to one account.'
                        : account.label}
                    >
                      {account.label}
                    </span>
                  )}
                  <span className="alert-when">
                    {fmt.relativeOrAbsolute(d.whenIso ?? '', ctx)}
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
