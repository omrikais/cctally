import { useSyncExternalStore } from 'react';
import { Modal } from '../modals/Modal';
import { getState, subscribeStore } from '../store/store';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { useSnapshot } from '../hooks/useSnapshot';
import { fmt } from '../lib/fmt';
import {
  alertSeverity,
  AXIS_CHIP_LABEL,
  budgetPeriodNoun,
  projectedContextText,
} from '../lib/alertAxis';
import { AlertsEmptyGauge } from './AlertsEmptyGauge';
import type { AlertEntry } from '../types/envelope';

// Recent alerts modal — full history (last 100). ESC and backdrop
// close via the shared `<Modal>` chrome (which also handles the
// `accent-amber` border + close button).
//
// IMPORTANT: the T5 envelope-rebuild path does NOT include
// `primary_model` for 5h alerts (only the live-dispatch payload
// does). The modal MUST therefore render 5h context as
// "Block HH:MM" with no model fragment, regardless of whether
// `context.primary_model` happens to be present — the rule is
// "never depend on primary_model for envelope-sourced 5h rows."
//
// Severity (chip-class on the % cell) comes from the kernel via
// `alertSeverity(a)` — consumes `alert.severity` (info <90 / warn 90-99 /
// critical >=100), with a threshold-derivation fallback for stale envelopes
// (and legacy amber/red normalization). See lib/alertAxis.ts.

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
    // Budget axis (issue #19): the window anchor is `week_start_at` (the
    // effective post-reset ISO *timestamp*), distinct from weekly's
    // `week_start_date` (a date-only string). `fmt.weekStart` renders a
    // calendar `YYYY-MM-DD`, so slice the date prefix off the timestamp
    // before routing through it. Period generalization
    // (calendar-period-codex-budgets, spec §6): the noun is period-aware
    // ("Month" / "Calendar week" / "Week") from `context.period`, so a
    // calendar budget no longer mislabels as "Week"; the subscription-week
    // default keeps the legacy "Week of …" rendering byte-stable. Show
    // consumption-of-budget as the secondary.
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
    // Codex budget axis (calendar-period-codex-budgets, spec §6): per-vendor
    // Codex budget over a CALENDAR period. The window anchor is
    // `period_start_at` (the resolved period-window start instant); the noun is
    // period-aware ("Month" / "Calendar week") from `context.period`.
    // `consumption_pct` is read from the row (snapshotted at crossing).
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
    // Per-project budget axis (issue #19/#121): like budget, but the context
    // leads with the project basename so the user knows WHICH project crossed.
    // `week_start_at` is the effective post-reset ISO timestamp (slice the
    // date prefix for fmt.weekStart); `consumption_pct` is read from the row.
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
    // Projected axis (issue #121): metric-aware context, read from the row
    // (Codex P0-4). The `metric` discriminator drives a single helper
    // (alertAxis.projectedContextText) rather than nested axis/metric arms.
    const text = projectedContextText(alert);
    return (
      <span className="alert-context alert-context--projected">
        {text ?? 'Projected —'}
      </span>
    );
  }
  // axis === 'five_hour' — render block start time only. Do NOT
  // append any model token here (see header comment).
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
    // Projected axis (issue #121): there is no realized spend — the
    // projection lives in the Context column (metric-aware). The Cost
    // column has nothing to show, so render an em-dash placeholder.
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
    // Budget / per-project / Codex budget axes (issue #19/#121 +
    // calendar-period-codex-budgets): the Cost column shows actual spend
    // (the project's spend for project_budget, the Codex API $ for
    // codex_budget).
    v = alert.context.spent_usd;
  } else {
    v = alert.context.block_cost_usd;
  }
  return <span className="num">{fmt.usd2(v ?? null)}</span>;
}

export function RecentAlertsModal(): JSX.Element {
  const allAlerts = useSyncExternalStore(
    subscribeStore,
    () => getState().alerts,
  );
  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  // RA-1 / #265 A — the empty state teaches instead of stating a bare line. It
  // reads the current weekly used% (header) and the CONFIGURED fire thresholds
  // (alertsConfig.weekly_thresholds, fallback [90, 95]); never hardcode 90/95.
  // The gauge markup lives in the shared <AlertsEmptyGauge> so the panel + modal
  // can't drift; when used% is unknown it renders the one-liner fallback.
  const env = useSnapshot();
  const usedPct = env?.header?.used_pct ?? null;
  const alertsConfig = useSyncExternalStore(subscribeStore, () => getState().alertsConfig);
  const weeklyThresholds =
    alertsConfig.weekly_thresholds?.length ? alertsConfig.weekly_thresholds : [90, 95];
  const alerts = allAlerts.slice(0, ALERTS_MODAL_CAP);

  if (alerts.length === 0) {
    return (
      <Modal title="Recent alerts" accentClass="accent-amber">
        <AlertsEmptyGauge usedPct={usedPct} thresholds={weeklyThresholds} />
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
              <th scope="col">Cost</th>
              <th scope="col">Context</th>
              <th scope="col">Alerted</th>
            </tr>
          </thead>
          <tbody>
            {alerts.map((a) => {
              const severity = alertSeverity(a);
              return (
                <tr key={a.id} className="alert-modal-row">
                  <td
                    className={`alert-threshold severity-${severity} ${severity} num`}
                  >
                    {a.threshold}%
                  </td>
                  <td>
                    <span className={`chip chip--${a.axis}`}>
                      {AXIS_CHIP_LABEL[a.axis]}
                    </span>
                  </td>
                  <td className="num">
                    <CostCell alert={a} />
                  </td>
                  <td>
                    <ContextCell alert={a} ctx={ctx} />
                  </td>
                  <td className="alert-when">
                    {fmt.relativeOrAbsolute(a.alerted_at, ctx)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {allAlerts.length > ALERTS_MODAL_CAP && (
          <div className="alerts-modal-foot">
            Showing {ALERTS_MODAL_CAP} of {allAlerts.length} most recent
          </div>
        )}
      </div>
    </Modal>
  );
}
