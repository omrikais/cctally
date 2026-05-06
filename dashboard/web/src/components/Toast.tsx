import { useEffect, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { OnboardingToast } from './OnboardingToast';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { fmt } from '../lib/fmt';

// Toast variant pattern (T8). The `status` shape is the legacy
// transient message (2.5s auto-dismiss); the `alert` shape is a
// percent-crossing alert with rich content (8s auto-dismiss +
// click-to-dismiss). Severity color flips amber→red at threshold ≥95.
const STATUS_DISMISS_MS = 2500;
const ALERT_DISMISS_MS = 8000;

export function Toast() {
  const toast = useSyncExternalStore(subscribeStore, () => getState().toast);
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

  return (
    <>
      <OnboardingToast />
      {toast?.kind === 'status' && (
        <div className="toast" role="status" aria-live="polite">
          {toast.text}
        </div>
      )}
      {toast?.kind === 'alert' && (
        <div
          className={`toast toast--alert toast--severity-${toast.payload.threshold >= 95 ? 'red' : 'amber'}`}
          role="alert"
          onClick={() => dispatch({ type: 'HIDE_TOAST' })}
        >
          <div className="toast--alert-head">
            <span className={`chip chip--${toast.payload.axis}`}>
              {toast.payload.axis === 'weekly' ? 'WEEKLY' : '5H-BLOCK'}
            </span>
            <span className="toast--alert-threshold num">
              {toast.payload.threshold}%
            </span>
            <span className="toast--alert-dismiss-hint">click to dismiss</span>
          </div>
          <div className="toast--alert-title">
            {toast.payload.axis === 'weekly' ? 'Weekly' : '5h-block'} usage{' '}
            {toast.payload.threshold}% reached
          </div>
          {toast.payload.context.week_start_date && (
            <div className="toast--alert-sub">
              Week starting{' '}
              {fmt.weekStart(toast.payload.context.week_start_date, ctx) ?? '—'}
            </div>
          )}
          {toast.payload.context.block_start_at && (
            <div className="toast--alert-sub">
              Block started {fmt.timeOnly(toast.payload.context.block_start_at, ctx)}
            </div>
          )}
          <div className="toast--alert-body">
            {toast.payload.context.cumulative_cost_usd != null && (
              <>
                <span className="num">
                  ${toast.payload.context.cumulative_cost_usd.toFixed(2)}
                </span>{' '}
                spent
                {toast.payload.context.dollars_per_percent != null && (
                  <>
                    {' '}·{' '}
                    <span className="num">
                      ${toast.payload.context.dollars_per_percent.toFixed(2)}
                    </span>{' '}
                    per 1%
                  </>
                )}
              </>
            )}
            {toast.payload.context.block_cost_usd != null && (
              <>
                <span className="num">
                  ${toast.payload.context.block_cost_usd.toFixed(2)}
                </span>{' '}
                in this block
                {toast.payload.context.primary_model && (
                  <> · model: {toast.payload.context.primary_model}</>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </>
  );
}
