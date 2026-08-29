import { useSyncExternalStore } from 'react';
import { Modal } from '../modals/Modal';
import { getState, subscribeStore } from '../store/store';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { useAccountScope, useScopedSnapshot } from '../hooks/useScopedSnapshot';
import { fmt } from '../lib/fmt';
import {
  alertSeverity,
  AXIS_CHIP_LABEL,
  budgetPeriodNoun,
  projectedContextText,
} from '../lib/alertAxis';
import {
  alertAccount,
  alertDisplay,
  filterAlertRowsForFocus,
  selectAlertRowsForView,
  toastAlertId,
  type AlertAccountFocus,
} from '../lib/alertIdentity';
import { AlertFollowCell } from './AlertFollow';
import { resolveSourceView } from '../store/sourceView';
import { resolveViewAccountFocus } from '../store/accountFocus';
import { AlertsEmptyGauge } from './AlertsEmptyGauge';
import { ZoneTag } from './ZoneTag';
import { rateChangeSummary } from '../lib/quotaCopy';
import type {
  AlertEntry,
  CodexAlertRow,
  MeterRateChangeEntry,
  SourceAlertRow,
} from '../types/envelope';

// Recent alerts modal — full history (last 100). ESC and backdrop
// close via the shared `<Modal>` chrome (which also handles the
// `accent-amber` border + close button).
//
// #294 S5 §6.7 — the modal is source-aware, sharing the panel's seam. It reads
// the active source's alert projection (`selectAlertRowsForView`, the one
// selector both components call) when a `sources` bundle is present, else
// falls back to the legacy `state.alerts` (wrapped as Claude rows) for older
// servers / unit tests. Claude rows render richly (full
// context/cost cells, unchanged); Codex rows render their lean cells.
//
// IMPORTANT: the T5 envelope-rebuild path does NOT include `primary_model` for
// 5h alerts (only the live-dispatch payload does). The modal MUST therefore
// render 5h context as "Block HH:MM" with no model fragment.
//
// Severity (chip-class on the % cell) comes from the kernel via
// `alertSeverity(a)` for Claude rows and the shared adapter for Codex rows.

const ALERTS_MODAL_CAP = 100;

function ContextCell({
  alert,
  ctx,
}: {
  alert: AlertEntry;
  ctx: { tz: string; offsetLabel: string };
}): JSX.Element {
  if (alert.axis === 'weekly') {
    const weekStart = alert.context.week_start_date
      ? fmt.weekStart(alert.context.week_start_date, ctx)
      : null;
    const dpp = alert.context.dollars_per_percent;
    return (
      <span className="alert-context alert-context--weekly">
        {weekStart ? `Week of ${weekStart}` : 'Week —'}
        {dpp != null && (
          <>
            {' · '}
            <span className="num">${dpp.toFixed(2)}/1%</span>
          </>
        )}
      </span>
    );
  }
  if (alert.axis === 'budget') {
    const noun = budgetPeriodNoun(alert.context.period);
    const start = alert.context.period_start_at ?? alert.context.week_start_at;
    const startLabel = start ? fmt.weekStart(start.slice(0, 10), ctx) : null;
    const pct = alert.context.consumption_pct;
    return (
      <span className="alert-context alert-context--budget">
        {startLabel ? `${noun} of ${startLabel}` : `${noun} —`}
        {pct != null && (
          <>
            {' · '}
            <span className="num">{Math.round(pct)}% of budget</span>
          </>
        )}
      </span>
    );
  }
  if (alert.axis === 'codex_budget') {
    const noun = budgetPeriodNoun(alert.context.period);
    const start = alert.context.period_start_at;
    const startLabel = start ? fmt.weekStart(start.slice(0, 10), ctx) : null;
    const pct = alert.context.consumption_pct;
    return (
      <span className="alert-context alert-context--codex_budget">
        {startLabel ? `${noun} of ${startLabel}` : `${noun} —`}
        {pct != null && (
          <>
            {' · '}
            <span className="num">{Math.round(pct)}% of budget</span>
          </>
        )}
      </span>
    );
  }
  if (alert.axis === 'project_budget') {
    const project = alert.context.project ?? '(project)';
    const pct = alert.context.consumption_pct;
    return (
      <span className="alert-context alert-context--project_budget">
        {project}
        {pct != null && (
          <>
            {' · '}
            <span className="num">{Math.round(pct)}% of budget</span>
          </>
        )}
      </span>
    );
  }
  if (alert.axis === 'projected') {
    const text = projectedContextText(alert);
    return (
      <span className="alert-context alert-context--projected">
        {text ?? 'Projected —'}
      </span>
    );
  }
  // axis === 'five_hour' — render block start time only.
  const t = alert.context.block_start_at
    ? fmt.timeOnly(alert.context.block_start_at, ctx)
    : '—';
  return (
    <span className="alert-context alert-context--five-hour">
      Block {t}
    </span>
  );
}

