import { useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { useScopedSnapshot } from '../hooks/useScopedSnapshot';
import { fmt } from '../lib/fmt';
import {
  alertAccount,
  alertDisplay,
  filterAlertRowsForFocus,
  selectAlertRowsForView,
  type AlertAccountFocus,
  toastAlertId,
} from '../lib/alertIdentity';
import {
  VENDOR_WIDE_ACCOUNT,
  type AccountCard,
  type SourceAlertRow,
  type SourceName,
} from '../types/envelope';
import { resolveSourceView } from '../store/sourceView';
import { resolveViewAccountFocus, shortAccountLabel } from '../store/accountFocus';
import { scopeProviderFor, useAccountScope } from '../hooks/useScopedSnapshot';
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
// projection through the shared seam (`selectAlertRowsForView`), never the
// legacy top-level array. On a pre-S4 envelope (no `sources` bundle) it falls
// back to the legacy `state.alerts` (wrapped as Claude rows) so older servers
// and unit tests keep working. #556 S3 §3.2: the Claude projection is that
// legacy array filtered by ownership — a SUBSET, not an equal — and it
// preserves every field of the rows it keeps.
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

  // #556 S5 Unit 2 review F3 — NAME THE PROVIDER. A bare `useAccountScope()`
  // resolves to "no provider" under All, so `scopesSupported` was false and
  // `accountKey` null there no matter what was focused. With a Codex account
  // focused under All that made `accountScoped` false and `accountUnfiltered`
  // TRUE, and the panel printed "all accounts (unfiltered)" while the Codex
  // subtree it was reading had already been narrowed to one account. Naming the
  // provider is the same decision every other All-aware surface makes, and on a
  // provider tab it resolves to that tab, so those paths are unchanged.
  const scope = useAccountScope(activeSource, scopeProviderFor(activeSource));
  // True when a Codex focus really narrowed the subtree this panel is reading.
  const scopeNarrows = scope.accountKey != null;
  const claudeLegacyRows: SourceAlertRow[] = legacyAlerts.map((a) => ({
    ...a,
    source: 'claude' as const,
    key: a.id,
  }));
  // #556 S3 §3.1/§3.3 — one shared selector for the panel and the modal.
  // Whenever a bundle exists the active source's OWN projection is read; the
  // legacy top-level array is the pre-bundle fallback only.
  const allRows: SourceAlertRow[] = selectAlertRowsForView(
    view, claudeLegacyRows, hasBundle,
  );
  // #556 S5 §5.11 — under All each provider carries its OWN focus slot, so the
  // filter is per provider. On a provider tab exactly one leg can be non-null,
  // which reproduces the pre-S5 single-key behaviour exactly.
  const focusState = useSyncExternalStore(subscribeStore, () => getState().accountFocus);
  const alertFocus: AlertAccountFocus = {
    claude: resolveViewAccountFocus(env, activeSource, 'claude', focusState),
    codex: resolveViewAccountFocus(env, activeSource, 'codex', focusState),
  };
  const rowsCarryAccounts = allRows.some((row) => alertAccount(row) != null);
  const focusedProviders = (['claude', 'codex'] as SourceName[])
    .filter((provider) => alertFocus[provider] != null);
  // The two badges answer one question between them: did focusing narrow this
  // list? Evidence is either that the rows carry accounts (so the per-provider
  // filter had something to act on) or that a Codex focus replaced the subtree.
  // `scopesSupported` is deliberately no longer consulted: on the Claude tab it
  // is false and `scopeNarrows` is false too, so that path is byte-identical,
  // while under All it described the absent provider rather than the focus.
  const accountScoped = focusedProviders.length > 0
    && (rowsCarryAccounts || scopeNarrows);
  const accountUnfiltered = focusedProviders.length > 0
    && !rowsCarryAccounts
    && !scopeNarrows;
  const focusedRows = filterAlertRowsForFocus(allRows, alertFocus);
  const focusedCards = focusedProviders
    .map((provider) => (
      (env?.sources?.[provider]?.data as { accounts?: AccountCard[] } | undefined)?.accounts ?? []
    ).find((card) => card.accountKey === alertFocus[provider]) ?? null)
    .filter((card): card is AccountCard => card != null);
  const focusedCard = scope.card ?? focusedCards[0] ?? null;
  // Two providers can be focused at once under All, and one account's label
  // would then name the filter wrongly.
  //
  // #556 S5 Unit 2 review F3 — and ONE provider focused under All over-claimed
  // the other way: the badge read "<label> only" while the panel still listed
  // the other provider's alerts in full, because each provider's rows are
  // filtered by that provider's own focus. Qualifying by provider rather than
  // suppressing the badge: the list really is narrowed for one provider, so
  // saying nothing would leave a user who watched rows disappear with no
  // explanation. On a provider tab exactly one leg can be non-null and only
  // that provider's rows are present, so the unqualified label stays correct
  // there and that markup is unchanged.
  const soleFocusedProvider = focusedProviders.length === 1 ? focusedProviders[0] : null;
  const qualifyByProvider = activeSource === 'all' && soleFocusedProvider != null;
  const focusedProviderLabel = soleFocusedProvider === 'codex' ? 'Codex' : 'Claude';
  const otherProviderLabel = soleFocusedProvider === 'codex' ? 'Claude' : 'Codex';
  const focusNoteLabel = focusedProviders.length > 1
    ? 'focused accounts'
    : `${qualifyByProvider ? `${focusedProviderLabel}: ` : ''}${
      shortAccountLabel(focusedCard?.label ?? 'focused account')}`;

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
              title={focusedProviders.length > 1
                ? 'Showing only the focused accounts\u2019 alerts, plus vendor-wide crossings.'
                : qualifyByProvider
                  ? `Showing only ${focusedCard?.label ?? 'this account'}'s ${focusedProviderLabel} alerts, plus vendor-wide crossings. ${otherProviderLabel} alerts are not filtered by account.`
                  : `Showing only ${focusedCard?.label ?? 'this account'}'s alerts, plus vendor-wide crossings.`}
            >
              {focusNoteLabel} only
            </span>
          )}
          {accountUnfiltered && (
            <span
              className="alerts-unfiltered-note"
              data-testid="alerts-unfiltered-note"
              // Names the FOCUSED provider, not a constant. Under All this
              // badge now fires when only Codex is focused, where the previous
              // hardcoded "Claude" named the wrong provider — the same
              // over-claim the sibling badge above was fixed for.
              title={soleFocusedProvider != null
                ? `${focusedProviderLabel} alerts are not filtered by account (showing all accounts).`
                : 'These alerts are not filtered by account (showing all accounts).'}
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
      {/* #556 S3 §4.6 — a SIBLING of `.panel-header`, never inside it: the
          S2 CSS contract gives this element full card width and lets it wrap,
          which is how it stays readable at 390px. Placed inside the header it
          would compete with the header actions for a nowrap row, which is
          exactly how S2's own 390px defect arose. Gated on `activeSource ===
          'all'` EXPLICITLY rather than on a negated Claude check, because S2
          shipped a bug where the else-branch also caught Codex. */}
      {activeSource === 'all' && (
        <div className="panel-range-note">Both providers, newest first</div>
      )}
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
              // #574 — the exact instant, with the calendar year the visible
              // text omits. The OrNull variant is what keeps an unformattable
              // instant from becoming title="—": it returns null for a null
              // `whenIso` and for a non-empty string that does not parse,
              // which testing `d.whenIso` for truthiness would not catch. The
              // modal's when-cell reads the same helper, so the contract has
              // one home rather than a copy per surface.
              const whenTitle = fmt.startedShortOrNull(d.whenIso, ctx);
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
                  <span
                    className="alert-when"
                    title={whenTitle ?? undefined}
                  >
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
