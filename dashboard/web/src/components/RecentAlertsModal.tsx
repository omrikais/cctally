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
import { alertDisplay, selectSourceAlertRows } from '../lib/alertIdentity';
import { resolveSourceView } from '../store/sourceView';
import { AlertsEmptyGauge } from './AlertsEmptyGauge';
import type { AlertEntry, CodexAlertRow, SourceAlertRow } from '../types/envelope';

// Recent alerts modal — full history (last 100). ESC and backdrop
// close via the shared `<Modal>` chrome (which also handles the
// `accent-amber` border + close button).
//
// #294 S5 §6.7 — the modal is source-aware, sharing the panel's seam. It reads
// the active source's alert projection (`selectSourceAlertRows`) when a `sources`
// bundle is present, else falls back to the legacy `state.alerts` (wrapped as
// Claude rows) for older servers / unit tests. Claude rows render richly (full
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

export function RecentAlertsModal(): JSX.Element {
  const env = useSnapshot();
  const activeSource = useSyncExternalStore(subscribeStore, () => getState().activeSource);
  const legacyAlerts = useSyncExternalStore(subscribeStore, () => getState().alerts);
  const hasBundle = env?.sources != null;
  const view = resolveSourceView(env ?? null, activeSource);
  const claudeLegacyRows: SourceAlertRow[] = legacyAlerts.map((a) => ({
    ...a,
    source: 'claude' as const,
    key: a.id,
  }));
  // #294 S5 §6.7 / §5.2 — see RecentAlertsPanel: Codex/All read the source
  // projection; Claude reads the legacy top-level projection when populated.
  const allRows: SourceAlertRow[] =
    !hasBundle
      ? claudeLegacyRows
      : activeSource === 'claude' && claudeLegacyRows.length > 0
        ? claudeLegacyRows
        : selectSourceAlertRows(view);

  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  const usedPct = env?.header?.used_pct ?? null;
  const alertsConfig = useSyncExternalStore(subscribeStore, () => getState().alertsConfig);
  const weeklyThresholds =
    alertsConfig.weekly_thresholds?.length ? alertsConfig.weekly_thresholds : [90, 95];
  const rows = allRows.slice(0, ALERTS_MODAL_CAP);
  const showSourceColumn = activeSource === 'all';

  if (rows.length === 0) {
    return (
      <Modal title="Recent alerts" accentClass="accent-amber">
        <AlertsEmptyGauge source={activeSource} usedPct={usedPct} thresholds={weeklyThresholds} />
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
              return (
                <tr key={`${d.source}:${row.key}`} className="alert-modal-row">
                  <td className={`alert-threshold severity-${severity} ${severity} num`}>
                    {d.threshold}%
                  </td>
                  <td>
                    <span className={`chip ${d.chipClass}`}>
                      {isClaude ? AXIS_CHIP_LABEL[row.axis] : d.chipLabel}
                    </span>
                  </td>
                  {showSourceColumn && (
                    <td>
                      <span className={`source-chip source-chip--${d.source}`}>{d.sourceLabel}</span>
                    </td>
                  )}
                  <td className="num">
                    {isClaude ? <CostCell alert={row} /> : <CodexCostCell />}
                  </td>
                  <td>
                    {isClaude ? <ContextCell alert={row} ctx={ctx} /> : <CodexContextCell row={row} />}
                  </td>
                  <td className="alert-when">
                    {fmt.relativeOrAbsolute(d.whenIso ?? '', ctx)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {allRows.length > ALERTS_MODAL_CAP && (
          <div className="alerts-modal-foot">
            Showing {ALERTS_MODAL_CAP} of {allRows.length} most recent
          </div>
        )}
      </div>
    </Modal>
  );
}