function CostCell({ alert }: { alert: AlertEntry }): JSX.Element {
  if (alert.axis === 'projected') {
    return <span className="num">—</span>;
  }
  let v: number | undefined;
  if (alert.axis === 'weekly') {
    v = alert.context.cumulative_cost_usd;
  } else if (
    alert.axis === 'budget' ||
    alert.axis === 'project_budget' ||
    alert.axis === 'codex_budget'
  ) {
    v = alert.context.spent_usd;
  } else {
    v = alert.context.block_cost_usd;
  }
  return <span className="num">{fmt.usd2(v ?? null)}</span>;
}

// Lean Codex source-row cells (the projections don't carry the rich context the
// legacy Claude rows do — render what the row provides).
function CodexContextCell({ row }: { row: CodexAlertRow }): JSX.Element {
  if (row.axis === 'quota') {
    return <span className="alert-context alert-context--quota">Quota threshold</span>;
  }
  const noun = budgetPeriodNoun(row.period);
  return (
    <span className={`alert-context alert-context--${row.axis}`}>
      {noun}
      {' · '}
      <span className="num">{Math.round(row.value)}% of budget</span>
    </span>
  );
}

function CodexCostCell(): JSX.Element {
  // The lean Codex projections don't carry a realized-spend figure.
  return <span className="num">—</span>;
}

