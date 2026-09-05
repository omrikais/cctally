import { useEffect, useSyncExternalStore } from 'react';
import type {
  FocusEvent as ReactFocusEvent,
  KeyboardEvent as ReactKeyboardEvent,
} from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { OnboardingToast } from './OnboardingToast';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { fmt } from '../lib/fmt';
import {
  AXIS_TITLE_LABEL,
  budgetPeriodNoun,
  projectedContextText,
} from '../lib/alertAxis';
import { alertAccount, alertDisplay } from '../lib/alertIdentity';
import {
  clearFocusOrigin,
  recordFocusOrigin,
  restoreFocusAfterDismiss,
} from '../lib/toastFocus';
import { AlertFollowCell } from './AlertFollow';
import { useScopedSnapshot } from '../hooks/useScopedSnapshot';
import { rateChangeSummary } from '../lib/quotaCopy';
import { rateChangeWithheldCopy } from '../lib/withheldCopy';
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

// #749 — the two dismiss strings, and why there are two.
//
// The undecorated head keeps the literal it has always carried, in its
// current position. R8 byte-stability governs what a SINGLE-account install
// renders, so editing that string would change the one surface the rule
// protects. The own-line form is new, so it is free to be viewport-neutral,
// and it must be: it renders on touch installs where "click" is wrong.
const IN_HEAD_DISMISS_HINT = 'click to dismiss';
const OWN_LINE_DISMISS_HINT = 'tap or click to dismiss';

// #750 S2 review — the titles the two bodies render, as strings.
//
// The accessible name is built from these, not from a second sentence written
// beside them. The review found the label saying "95% reached" on the
// `projected` axis, whose visible title reads "Projected to reach 95%" and
// whose whole point is that nothing has been reached yet, and saying "project
// alert" where the title names the project. One branch cannot drift from the
// other when there is only one branch.
function claudeToastTitle(alert: ClaudeAlertSourceRow): string {
  if (alert.axis === 'projected') {
    return `${AXIS_TITLE_LABEL.projected} to reach ${alert.threshold}%`;
  }
  if (alert.axis === 'project_budget') {
    return `${alert.context.project ?? 'Project'} budget ${alert.threshold}% reached`;
  }
  if (alert.axis === 'codex_budget') {
    return `${AXIS_TITLE_LABEL.codex_budget} ${alert.threshold}% reached`;
  }
  return `${AXIS_TITLE_LABEL[alert.axis]} usage ${alert.threshold}% reached`;
}

// The lean Codex source rows (budget/projected carry `value`; quota carries
// `severity` only). Render an honest title from what the row carries.
function codexToastTitle(row: CodexAlertRow): string {
  if (row.axis === 'quota') return `Codex quota ${row.threshold}% reached`;
  if (row.axis === 'projected') return `Codex projected to reach ${row.threshold}%`;
  return `Codex budget ${row.threshold}% reached`;
}

function toastTitleText(row: SourceAlertRow): string {
  return row.source === 'claude' ? claudeToastTitle(row) : codexToastTitle(row);
}

