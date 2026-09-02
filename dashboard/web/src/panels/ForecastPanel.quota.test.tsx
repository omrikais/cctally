// #661 S2 Task D4 — the Forecast panel's quota surface.
//
// Two additions, and both must be INVISIBLE on a server that publishes no
// `forecast.quota` object: every server predating this session, and every
// Codex projection. The panel keeps its committed shape there rather than
// growing an empty row and an empty chip, which is why each test below has a
// twin asserting the absence.
import { render } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { ForecastPanel, forecastBasisLine } from './ForecastPanel';
import { _resetForTests, updateSnapshot } from '../store/store';
import type {
  Envelope,
  ForecastEnvelope,
  ForecastQuotaEnvelope,
} from '../types/envelope';

function quota(over: Partial<ForecastQuotaEnvelope> = {}): ForecastQuotaEnvelope {
  return {
    basis: 'corrected-meter',
    basis_presentation: {
      code: 'corrected-meter', short: 'meter', long: 'corrected meter',
    },
    projection_pct: 88,
    right_censored: false,
    code: null,
    code_presentation: null,
    calibration_code: null,
    calibration_code_presentation: null,
    corrected_interval: { lo: 39, hi: 40 },
    calibrated_consumption_pct: null,
    calibrated_consumption_interval: null,
    calibrated_headroom_pct: null,
    rate_change: null,
    observed_minus_modelled_pct: null,
    ...over,
  };
}

function forecast(q: ForecastQuotaEnvelope | null): ForecastEnvelope {
  return {
    verdict: 'ok',
    week_avg_projection_pct: 88,
    recent_24h_projection_pct: 92,
    budget_100_per_day_usd: 4.2,
    budget_90_per_day_usd: 3.1,
    confidence: 'high',
    confidence_score: 3,
    explain: {},
    ...(q === null ? {} : { quota: q }),
  };
}

function env(q: ForecastQuotaEnvelope | null): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-06-30T10:00:00Z',
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: {
      week_label: 'wk Jun 30', used_pct: 11, five_hour_pct: 8,
      dollar_per_pct: 23.4, forecast_pct: 31, forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null, forecast: forecast(q), trend: null,
    weekly: { rows: [] }, monthly: { rows: [] }, blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [] },
  } as Envelope;
}