// #661 S2 section 6.1 — the non-threshold family gets its OWN section with
// its own columns, and never a row in the table above.
//
// That table is threshold-shaped: its columns are `%`, `Axis`, `Cost` and
// `Context`, and a rate transition has no percentage, no axis in
// `AXIS_REGISTRY`, no cost and no window to follow. Forcing one into those
// columns would print an em-dash in three of them and mislabel the fourth.
// The columns here are the ones a rate transition actually has: which
// provider's rate moved, how far, and when it took effect.
function RateChangeSection({
  rows,
  ctx,
  showAccountColumn,
}: {
  rows: MeterRateChangeEntry[];
  ctx: { tz: string; offsetLabel: string };
  showAccountColumn: boolean;
}): JSX.Element {
  return (
    <section className="alerts-rate-change" data-testid="alerts-rate-change">
      <h3 className="alerts-rate-change-heading">Metering rate changes</h3>
      <p className="alerts-rate-change-note">
        The provider changed how much work one meter point buys. This is the
        provider&rsquo;s behaviour, not a cctally malfunction.
      </p>
      <table className="alerts-table alerts-table--rate-change">
        <thead>
          <tr>
            <th scope="col">Provider</th>
            {showAccountColumn && <th scope="col">Account</th>}
            <th scope="col">Change</th>
            <th scope="col">Units / point</th>
            <th scope="col">Effective</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const summary = rateChangeSummary(
              row.previous_units_per_point, row.new_units_per_point,
            );
            const effectiveTitle = fmt.startedShortOrNull(row.effective_from, ctx);
            const effectiveText = fmt.relativeOrAbsolute(row.effective_from ?? '', ctx);
            return (
              <tr key={row.id} className="alert-modal-row">
                <td className="alert-cell-source">
                  <span className={`source-chip source-chip--${row.provider}`}>
                    {row.provider}
                  </span>
                </td>
                {showAccountColumn && (
                  <td className="alert-cell-account">
                    {row.accountLabel != null && (
                      <span className="alert-account-chip" title={row.accountLabel}>
                        {row.accountLabel}
                      </span>
                    )}
                  </td>
                )}
                <td className={`alert-cell-axis severity-${row.severity} ${row.severity}`}>
                  <span className={`chip chip-rate-change severity-${row.severity}`}>
                    {summary ?? 'rate changed'}
                  </span>
                </td>
                <td className="alert-cell-cost num">
                  {row.previous_units_per_point != null && row.new_units_per_point != null
                    ? `${Math.round(row.previous_units_per_point).toLocaleString()} → ${Math.round(row.new_units_per_point).toLocaleString()}`
                    : '—'}
                </td>
                <td
                  className="alert-cell-when alert-when"
                  title={effectiveTitle ?? undefined}
                >
                  {effectiveText}
                  {effectiveText === '—' ? null : <> <ZoneTag tz={ctx.tz} /></>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

export function RecentAlertsModal(): JSX.Element {
  const activeSource = useSyncExternalStore(subscribeStore, () => getState().openModalSource ?? getState().activeSource);
  const env = useScopedSnapshot(activeSource);
  const scope = useAccountScope(activeSource);
  const legacyAlerts = useSyncExternalStore(subscribeStore, () => getState().alerts);
  const hasBundle = env?.sources != null;
  const view = resolveSourceView(env ?? null, activeSource);
  const claudeLegacyRows: SourceAlertRow[] = legacyAlerts.map((a) => ({
    ...a,
    source: 'claude' as const,
    key: a.id,
  }));
  // #556 S3 §3.3 — the same shared selector the panel calls, so the two can no
  // longer be changed apart.
  const allRows: SourceAlertRow[] = selectAlertRowsForView(
    view, claudeLegacyRows, hasBundle,
  );
  // #556 S5 §5.11 — the same provider-aware filter the panel calls, so the two
  // still cannot be changed apart.
  const focusState = useSyncExternalStore(subscribeStore, () => getState().accountFocus);
  const alertFocus: AlertAccountFocus = {
    claude: resolveViewAccountFocus(env, activeSource, 'claude', focusState),
    codex: resolveViewAccountFocus(env, activeSource, 'codex', focusState),
  };
  const focusedRows = filterAlertRowsForFocus(allRows, alertFocus);

  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  const alertsConfig = useSyncExternalStore(subscribeStore, () => getState().alertsConfig);
  const codexQuota = env?.sources?.codex?.data?.quota.summary;
  const claudeThresholds = alertsConfig.weekly_thresholds?.length
    ? alertsConfig.weekly_thresholds
    : [90, 95];
  const codexThresholds = env?.sources?.codex?.data?.alerts.actual_thresholds?.length
    ? env.sources.codex.data.alerts.actual_thresholds
    : [90, 95];
  // #416 QA sweep — see `RecentAlertsPanel`: `latest_percent` is the MAX across
  // accounts, so under "All accounts" it is one account's number printed as the
  // provider's. The gauge abstains rather than electing one.
  const usedPct = activeSource === 'claude'
    ? env?.header?.used_pct ?? null
    : activeSource === 'codex'
      ? (scope.scopesSupported && scope.accountKey == null
        ? null
        : codexQuota?.latest_percent ?? null)
      : null;
  const weeklyThresholds = activeSource === 'claude'
    ? claudeThresholds
    : activeSource === 'codex'
      ? codexThresholds
      : [...new Set([...claudeThresholds, ...codexThresholds])].sort((a, b) => a - b);
  const rows = focusedRows.slice(0, ALERTS_MODAL_CAP);
  const showSourceColumn = activeSource === 'all';
  const showAccountColumn = rows.some((row) => alertAccount(row) != null);
  // #661 S2 section 6.1 — the non-threshold family, from its OWN wire array.
  // Filtered by the active provider tab the same way the threshold rows are,
  // because ownership is a rendering decision the row already carries.
  const rateChanges = (env?.meter_rate_changes ?? []).filter(
    (row) => activeSource === 'all' || row.owner === activeSource,
  );
  const showRateAccountColumn = rateChanges.some((r) => r.accountLabel != null);

  if (rows.length === 0 && rateChanges.length === 0) {
    return (
      <Modal title="Recent alerts" accentClass="accent-amber">
        <AlertsEmptyGauge source={activeSource} usedPct={usedPct} thresholds={weeklyThresholds} />
      </Modal>
    );
  }

  if (rows.length === 0) {
    // A store with a recorded rate transition and no threshold crossing is a
    // real state, and the empty gauge would hide the one thing there IS to
    // report. The gauge still renders, because "no thresholds crossed" is
    // also true and is what it says.
    return (
      <Modal title="Recent alerts" accentClass="accent-amber">
        <div className="alerts-modal-body">
          <AlertsEmptyGauge source={activeSource} usedPct={usedPct} thresholds={weeklyThresholds} />
          <RateChangeSection
            rows={rateChanges} ctx={ctx}
            showAccountColumn={showRateAccountColumn}
          />
        </div>
      </Modal>
    );
  }

  return (
    <Modal title="Recent alerts" accentClass="accent-amber">
      <div className="alerts-modal-body">
        <table className="alerts-table">
          <thead>
            <tr>
              <th scope="col">%</th>
              <th scope="col">Axis</th>
              {showSourceColumn && <th scope="col">Source</th>}
              {showAccountColumn && <th scope="col">Account</th>}
              <th scope="col">Cost</th>
              <th scope="col">Context</th>
              <th scope="col">Alerted</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const d = alertDisplay(row);
              const isClaude = row.source === 'claude';
              const severity = isClaude ? alertSeverity(row) : d.severity;
              const account = alertAccount(row);
              // #574 — same helper as the panel's when-cell, deliberately:
              // `fmt.startedShortOrNull` returns null for a null instant AND
              // for a non-empty string that does not parse, so neither surface
              // can emit title="—" and neither carries its own copy of that
              // rule. A truthiness test on `d.whenIso` would miss the second
              // case.
              const whenTitle = fmt.startedShortOrNull(d.whenIso, ctx);
              const whenText = fmt.relativeOrAbsolute(d.whenIso ?? '', ctx);
              return (
                <tr key={toastAlertId(row)} className="alert-modal-row">
                  <td className={`alert-threshold alert-cell-threshold severity-${severity} ${severity} num`}>
                    {d.threshold}%
                  </td>
                  <td className="alert-cell-axis">
                    <span className={`chip ${d.chipClass}`}>
                      {isClaude ? AXIS_CHIP_LABEL[row.axis] : d.chipLabel}
                    </span>
                  </td>
                  {showSourceColumn && (
                    <td className="alert-cell-source">
                      <span className={`source-chip source-chip--${d.source}`}>{d.sourceLabel}</span>
                    </td>
                  )}
                  {showAccountColumn && (
                    <td className="alert-cell-account">
                      {account != null && (
                        <span className="alert-account-chip" title={account.label}>
                          {account.label}
                        </span>
                      )}
                    </td>
                  )}
                  <td className="alert-cell-cost num">
                    {isClaude ? <CostCell alert={row} /> : <CodexCostCell />}
                  </td>
                  <td className="alert-cell-context">
                    {isClaude ? <ContextCell alert={row} ctx={ctx} /> : <CodexContextCell row={row} />}
                    {isClaude && <AlertFollowCell alert={row} env={env} />}
                  </td>
                  <td
                    className="alert-cell-when alert-when"
                    title={whenTitle ?? undefined}
                  >
                    {whenText}
                    {whenText === '—' ? null : <> <ZoneTag tz={ctx.tz} /></>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {focusedRows.length > ALERTS_MODAL_CAP && (
          <div className="alerts-modal-foot">
            Showing {ALERTS_MODAL_CAP} of {focusedRows.length} most recent
          </div>
        )}
        {rateChanges.length > 0 && (
          <RateChangeSection
            rows={rateChanges} ctx={ctx}
            showAccountColumn={showRateAccountColumn}
          />
        )}
      </div>
    </Modal>
  );
}