// #749 — explicit activation semantics for a toast root.
//
// Both roots are plain `div`s carrying `onClick`, and making such an element
// focusable is NOT sufficient: unlike a native button, a focusable div does
// not synthesize a click for Enter or Space, so `tabIndex` plus `aria-label`
// would leave the toast focusable and still not dismissible — which satisfies
// nothing. The handler below is what actually makes the affordance reachable
// without a pointer, and it calls the SAME dismiss path the click does.
//
// Nothing here focuses the toast. An arriving alert must not take focus away
// from whatever the person was doing; `role="alert"` already announces it.
//
// `label` is the alert's OWN sentence, not a fixed string. #750 S2's review
// found that `role="alert"` takes its name from the author, so an `aria-label`
// carrying only the dismiss instruction replaced the toast's content as its
// accessible name: focusing the toast deliberately announced how to close it
// and never which threshold on which account had fired. The instruction is
// appended to the alert's own sentence instead.
//
// A nested `role="button"` holding the instruction was the alternative and is
// rejected: ARIA makes a button's descendants presentational, so wrapping the
// threshold toast's content would take the "open this block" button out of the
// accessibility tree — trading a missing announcement for a missing control.
//
// `onClick` is part of this bundle rather than written separately on each
// root. #750 S2's review found the pointer path and the 8-second expiry both
// removing the toast without restoring focus, so a click that had focused the
// root left `document.activeElement` on <body> and left the remembered element
// referenced for the life of the tab. One dismiss chokepoint, used by every
// path, is what stops the two from drifting again.
function activationProps(onDismiss: () => void, label: string): {
  tabIndex: number;
  'aria-label': string;
  onClick: () => void;
  onFocus: (event: ReactFocusEvent<HTMLDivElement>) => void;
  onBlur: (event: ReactFocusEvent<HTMLDivElement>) => void;
  onKeyDown: (event: ReactKeyboardEvent<HTMLDivElement>) => void;
} {
  const dismiss = (): void => {
    restoreFocusAfterDismiss();
    onDismiss();
  };
  return {
    tabIndex: 0,
    'aria-label': `${label} Press Enter or Space to dismiss.`,
    onClick: dismiss,
    onFocus: (event) => {
      if (event.target !== event.currentTarget) return;
      recordFocusOrigin(event.currentTarget, event.relatedTarget);
    },
    onBlur: (event) => {
      // Focus moving to another control INSIDE the toast is not leaving it, so
      // the remembered element stays. Focus moving anywhere else is: the
      // reader has moved on, and a later auto-expiry must not yank them back.
      if (event.currentTarget.contains(event.relatedTarget as Node | null)) return;
      clearFocusOrigin();
    },
    onKeyDown: (event) => {
      // #750 S2 review — the FIRST line of this handler, and why it is first.
      //
      // `keydown` bubbles, and the threshold toast contains a native
      // `<button>` (`AlertFollowCell`). Without this test the root ran for the
      // button's own keys too: `preventDefault()` cancelled the button's
      // activation so no `click` was ever synthesized, and the root then
      // dismissed the toast — the alert vanished and nothing opened. The
      // button's `onClick` calls `stopPropagation`, which guards the pointer
      // path only.
      if (event.target !== event.currentTarget) return;
      if (event.key !== 'Enter' && event.key !== ' ' && event.key !== 'Spacebar') return;
      // Space scrolls the page by default and Enter would re-fire on a
      // repeat, so the activation is claimed rather than shared.
      event.preventDefault();
      dismiss();
    },
  };
}

// #749 — the hint on its own line, below the alert's body.
//
// It renders exactly when the head names an account, which is exactly when
// #700 measured that the two could not share the head. An undecorated install
// still gets the in-head form and this renders nothing at all, so the surface
// R8 governs is untouched.
//
// #750 S2 review — it goes AFTER the body, not between the head and the title.
// The root is a `role="alert"` live region, so DOM order is the announcement
// order: sitting above the title, the instruction was read out before the
// alert it belongs to. Spec §4.2's "on its own line below the head" is still
// satisfied, and the alert is now read before the instruction on every family.
//
// The hint is NOT always the last thing in the toast, and the earlier claim
// here that it was is wrong: a Claude threshold toast places
// `.toast--alert-follow` after it, so on that family the reading order ends
// with the follow button. That ordering is deliberate — the affordance that
// does something is the one worth reaching last — and the instruction still
// follows the alert it belongs to, which is the property this position exists
// for. A Codex toast and a metering-rate toast render no follow button, so
// there the hint is genuinely last.
function OwnLineDismissHint({ show }: { show: boolean }): JSX.Element | null {
  if (!show) return null;
  return (
    <div className="toast--alert-dismiss-hint toast--alert-dismiss-hint--own-line">
      {OWN_LINE_DISMISS_HINT}
    </div>
  );
}

