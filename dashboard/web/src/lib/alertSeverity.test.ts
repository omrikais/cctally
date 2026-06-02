// alertSeverity — single severity authority (Phase B 3-tier).
//
// The Python kernel `bin/_lib_alert_axes.py::severity_for` emits the 3-tier
// `alert.severity` token ('info' <90 / 'warn' 90-99 / 'critical' >=100) on
// every envelope item; this helper CONSUMES it. The fallback (derive from
// `threshold`) only fires when `severity` is absent (stale server),
// reproducing the EXACT same band split. A pre-Phase-B backend may still
// emit the legacy `amber`/`red` tokens — those are normalized to
// `warn`/`critical` so the UI never renders an unknown tier class.
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

describe('alertSeverity 3-tier', () => {
  it('consumes alert.severity when present (does NOT recompute from threshold)', () => {
    // Smoking-gun: severity:'critical' with a threshold (0) that WOULD derive
    // 'info' if recomputed. Consumption ⇒ 'critical'.
    expect(alertSeverity(entry({ severity: 'critical', threshold: 0 }))).toBe(
      'critical',
    );
    // And the inverse: severity:'info' with a threshold (100) that would
    // derive 'critical'.
    expect(alertSeverity(entry({ severity: 'info', threshold: 100 }))).toBe(
      'info',
    );
    // The middle tier passes through unchanged too.
    expect(alertSeverity(entry({ severity: 'warn', threshold: 0 }))).toBe(
      'warn',
    );
  });

  it('normalizes the legacy amber/red tokens from a stale backend', () => {
    // A pre-Phase-B server still emits 'amber'/'red'. Map them onto the
    // closest new tier so the rendered class is always a known tier.
    expect(
      alertSeverity(entry({ severity: 'amber' as never, threshold: 95 })),
    ).toBe('warn');
    expect(
      alertSeverity(entry({ severity: 'red' as never, threshold: 95 })),
    ).toBe('critical');
  });

  it('falls back to threshold bands when severity is absent', () => {
    // Byte-identical with the Python kernel `severity_for` bands:
    // info <90, warn 90-99, critical >=100.
    expect(alertSeverity(entry({ threshold: 89 }))).toBe('info');
    expect(alertSeverity(entry({ threshold: 90 }))).toBe('warn');
    expect(alertSeverity(entry({ threshold: 99 }))).toBe('warn');
    expect(alertSeverity(entry({ threshold: 100 }))).toBe('critical');
  });
});
