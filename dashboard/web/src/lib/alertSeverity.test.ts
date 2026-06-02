// alertSeverity — single severity authority (Task F).
//
// The Python kernel `bin/_lib_alert_axes.py::severity_for` emits
// `alert.severity` on every envelope item; this helper CONSUMES it. The
// fallback (derive from `threshold`) only fires when `severity` is absent
// (stale server), reproducing the EXACT same amber<95 / red>=95 rule.
import { describe, expect, it } from 'vitest';
import { alertSeverity } from './alertAxis';
import type { AlertEntry } from '../types/envelope';

function entry(partial: Partial<AlertEntry>): AlertEntry {
  return {
    id: 'weekly:2026-04-13:90:0',
    axis: 'weekly',
    threshold: 90,
    crossed_at: '2026-04-16T12:00:00Z',
    alerted_at: '2026-04-16T12:00:00Z',
    context: {},
    ...partial,
  };
}

describe('alertSeverity', () => {
  it('consumes alert.severity when present (does NOT recompute from threshold)', () => {
    // Smoking-gun: severity:'red' with a threshold (50) that WOULD derive
    // 'amber' if recomputed. Consumption ⇒ 'red'.
    expect(alertSeverity(entry({ severity: 'red', threshold: 50 }))).toBe('red');
    // And the inverse: severity:'amber' with a threshold (99) that would
    // derive 'red'.
    expect(alertSeverity(entry({ severity: 'amber', threshold: 99 }))).toBe(
      'amber',
    );
  });

  it('falls back to threshold derivation when severity is absent', () => {
    expect(alertSeverity(entry({ threshold: 90 }))).toBe('amber');
    expect(alertSeverity(entry({ threshold: 94 }))).toBe('amber');
    expect(alertSeverity(entry({ threshold: 95 }))).toBe('red');
    expect(alertSeverity(entry({ threshold: 100 }))).toBe('red');
  });
});
