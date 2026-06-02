import type { AlertAxis, AlertEntry } from '../types/envelope';

// Shared alert-axis labels (issue #19 widened the binary weekly|five_hour
// union with a third `budget` axis; issue #121 adds the fourth `projected`
// axis). Single source of truth so Toast / RecentAlertsPanel /
// RecentAlertsModal never drift on the chip text. The chip uses the SHOUT
// form; the title uses the sentence-case form.
//
// These MUST stay byte-identical with the Python kernel
// bin/_lib_alert_axes.py AXIS_REGISTRY chip_label / title_label fields
// (`PROJECTED` / `Projected`, etc.).
export const AXIS_CHIP_LABEL: Record<AlertAxis, string> = {
  weekly: 'WEEKLY',
  five_hour: '5H-BLOCK',
  budget: 'BUDGET',
  projected: 'PROJECTED',
};

export const AXIS_TITLE_LABEL: Record<AlertAxis, string> = {
  weekly: 'Weekly',
  five_hour: '5h-block',
  budget: 'Budget',
  projected: 'Projected',
};

// Single severity authority (Phase B 3-tier). The Python kernel
// `bin/_lib_alert_axes.py::severity_for` emits the 3-tier `alert.severity`
// token on every envelope item — `info` (<90) / `warn` (90-99) / `critical`
// (>=100) — and this helper consumes it so the frontend never recomputes the
// band split independently. Three safety nets keep rendering correct against
// older backends:
//   1. A pre-Phase-B server that still emits the legacy `amber`/`red` tokens
//      is normalized onto the closest new tier (amber→warn, red→critical) so
//      the rendered class is always a known `.alert-threshold.{tier}` rule.
//   2. An envelope that predates the `severity` field entirely (no token)
//      falls back to deriving the tier from `threshold` — byte-identical with
//      the Python kernel bands.
//   3. Any other unexpected string also lands on the threshold fallback.
export function alertSeverity(alert: AlertEntry): 'info' | 'warn' | 'critical' {
  const s = alert.severity as string | undefined;
  if (s === 'info' || s === 'warn' || s === 'critical') return s;
  if (s === 'amber') return 'warn'; // legacy token from a stale backend
  if (s === 'red') return 'critical'; // legacy token from a stale backend
  // threshold fallback — byte-identical with the Python kernel bands
  // (info <90 / warn 90-99 / critical >=100).
  return alert.threshold >= 100 ? 'critical' : alert.threshold >= 90 ? 'warn' : 'info';
}

// Metric-aware renderer for the `projected` axis (Codex P2-2). The
// chip/title maps above are text-only; they don't cover the context + cost
// cells, which the modal and toast still branch by axis to build. Rather
// than nest `axis === 'projected' && metric === …` ladders at each render
// site, the `metric` discriminator drives this single helper. Values are
// read FROM THE ROW (`context.projected_value` / `context.denominator`),
// never from live config (Codex P0-4).
//
//   weekly_pct  → "projected 102% of cap"
//   budget_usd  → "projected $312 of $300"
//
// Returns `null` when the row is not a projected alert or lacks the
// projection fields, so callers can fall through to their existing axis
// arms without a defensive branch of their own.
export function projectedContextText(alert: AlertEntry): string | null {
  if (alert.axis !== 'projected') return null;
  const metric = alert.metric ?? alert.context.metric;
  const proj = alert.context.projected_value;
  if (metric == null || proj == null) return null;
  if (metric === 'weekly_pct') {
    return `projected ${Math.round(proj)}% of cap`;
  }
  // budget_usd
  const denom = alert.context.denominator;
  if (denom == null) return null;
  return `projected $${Math.round(proj)} of $${Math.round(denom)}`;
}