function renderWith(q: ForecastQuotaEnvelope | null) {
  _resetForTests();
  updateSnapshot(env(q));
  return render(<ForecastPanel />);
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('#661 S2 D4 — the basis line', () => {
  it('names the measurement the projection came from', () => {
    const { container } = renderWith(quota());
    const line = container.querySelector('[data-testid="fc-basis-line"]');
    expect(line?.textContent).toContain('Basis');
    expect(line?.textContent).toContain('meter');
  });

  it('says "model" when the calibrated basis was selected', () => {
    const { container } = renderWith(quota({
      basis: 'calibrated',
      basis_presentation: {
        code: 'calibrated', short: 'model', long: 'calibrated model',
      },
    }));
    expect(
      container.querySelector('[data-testid="fc-basis-line"]')?.textContent,
    ).toContain('model');
  });

  it('uses the server short register instead of a client-owned basis word', () => {
    const q = quota({ basis: 'calibrated' });
    Object.assign(q, {
      basis_presentation: {
        code: 'calibrated', short: 'server model', long: 'server calibrated model',
      },
    });
    const { container } = renderWith(q);
    const text = container
      .querySelector('[data-testid="fc-basis-line"]')?.textContent ?? '';
    expect(text).toContain('server model');
  });

  it('renders the withholding cause in the short register', () => {
    const { container } = renderWith(quota({
      basis: 'withheld',
      projection_pct: null,
      code: 'right-censored',
      code_presentation: {
        code: 'right-censored', short: 'right censored', long: 'the long one',
      },
    }));
    expect(
      container.querySelector('[data-testid="fc-basis-line"]')?.textContent,
    ).toContain('right censored');
  });

  it('falls back to the derived short token when the server sends no presentation fields', () => {
    // A NEW client against an OLD server: the `code` arrives and the
    // presentation object does not. The token is derived from the code by the
    // same rule the server uses, so it cannot disagree — only the sentence is
    // lost, and nothing renders blank.
    const { container } = renderWith(quota({
      basis: 'withheld', projection_pct: null,
      code: 'unsupported-model-mix', code_presentation: null,
    }));
    expect(
      container.querySelector('[data-testid="fc-basis-line"]')?.textContent,
    ).toContain('unsupported model mix');
  });

  it('renders NO basis row when the server publishes no quota object', () => {
    const { container } = renderWith(null);
    expect(container.querySelector('[data-testid="fc-basis-line"]')).toBeNull();
  });

  it('the pure selector returns null for an absent object and an absent basis', () => {
    expect(forecastBasisLine(null)).toBeNull();
    expect(forecastBasisLine(env(quota({ basis: null })))).toBeNull();
  });
});

describe('#661 S2 D4 — the rate-change chip (section 6.6)', () => {
  const active = {
    active: true,
    effective_from: '2026-08-25T00:00:00+00:00',
    severity: 'alarm' as const,
    previous_units_per_point: 2_442_620,
    new_units_per_point: 1_685_000,
  };

  it('shows while the marker predicate holds', () => {
    const { container } = renderWith(quota({ rate_change: active }));
    const chip = container.querySelector('[data-testid="fc-rate-change-chip"]');
    expect(chip).not.toBeNull();
    expect(chip?.className).toContain('severity-alarm');
  });

  it('does NOT show when the predicate is false', () => {
    const { container } = renderWith(quota({
      rate_change: { ...active, active: false },
    }));
    expect(
      container.querySelector('[data-testid="fc-rate-change-chip"]'),
    ).toBeNull();
  });

  it('does NOT show when the server publishes no quota object', () => {
    const { container } = renderWith(null);
    expect(
      container.querySelector('[data-testid="fc-rate-change-chip"]'),
    ).toBeNull();
  });
});

describe('#661 S2 D4 — the panel body owns no scroll of its own', () => {
  it('puts the foot and the budget block inside the bounded scroll region', () => {
    // The structural half of section 10.2's defect 1, which is what a JSDOM
    // test CAN assert: the hero and the pace bar are siblings of the scroll
    // region rather than inside it, so they stay pinned. Whether the
    // resulting geometry satisfies `scrollHeight <= clientHeight` at four
    // widths is a real-browser measurement and is NOT claimed here.
    const { container } = renderWith(quota());
    const body = container.querySelector('.panel-body.fc-body');
    const scroll = container.querySelector('.fc-scroll');
    expect(scroll).not.toBeNull();
    expect(scroll?.parentElement).toBe(body);
    expect(body?.querySelector(':scope > .fc-hero')).not.toBeNull();
    expect(body?.querySelector(':scope > .fc-pace')).not.toBeNull();
    expect(scroll?.querySelector('.fc-budget-foot')).not.toBeNull();
    expect(scroll?.querySelector('.fc-budget-block')).not.toBeNull();
  });
});

describe('#661 S2 remediation — the hero chips share one row', () => {
  const activeChange = {
    active: true,
    effective_from: '2026-08-25T00:00:00+00:00',
    severity: 'alarm' as const,
    previous_units_per_point: 2_442_620,
    new_units_per_point: 1_685_000,
  };

  it('puts the verdict chip and the rate chip in the same chip row', () => {
    // Finding C2. `.fc-hero` is a column flex, so a chip added as a plain
    // sibling stacks below the verdict chip: measured 45.9px apart on the
    // same x at 1440, 1280, 1100 and 960. JSDOM cannot measure that, so what
    // it asserts is the structure the CSS rule keys on — both chips inside
    // one `.fc-chiprow`. The geometry is the browser gate's.
    const { container } = renderWith(quota({ rate_change: activeChange }));
    const row = container.querySelector('.fc-chiprow');
    expect(row).not.toBeNull();
    expect(row?.parentElement?.className).toContain('fc-hero');
    expect(row?.querySelector('.fc-verdict-chip')).not.toBeNull();
    expect(
      row?.querySelector('[data-testid="fc-rate-change-chip"]'),
    ).not.toBeNull();
  });

  it('leaves no chip as a direct child of the column-flex hero', () => {
    // The discriminating twin. A chip row that existed while the chips also
    // stayed direct `.fc-hero` children would satisfy the test above and
    // still stack, because the stacking is caused by the parent being a
    // column flex, not by the wrapper being absent.
    const { container } = renderWith(quota({ rate_change: activeChange }));
    expect(container.querySelector('.fc-hero > .fc-verdict-chip')).toBeNull();
    expect(
      container.querySelector('.fc-hero > [data-testid="fc-rate-change-chip"]'),
    ).toBeNull();
  });

  it('renders no rate chip inside the row when the predicate is false', () => {
    // The row survives for the verdict chip; the rate chip does not appear
    // in it, so the wrapper is not emitting its children unconditionally.
    const { container } = renderWith(quota());
    const row = container.querySelector('.fc-chiprow');
    expect(row?.querySelector('.fc-verdict-chip')).not.toBeNull();
    expect(
      row?.querySelector('[data-testid="fc-rate-change-chip"]'),
    ).toBeNull();
  });
});
