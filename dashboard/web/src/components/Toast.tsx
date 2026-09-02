import { useEffect, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { OnboardingToast } from './OnboardingToast';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { fmt } from '../lib/fmt';
import {
  AXIS_TITLE_LABEL,
  budgetPeriodNoun,
  projectedContextText,
} from '../lib/alertAxis';
import { alertDisplay } from '../lib/alertIdentity';
import { AlertFollowCell } from './AlertFollow';
import { useScopedSnapshot } from '../hooks/useScopedSnapshot';
import { rateChangeSummary } from '../lib/quotaCopy';
import { isMeterRateChangePayload } from '../store/store';
import type {
  AlertEntry,
  CodexAlertRow,
  ClaudeAlertSourceRow,
  MeterRateChangeEntry,
  SourceAlertRow,
} from '../types/envelope';

// Normalize a toast payload (a legacy AlertEntry from SHOW_ALERT_TOAST /
// INGEST_SNAPSHOT_ALERTS, or a source-qualified row from INGEST_SOURCE_ALERTS)
// into a SourceAlertRow so the Toast renders both uniformly (§6.7).
function normalizeToastRow(p: AlertEntry | SourceAlertRow): SourceAlertRow {
  return 'source' in p ? p : { ...p, source: 'claude', key: p.id };
}

// Toast variant pattern (T8). The `status` shape is the legacy
// transient message (2.5s auto-dismiss); the `alert` shape is a
// percent-crossing alert with rich content (8s auto-dismiss +
// click-to-dismiss). Severity color flips amber→red at threshold ≥95.
//
// #294 S5 §6.7 — the payload is a source-qualified row. Both Claude and Codex
// toasts show a source chip in the head; the rich body branches by provider
// (Claude keeps the full context/cost rendering; Codex renders its lean row).
const STATUS_DISMISS_MS = 2500;
const ALERT_DISMISS_MS = 8000;

function ClaudeToastBody({
  alert,
  ctx,
}: {
  alert: ClaudeAlertSourceRow;
  ctx: { tz: string; offsetLabel: string };
}): JSX.Element {
  return (
    <>
      <div className="toast--alert-title">
        {alert.axis === 'projected' ? (
          <>
            {AXIS_TITLE_LABEL.projected} to reach {alert.threshold}%
          </>
        ) : alert.axis === 'project_budget' ? (
          <>
            {alert.context.project ?? 'Project'} budget {alert.threshold}% reached
          </>
        ) : alert.axis === 'codex_budget' ? (
          <>
            {AXIS_TITLE_LABEL.codex_budget} {alert.threshold}% reached
          </>
        ) : (
          <>
            {AXIS_TITLE_LABEL[alert.axis]} usage {alert.threshold}% reached
          </>
        )}
      </div>
      {alert.context.week_start_date && (
        <div className="toast--alert-sub">
          Week starting {fmt.weekStart(alert.context.week_start_date, ctx) ?? '—'}
        </div>
      )}
      {alert.context.block_start_at && (
        <div className="toast--alert-sub">
          Block started {fmt.timeOnly(alert.context.block_start_at, ctx)}
        </div>
      )}
      {(alert.axis === 'budget' ||
        alert.axis === 'project_budget' ||
        alert.axis === 'codex_budget') &&
        (alert.context.period_start_at ?? alert.context.week_start_at) && (
          <div className="toast--alert-sub">
            {budgetPeriodNoun(alert.context.period)} starting{' '}
            {fmt.weekStart(
              (alert.context.period_start_at ?? alert.context.week_start_at ?? '').slice(0, 10),
              ctx,
            ) ?? '—'}
          </div>
        )}
      <div className="toast--alert-body">
        {alert.context.cumulative_cost_usd != null && (
          <>
            <span className="num">${alert.context.cumulative_cost_usd.toFixed(2)}</span> spent
            {alert.context.dollars_per_percent != null && (
              <>
                {' '}·{' '}
                <span className="num">${alert.context.dollars_per_percent.toFixed(2)}</span> per 1%
              </>
            )}
          </>
        )}
        {alert.context.block_cost_usd != null && (
          <>
            <span className="num">${alert.context.block_cost_usd.toFixed(2)}</span> in this block
            {alert.context.primary_model && <> · model: {alert.context.primary_model}</>}
          </>
        )}
        {(alert.axis === 'budget' || alert.axis === 'codex_budget') &&
          alert.context.spent_usd != null &&
          alert.context.budget_usd != null && (
            <>
              <span className="num">${alert.context.spent_usd.toFixed(2)}</span> of{' '}
              <span className="num">${alert.context.budget_usd.toFixed(2)}</span> budget
            </>
          )}
        {alert.axis === 'projected' && (
          <span className="num">{projectedContextText(alert) ?? '—'}</span>
        )}
        {alert.axis === 'project_budget' &&
          alert.context.spent_usd != null &&
          alert.context.budget_usd != null && (
            <>
              {alert.context.project && <>{alert.context.project}: </>}
              <span className="num">${alert.context.spent_usd.toFixed(2)}</span> of{' '}
              <span className="num">${alert.context.budget_usd.toFixed(2)}</span> budget
            </>
          )}
      </div>
    </>
  );
}

function CodexToastBody({ row }: { row: CodexAlertRow }): JSX.Element {
  // The lean Codex source rows (budget/projected carry `value`; quota carries
  // `severity` only). Render an honest title from what the row carries.
  const title =
    row.axis === 'quota'
      ? `Codex quota ${row.threshold}% reached`
      : row.axis === 'projected'
        ? `Codex projected to reach ${row.threshold}%`
        : `Codex budget ${row.threshold}% reached`;
  return (
    <>
      <div className="toast--alert-title">{title}</div>
      {row.axis !== 'quota' && (
        <div className="toast--alert-body">
          <span className="num">{Math.round(row.value)}%</span> of budget
        </div>
      )}
    </>
  );
}

// #661 S2 section 6.1 — the non-threshold family's OWN toast branch, on the
// variant tag. It is not a widening of the threshold body: there is no
// threshold to name, no threshold-derived severity, and no window to follow,
// so the head renders the family's explicit severity and the body states the
// direction and size of the change plus the one command that explains it.
function RateChangeToast({
  entry,
  onDismiss,
}: {
  entry: MeterRateChangeEntry;
  onDismiss: () => void;
}): JSX.Element {
  const summary = rateChangeSummary(
    entry.previous_units_per_point, entry.new_units_per_point,
  );
  const provider = entry.provider
    ? entry.provider.charAt(0).toUpperCase() + entry.provider.slice(1)
    : 'Provider';
  return (
    <div
      className={`toast toast--alert toast--severity-${entry.severity}`}
      role="alert"
      onClick={onDismiss}
      data-testid="toast-meter-rate-change"
    >
      <div className="toast--alert-head">
        <span className="chip chip-rate-change">metering rate</span>
        <span className={`source-chip source-chip--${entry.provider}`}>
          {provider}
        </span>
        <span className="toast--alert-dismiss-hint">click to dismiss</span>
      </div>
      <div className="toast--alert-title">{provider} metering rate changed</div>
      {summary && <div className="toast--alert-sub">{summary}</div>}
      {/*
        #688: a transition is recorded whenever the detector qualifies,
        including when `cctally quota` withholds its own verdict, so the
        calibration behind this toast may have no fitted budget at all.
        Pointing at the evidence is true either way. The CAUSE is
        deliberately not named here: this surface is fed from the stats row
        rather than from the alert payload, so carrying `withholding_status`
        would need a new column, its fold, the envelope builder, the
        TypeScript variant and a stats epoch bump. That is #690.
      */}
      <div className="toast--alert-body">
        Run <code>cctally quota</code> for the evidence behind this change.
      </div>
    </div>
  );
}

export function Toast() {
  const toast = useSyncExternalStore(subscribeStore, () => getState().toast);
  const env = useScopedSnapshot();
  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };

  useEffect(() => {
    if (toast == null) return;
    const ms = toast.kind === 'alert' ? ALERT_DISMISS_MS : STATUS_DISMISS_MS;
    const id = window.setTimeout(
      () => dispatch({ type: 'HIDE_TOAST' }),
      ms,
    );
    return () => window.clearTimeout(id);
  }, [toast]);

  const rawPayload = toast?.kind === 'alert' ? toast.payload : null;
  // Branch on the variant tag BEFORE normalizing. `normalizeToastRow` maps a
  // payload onto a threshold-shaped row, and a rate transition has no
  // threshold to map.
  const rateChange =
    rawPayload && isMeterRateChangePayload(rawPayload) ? rawPayload : null;
  const alertPayload =
    rawPayload != null && !isMeterRateChangePayload(rawPayload)
      ? normalizeToastRow(rawPayload)
      : null;
  const d = alertPayload ? alertDisplay(alertPayload) : null;

  return (
    <>
      {/* #207 D8 — keep the onboarding toast MOUNTED (so its 8s auto-dismiss
          timer keeps running) but visually suppressed while a status/alert
          toast is live, so the two can't overlap at narrow widths. */}
      <OnboardingToast suppressed={toast != null} />
      {toast?.kind === 'status' && (
        <div className="toast" role="status" aria-live="polite">
          {toast.text}
        </div>
      )}
      {rateChange && (
        <RateChangeToast
          entry={rateChange}
          onDismiss={() => dispatch({ type: 'HIDE_TOAST' })}
        />
      )}
      {alertPayload && d && (
        <div
          className={`toast toast--alert toast--severity-${d.severity}`}
          role="alert"
          onClick={() => dispatch({ type: 'HIDE_TOAST' })}
        >
          <div className="toast--alert-head">
            <span className={`chip ${d.chipClass}`}>{d.chipLabel}</span>
            <span className={`source-chip source-chip--${d.source}`}>{d.sourceLabel}</span>
            <span className="toast--alert-threshold num">{d.threshold}%</span>
            <span className="toast--alert-dismiss-hint">click to dismiss</span>
          </div>
          {alertPayload.source === 'claude' ? (
            <ClaudeToastBody alert={alertPayload} ctx={ctx} />
          ) : (
            <CodexToastBody row={alertPayload} />
          )}
          {/* #620 S1 D12 — the toast is the first place a warning is seen, so
              it offers the same next step the alerts modal does. The click
              handler on the container above dismisses; this one stops that
              propagation, follows the target, and retires the toast itself. */}
          {alertPayload.source === 'claude' && (
            <div className="toast--alert-follow">
              <AlertFollowCell
                alert={alertPayload}
                env={env}
                onFollow={() => dispatch({ type: 'HIDE_TOAST' })}
              />
            </div>
          )}
        </div>
      )}
    </>
  );
}
