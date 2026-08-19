// #620 S1 D5 (F10) / A5 — absent signal is typed, not zero.
//
// When no usage has been observed this week the server now emits
// `dollars_per_percent: null` beside `dollars_per_percent_source:
// "no_usage_observed"`. The modal used to render the null as a bare dash and
// say nothing about why, which reads the same as a transport failure. It must
// render the explanation the code names, and — because the in-place update path
// lets an old client meet a newer server — an unrecognised code must take a
// required fallback branch rather than rendering nothing.
import { describe, it, expect, beforeEach } from 'vitest';
import { render } from '@testing-library/react';
import { ForecastModal } from '../src/modals/ForecastModal';
import { updateSnapshot, _resetForTests } from '../src/store/store';
import { dollarsPerPercentReason } from '../src/lib/withheldCopy';
import fixture from './fixtures/envelope.json';
import type { Envelope } from '../src/types/envelope';

/** The fixture with its forecast `explain` block replaced wholesale. Typed
 *  loosely because `ForecastEnvelope.explain` is declared `unknown` on the
 *  wire — the modal is the one place that narrows it. */
function envWithExplain(explain: Record<string, unknown>): Envelope {
  const env = JSON.parse(JSON.stringify(fixture)) as Record<string, unknown>;
  env.forecast = { ...(env.forecast as Record<string, unknown>), explain };
  return env as unknown as Envelope;
}

function envWithRateSource(source: string | null): Envelope {
  return envWithExplain({
    rates: {
      dollars_per_percent: null,
      week_average_pct_per_hour: 0.4,
      recent_24h_pct_per_hour: null,
      ...(source == null ? {} : { dollars_per_percent_source: source }),
    },
    week: { elapsed_hours: 30, remaining_hours: 138 },
  });
}

beforeEach(() => {
  _resetForTests();
});

describe('#620 S1 D5 — the forecast modal explains an absent $/1%', () => {
  it('renders a reason, not a zero, for no_usage_observed', () => {
    updateSnapshot(envWithRateSource('no_usage_observed'));
    const { container } = render(<ForecastModal />);

    const value = document.getElementById('mfc-dpp');
    // Precondition, asserted unconditionally: the value itself is withheld.
    expect(value?.textContent).not.toContain('0.00');
    expect(value?.classList.contains('m-unavailable')).toBe(true);

    const note = container.querySelector('.mfc-rate-note');
    expect(note).not.toBeNull();
    expect(note?.textContent).toBe(dollarsPerPercentReason('no_usage_observed'));
    // The specific explanation, not the generic fallback.
    expect(note?.textContent).toMatch(/no quota usage/i);
    expect(note?.textContent).not.toContain('no_usage_observed');
  });

  it('an unrecognised source code takes the fallback branch and still says something', () => {
    updateSnapshot(envWithRateSource('a_code_from_a_newer_server'));
    const { container } = render(<ForecastModal />);

    const note = container.querySelector('.mfc-rate-note');
    expect(note).not.toBeNull();
    expect(note?.textContent).toBe(dollarsPerPercentReason('a_code_from_a_newer_server'));
    // The fallback carries the code so a bug report can name it — the same
    // shape `withheldMessage` uses for the same reason.
    expect(note?.textContent).toContain('a_code_from_a_newer_server');
  });

  it('an older server that sends no source code still explains the absence', () => {
    updateSnapshot(envWithRateSource(null));
    const { container } = render(<ForecastModal />);

    const note = container.querySelector('.mfc-rate-note');
    expect(note).not.toBeNull();
    expect(note?.textContent).toBe(dollarsPerPercentReason(null));
    expect(note?.textContent).not.toBe('');
  });

  it('a measured rate renders the number and no reason line', () => {
    // The committed fixture carries `explain: null`, so the measured case has
    // to be pinned here rather than inherited — an inherited null rate would
    // make this test assert the withheld branch under a name that says the
    // opposite.
    updateSnapshot(envWithExplain({
      rates: {
        dollars_per_percent: 0.42,
        dollars_per_percent_source: 'this_week',
        week_average_pct_per_hour: 0.4,
        recent_24h_pct_per_hour: null,
      },
      week: { elapsed_hours: 30, remaining_hours: 138 },
    }));
    const { container } = render(<ForecastModal />);

    expect(document.getElementById('mfc-dpp')?.textContent).toContain('0.42');
    expect(document.getElementById('mfc-dpp')?.classList.contains('m-unavailable')).toBe(false);
    expect(container.querySelector('.mfc-rate-note')).toBeNull();
  });
});

describe('#620 S1 D5 — dollarsPerPercentReason', () => {
  // `no_usage_observed` is the only code the server pairs with a withheld
  // value today, so it is the only one with copy of its own. Every other code
  // — including a future one that starts withholding — reaches the fallback.
  it('explains the one code that withholds, in words rather than the token', () => {
    const text = dollarsPerPercentReason('no_usage_observed');
    expect(text).not.toBe('');
    expect(text).not.toContain('no_usage_observed');
  });

  it('carries an unknown code verbatim in the fallback', () => {
    expect(dollarsPerPercentReason('brand_new')).toContain('brand_new');
  });

  it('says something when no code was sent at all', () => {
    expect(dollarsPerPercentReason(null)).not.toBe('');
    expect(dollarsPerPercentReason(undefined)).toBe(dollarsPerPercentReason(null));
  });
});
