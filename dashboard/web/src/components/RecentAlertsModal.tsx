import { useSyncExternalStore } from 'react';
import { Modal } from '../modals/Modal';
import { getState, subscribeStore } from '../store/store';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { fmt } from '../lib/fmt';
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
// Threshold severity is amber<95, red>=95 (chip-class on the % cell).

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
  const v =
    alert.axis === 'weekly'
      ? alert.context.cumulative_cost_usd
      : alert.context.block_cost_usd;
  return <span className="num">{fmt.usd2(v ?? null)}</span>;
}

export function RecentAlertsModal(): JSX.Element {
  const allAlerts = useSyncExternalStore(
    subscribeStore,
    () => getState().alerts,
  );
  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  const alerts = allAlerts.slice(0, ALERTS_MODAL_CAP);

  if (alerts.length === 0) {
    return (
      <Modal title="Recent alerts" accentClass="accent-amber">
        <div className="panel-empty">
          No alerts yet. Alerts appear when usage crosses 90% or 95%.
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
              <th scope="col">Cost</th>
              <th scope="col">Context</th>
              <th scope="col">Alerted</th>
            </tr>
          </thead>
          <tbody>
            {alerts.map((a) => {
              const severity = a.threshold >= 95 ? 'red' : 'amber';
              return (
                <tr key={a.id} className="alert-modal-row">
                  <td
                    className={`alert-threshold severity-${severity} ${severity} num`}
                  >
                    {a.threshold}%
                  </td>
                  <td>
                    <span className={`chip chip--${a.axis}`}>
                      {a.axis === 'weekly' ? 'WEEKLY' : '5H-BLOCK'}
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
