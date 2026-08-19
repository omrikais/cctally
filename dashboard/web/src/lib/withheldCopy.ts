import type { AggregateWithheld } from './dashboardPresentation';

// #556 S2 §3.7 — copy for a withheld aggregate.
//
// A withheld outcome must render as ITS OWN state, distinct from both "no
// activity yet" and "restart the dashboard", and the copy must name which fact
// is missing. Reporting a range problem as emptiness is the failure this whole
// section exists to remove; reporting it as a broken instance sends the user to
// fix something that is not broken.
//
// The switch has a REQUIRED fallback branch. The code union is closed for the
// server and for tests and deliberately OPEN here: the in-place update path
// lets an old client meet a newer server without reloading the JavaScript, so
// an unheard-of code must render generic copy rather than nothing.

function providerName(provider: string | undefined): string {
  return provider === 'codex' ? 'Codex' : provider === 'claude' ? 'Claude' : 'A provider';
}

/**
 * `noun` names the thing being withheld, e.g. "ranking" or "history", so one
 * message works for both aggregates without either panel inventing its own.
 */
export function withheldMessage(result: AggregateWithheld, noun: string): string {
  switch (result.code) {
    case 'range_unresolved':
      return `The shared range could not be resolved, so the combined ${noun} is withheld.`;
    case 'provider_unavailable':
      return `${providerName(result.provider)} data is unavailable, so the combined ${noun} is withheld.`;
    case 'provider_incoherent':
      return `${providerName(result.provider)} data is out of date, so the combined ${noun} is withheld.`;
    case 'claude_fold_failed':
      return `Claude's totals for the shared range could not be computed, so the combined ${noun} is withheld.`;
    case 'retained_range_mismatch':
      return `The two providers describe different ranges, so the combined ${noun} is withheld.`;
    case 'rows_absent':
      return `This page is talking to a server that does not publish the combined ${noun}. Reload to pick up the current one.`;
    default:
      // An unknown code from a newer server. Say what is true — the figure is
      // withheld — and carry the code so a bug report can name it.
      return `The combined ${noun} is withheld (${result.code}).`;
  }
}

// #620 S1 D5 (F10) — why the forecast's `$ / 1%` rate is withheld.
//
// The server emits `dollars_per_percent: null` together with a companion
// `dollars_per_percent_source` code; it owns a closed cause set, and today
// `no_usage_observed` is the only member that accompanies a withheld value.
// The client types the code as a bare `string` and carries a REQUIRED fallback
// branch for the same reason `withheldMessage` does: the in-place update path
// lets an old client meet a newer server without reloading the JavaScript, so
// an unheard-of code must render generic copy rather than nothing.
export function dollarsPerPercentReason(code: string | null | undefined): string {
  switch (code) {
    case 'no_usage_observed':
      return 'No quota usage has been observed in this window, so there is no rate to measure.';
    case null:
    case undefined:
    case '':
      return 'The $ / 1% rate is unavailable for this window.';
    default:
      // An unknown code from a newer server. Say what is true — the rate is
      // withheld — and carry the code so a bug report can name it.
      return `The $ / 1% rate is unavailable for this window (${code}).`;
  }
}