function ClaudeToastBody({
  alert,
  ctx,
}: {
  alert: ClaudeAlertSourceRow;
  ctx: { tz: string; offsetLabel: string };
}): JSX.Element {
  return (
    <>
      <div className="toast--alert-title">{claudeToastTitle(alert)}</div>
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
  return (
    <>
      <div className="toast--alert-title">{codexToastTitle(row)}</div>
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
  // #700 — the same accessor the threshold toasts and the alerts modal use.
  // The presence of the wire field IS the R8 gate; nothing here re-derives it.
  const account = alertAccount(entry);
  const withheld = rateChangeWithheldCopy(entry.withholding_status);
  return (
    <div
      className={`toast toast--alert toast--severity-${entry.severity}`}
      role="alert"
      {...activationProps(
        onDismiss,
        `${provider} metering rate changed.`
        + (account == null ? '' : ` Account ${account.label}.`),
      )}
      data-testid="toast-meter-rate-change"
    >
      <div className="toast--alert-head">
        {/* #693 D2 — the chip carries its severity modifier, as the alerts
            modal's rate-change chip already does. Without it one event read
            amber in the toast and red in the modal. */}
        <span className={`chip chip-rate-change severity-${entry.severity}`}>
          metering rate
        </span>
        <span className={`source-chip source-chip--${entry.provider}`}>
          {provider}
        </span>
        {account != null && (
          <span className="alert-account-chip" title={account.label}>
            {account.label}
          </span>
        )}
        {/* #700 — the account chip and this hint cannot both fit. The head is
            a fixed 360px row with about 326px of content width, and its other
            items already consume roughly 298px, so with both present the hint
            wrapped onto two or three lines on EVERY decorated install: a
            four-character label was enough to trigger it, and the R8 sentinel
            labels `All accounts` and `Unattributed` are twelve characters.
            Measured in a real browser at 1440x900 and 390x844, which is the
            same at both because the toast carries no @media rule at all.
            #749 — the hint is the item that leaves the HEAD, but it no longer
            leaves the toast: dropping it left a decorated install with no
            statement of how to dismiss at all. It renders on its own line
            under the body, at a measured cost of +17.9px against 652.7px of
            headroom. */}
        {account == null && (
          <span className="toast--alert-dismiss-hint">{IN_HEAD_DISMISS_HINT}</span>
        )}
      </div>
      <div className="toast--alert-title">{provider} metering rate changed</div>
      {summary && <div className="toast--alert-sub">{summary}</div>}
      {/*
        #688: a transition is recorded whenever the detector qualifies,
        including when `cctally quota` withholds its own verdict, so the
        calibration behind this toast may have no fitted budget at all.
        #690 supplied the column, its fold, the envelope builder and the
        TypeScript variant, so the cause is named here rather than implied.

        `withheld` is null on TWO different rows and both take the ordinary
        branch: an ordinary confirmed transition, whose status is null beside
        populated evidence, and a legacy or #689-recovered row carrying all
        four nulls. Neither has a withholding to disclose, and reading a null
        status alone as missing evidence would print withheld copy over every
        ordinary change — which is exactly what spec §4.1 forbids.

        The baseline day count and the typed provenance are NOT here. They are
        Recent Alerts' (§4.4): the toast is a fixed 360px surface whose head
        already lost an affordance to width once.
      */}
      <div className="toast--alert-body">
        {withheld != null ? (
          <>
            {withheld} Run <code>cctally quota</code> for the evidence.
          </>
        ) : (
          <>Run <code>cctally quota</code> for the evidence behind this change.</>
        )}
      </div>
      <OwnLineDismissHint show={account != null} />
    </div>
  );
}

export function Toast() {
  const toast = useSyncExternalStore(subscribeStore, () => getState().toast);
  const env = useScopedSnapshot();
  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };

  // #750 S2 review — nothing may hold the remembered element once no toast is
  // live. `restoreFocusAfterDismiss` already clears it on every dismiss path,
  // and `onBlur` clears it when focus leaves the toast for somewhere else;
  // these two are the backstops for the paths that do neither, such as the
  // follow button retiring the toast under its own handler.
  useEffect(() => clearFocusOrigin, []);

  useEffect(() => {
    if (toast == null) {
      clearFocusOrigin();
      return;
    }
    const ms = toast.kind === 'alert' ? ALERT_DISMISS_MS : STATUS_DISMISS_MS;
    const id = window.setTimeout(
      () => {
        // Restore BEFORE the dispatch: the 8-second expiry removes the toast
        // just as a key press does, so it must return the reader's position
        // the same way. It is a no-op unless focus is inside the toast, which
        // `onBlur` is what guarantees.
        restoreFocusAfterDismiss();
        dispatch({ type: 'HIDE_TOAST' });
      },
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
  const alertAccountChip = alertPayload ? alertAccount(alertPayload) : null;

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
          {...activationProps(
            () => dispatch({ type: 'HIDE_TOAST' }),
            `${d.sourceLabel} alert. ${toastTitleText(alertPayload)}.`
            + (alertAccountChip == null ? '' : ` Account ${alertAccountChip.label}.`),
          )}
        >
          <div className="toast--alert-head">
            <span className={`chip ${d.chipClass}`}>{d.chipLabel}</span>
            <span className={`source-chip source-chip--${d.source}`}>{d.sourceLabel}</span>
            {/* #700 — which account crossed. Both provider families render it
                from the shared accessor, so a decorated install can tell the
                toast apart the way the alerts modal already lets it. */}
            {alertAccountChip != null && (
              <span className="alert-account-chip" title={alertAccountChip.label}>
                {alertAccountChip.label}
              </span>
            )}
            <span className="toast--alert-threshold num">{d.threshold}%</span>
            {/* The head-width trade RateChangeToast explains above. This head
                is the tighter of the two, because it also prints a threshold
                percentage — 325.3px of 326.0px on the threshold family, which
                leaves 0.7px of slack and is why the hint cannot stay here. */}
            {alertAccountChip == null && (
              <span className="toast--alert-dismiss-hint">{IN_HEAD_DISMISS_HINT}</span>
            )}
          </div>
          {alertPayload.source === 'claude' ? (
            <ClaudeToastBody alert={alertPayload} ctx={ctx} />
          ) : (
            <CodexToastBody row={alertPayload} />
          )}
          <OwnLineDismissHint show={alertAccountChip != null} />
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
