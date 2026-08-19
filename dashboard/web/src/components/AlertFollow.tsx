import { alertNavigation, envelopeNow } from '../lib/alertScope';
import { followAlertTarget } from '../store/followAlertTarget';
import type { AlertEntry, Envelope } from '../types/envelope';

// #620 S1 D12 — the follow affordance, or the sentence saying why there is
// none. Rendered under the context text on the same cell, so no column is
// added: a new column would be a new surface, and the boundary forbids one.
//
// A native `<button>`, so Tab reaches it and Enter or Space activates it
// without the panel-focus flow residual R7 records as absent.
export function AlertFollowCell({
  alert,
  env,
  onFollow,
}: {
  alert: AlertEntry;
  env: Envelope | null;
  /** Ran after the target is opened. The toast retires itself here, because
   *  leaving it over the modal it just opened would cover the answer. */
  onFollow?: () => void;
}): JSX.Element {
  const nav = alertNavigation(alert, env, envelopeNow(env));
  if (!nav.available || nav.target == null) {
    return <span className="alert-row-withheld">{nav.withheldReason}</span>;
  }
  const target = nav.target;
  return (
    <button
      type="button"
      className="alert-row-open"
      onClick={(e) => {
        e.stopPropagation();
        followAlertTarget(target);
        onFollow?.();
      }}
    >
      {target.label}
    </button>
  );
}
