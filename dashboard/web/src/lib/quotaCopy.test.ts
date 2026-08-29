// #661 S2 section 8 — the client half of the withheld-figure vocabulary, and
// specifically the compatibility direction the spec names: a NEW client
// meeting an OLD server that sends no presentation fields at all.
import { describe, expect, it } from 'vitest';
import {
  causeLong,
  causeShort,
  deriveShortFromCode,
  rateChangeSummary,
} from './quotaCopy';

describe('deriveShortFromCode', () => {
  it('is the same rule the server uses: the hyphens become spaces', () => {
    expect(deriveShortFromCode('insufficient-history')).toBe('insufficient history');
    expect(deriveShortFromCode('right-censored')).toBe('right censored');
    expect(deriveShortFromCode('stale')).toBe('stale');
  });

  it('never returns a blank for a non-empty code, including an unknown one', () => {
    // The property the degraded path depends on. A code from a NEWER server
    // than this client is exactly as renderable as a known one, because the
    // rule is over the string and not over a table.
    expect(deriveShortFromCode('a-code-from-the-future')).toBe('a code from the future');
  });

  it('renders nothing for an absent cause', () => {
    expect(deriveShortFromCode(null)).toBe('');
    expect(deriveShortFromCode(undefined)).toBe('');
    expect(deriveShortFromCode('   ')).toBe('');
  });
});

describe('causeShort / causeLong — both compatibility directions', () => {
  const wire = {
    code: 'unstable-fit',
    short: 'unstable fit',
    long: 'the fit did not settle on one rate',
  };

  it('prefers the server rendering when the server sent one', () => {
    expect(causeShort(wire, 'unstable-fit')).toBe('unstable fit');
    expect(causeLong(wire, 'unstable-fit')).toBe('the fit did not settle on one rate');
  });

  it('OLD server, NEW client: no presentation object at all', () => {
    // Degraded to no SENTENCE, never to a blank — the long register falls
    // back to the derived token rather than rendering nothing.
    expect(causeShort(null, 'unstable-fit')).toBe('unstable fit');
    expect(causeLong(null, 'unstable-fit')).toBe('unstable fit');
    expect(causeShort(undefined, 'unstable-fit')).toBe('unstable fit');
  });

  it('NEW server, OLD client: a code this build has never heard of', () => {
    // The client enumerates no causes, so an unheard-of code carries its own
    // server-supplied copy through unchanged.
    const future = { code: 'novel-cause', short: 'novel cause', long: 'a new sentence' };
    expect(causeShort(future, 'novel-cause')).toBe('novel cause');
    expect(causeLong(future, 'novel-cause')).toBe('a new sentence');
  });

  it('a partial presentation object falls back field by field', () => {
    expect(causeLong({ code: 'x', short: 'x', long: '' }, 'stale')).toBe('stale');
  });

  it('renders nothing for an absent cause', () => {
    expect(causeShort(null, null)).toBe('');
    expect(causeLong(null, null)).toBe('');
  });
});

describe('rateChangeSummary', () => {
  it('states the direction and size, with a HIGHER rate being more generous', () => {
    // 2,442,620 -> 1,685,000 units per point: one point now buys 31% less
    // work, so the meter moves faster for the same usage.
    expect(rateChangeSummary(2_442_620, 1_685_000))
      .toBe('each meter point now covers 31% less usage');
    expect(rateChangeSummary(1_685_000, 2_442_620))
      .toBe('each meter point now covers 45% more usage');
  });

  it('returns null rather than a percentage from an absent operand', () => {
    expect(rateChangeSummary(null, 1_685_000)).toBeNull();
    expect(rateChangeSummary(2_442_620, null)).toBeNull();
    expect(rateChangeSummary(undefined, undefined)).toBeNull();
  });

  it('returns null for a non-positive or non-finite rate', () => {
    // A zero previous rate would divide by zero and a non-finite one would
    // render `NaN%`, which reads as a measurement.
    expect(rateChangeSummary(0, 1_685_000)).toBeNull();
    expect(rateChangeSummary(2_442_620, 0)).toBeNull();
    expect(rateChangeSummary(Number.NaN, 1_685_000)).toBeNull();
    expect(rateChangeSummary(Number.POSITIVE_INFINITY, 1_685_000)).toBeNull();
  });
});
