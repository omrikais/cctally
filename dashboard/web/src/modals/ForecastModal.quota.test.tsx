// #661 S2 — the Forecast modal's quota section (section 10) and the qualified
// `$ / 1%` note (Stage C review, F11).
import { render } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { QuotaSection } from './ForecastModal';
import { _resetForTests } from '../store/store';
import { dollarsPerPercentQualification } from '../lib/withheldCopy';
import type { Envelope, ForecastQuotaEnvelope } from '../types/envelope';

function quota(over: Partial<ForecastQuotaEnvelope> = {}): ForecastQuotaEnvelope {
  return {
    basis: 'calibrated',
    basis_presentation: {
      code: 'calibrated', short: 'model', long: 'calibrated model',
    },
    projection_pct: 61,
    right_censored: false,
    code: null,
    code_presentation: null,
    calibration_code: null,
    calibration_code_presentation: null,
    corrected_interval: { lo: 39, hi: 40 },
    calibrated_consumption_pct: 34,
    calibrated_consumption_interval: { lo: 32, hi: 36 },
    calibrated_headroom_pct: 66,
    rate_change: null,
    observed_minus_modelled_pct: 6,
    ...over,
  };
}

function envWith(q: ForecastQuotaEnvelope | null): Envelope {
  return {
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    forecast: q === null ? null : { quota: q },
  } as unknown as Envelope;
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('the quota section', () => {
  it('renders nothing when the server publishes no quota object', () => {
    const { container } = render(<QuotaSection env={envWith(null)} />);
    expect(container.querySelector('[data-testid="mfc-quota"]')).toBeNull();
  });

  it('states the basis, the modelled consumption and the headroom', () => {
    const { container } = render(<QuotaSection env={envWith(quota())} />);
    const grid = container.querySelector('[data-testid="mfc-quota"]');
    expect(grid?.textContent).toContain('calibrated model');
    expect(grid?.textContent).toContain('34.0%');
    expect(grid?.textContent).toContain('66.0%');
  });

  it('uses the server long register instead of client-owned modal copy', () => {
    const q = quota();
    Object.assign(q, {
      basis_presentation: {
        code: 'calibrated', short: 'server model', long: 'server calibrated model',
      },
    });
    const { container } = render(<QuotaSection env={envWith(q)} />);
    expect(container.querySelector('#mfc-quota-basis')?.textContent).toBe(
      'server calibrated model',
    );
  });

  it('states what the meter reading covers, unbounded above when censored', () => {
    const bounded = render(<QuotaSection env={envWith(quota())} />);
    expect(
      bounded.container.querySelector('[data-testid="mfc-quota"]')?.textContent,
    ).toContain('39.0% – 40.0%');
    const censored = render(<QuotaSection env={envWith(quota({
      corrected_interval: { lo: 99, hi: null },
    }))} />);
    expect(
      censored.container.querySelector('[data-testid="mfc-quota"]')?.textContent,
    ).toContain('99.0% or more');
  });

  it('carries the two cause fields SEPARATELY', () => {
    // `code` is why a projection was WITHHELD; `calibration_code` is why the
    // calibrated basis was not reached. Falling back to the meter is not a
    // withholding, so one line cannot answer both questions.
    const { container } = render(<QuotaSection env={envWith(quota({
      basis: 'corrected-meter',
      calibration_code: 'unstable-fit',
      calibration_code_presentation: {
        code: 'unstable-fit', short: 'unstable fit',
        long: 'the fit did not settle on one rate',
      },
    }))} />);
    expect(container.querySelector('[data-testid="mfc-quota-code"]')).toBeNull();
    expect(
      container.querySelector('[data-testid="mfc-quota-calibration-code"]')?.textContent,
    ).toContain('the fit did not settle on one rate');
  });

  it('falls back to the derived token when the server sends no sentence', () => {
    const { container } = render(<QuotaSection env={envWith(quota({
      basis: 'withheld', projection_pct: null,
      code: 'right-censored', code_presentation: null,
    }))} />);
    expect(
      container.querySelector('[data-testid="mfc-quota-code"]')?.textContent,
    ).toContain('right censored');
  });

  it('states the residual as a difference and asserts no direction', () => {
    const positive = render(<QuotaSection env={envWith(quota())} />);
    const text = positive.container
      .querySelector('[data-testid="mfc-quota-residual"]')?.textContent ?? '';
    expect(text).toContain('Observed meter minus modelled local quota');
    expect(text).toContain('+6.00');
    expect(text).toContain('not an identification or an estimate of usage from another machine');
    for (const forbidden of ['more than', 'less than', 'unaccounted', 'missing']) {
      expect(text).not.toContain(forbidden);
    }
    const negative = render(<QuotaSection env={envWith(quota({
      observed_minus_modelled_pct: -6,
    }))} />);
    expect(
      negative.container.querySelector('[data-testid="mfc-quota-residual"]')?.textContent,
    ).toContain('-6.00');
  });

  it('withholds the residual when either side is absent', () => {
    const { container } = render(<QuotaSection env={envWith(quota({
      observed_minus_modelled_pct: null,
    }))} />);
    expect(container.querySelector('[data-testid="mfc-quota-residual"]')).toBeNull();
  });

  it('renders the rate-change sentence only while the predicate holds', () => {
    const active = render(<QuotaSection env={envWith(quota({
      rate_change: {
        active: true, effective_from: '2026-08-25T00:00:00+00:00',
        severity: 'alarm',
        previous_units_per_point: 2_442_620, new_units_per_point: 1_685_000,
      },
    }))} />);
    const note = active.container
      .querySelector('[data-testid="mfc-quota-rate-change"]')?.textContent ?? '';
    expect(note).toContain('31% less usage');
    expect(note).toContain('not a cctally malfunction');
    const inactive = render(<QuotaSection env={envWith(quota({
      rate_change: {
        active: false, effective_from: null, severity: null,
        previous_units_per_point: null, new_units_per_point: null,
      },
    }))} />);
    expect(
      inactive.container.querySelector('[data-testid="mfc-quota-rate-change"]'),
    ).toBeNull();
  });
});

describe('the qualified $ / 1% note (F11)', () => {
  it('states the qualification for each qualified source label', () => {
    expect(dollarsPerPercentQualification('trailing_4wk_median_drifted'))
      .toContain('reduced population');
    expect(dollarsPerPercentQualification('trailing_4wk_median_unverified'))
      .toContain('not checked against the current');
  });

  it('states NOTHING for an unqualified source', () => {
    // The non-vacuity twin: a note on every rate would be a reassurance, not
    // a qualification, and the plain trailing median is the common case.
    expect(dollarsPerPercentQualification('trailing_4wk_median')).toBeNull();
    expect(dollarsPerPercentQualification('this_week')).toBeNull();
    expect(dollarsPerPercentQualification(null)).toBeNull();
    expect(dollarsPerPercentQualification('a_code_from_the_future')).toBeNull();
  });
});
