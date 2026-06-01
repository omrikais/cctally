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
