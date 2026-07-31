import { describe, expect, it } from 'vitest';
import {
  cacheReportVerdict, cacheRowFlagClass, cacheRowFlagGlyph, cacheRowFlagLabel,
  cacheRowVerdict,
} from './cacheReportVerdict';
import type { CacheVerdictInput } from './cacheReportVerdict';

const row = (over: Partial<CacheVerdictInput> = {}): CacheVerdictInput => ({
  anomaly_triggered: false,
  anomaly_reasons: [],
  ...over,
});

describe('cacheRowVerdict', () => {
  it('resolves a fully evaluated clean row to clean', () => {
    expect(cacheRowVerdict(row({ anomaly_unevaluated: [] })).state).toBe('clean');
  });

  it('treats an absent anomaly_unevaluated as fully evaluated', () => {
    expect(cacheRowVerdict(row()).state).toBe('clean');
  });

  it('treats an absent observed as observed', () => {
    expect(cacheRowVerdict(row()).observed).toBe(true);
  });

  it('resolves a row with no predicate run to unevaluated', () => {
    expect(cacheRowVerdict(row({
      anomaly_unevaluated: ['net_negative', 'cache_drop'],
    })).state).toBe('unevaluated');
  });

  it('resolves a partially evaluated clean row to partial', () => {
    expect(cacheRowVerdict(row({
      anomaly_unevaluated: ['cache_drop'],
    })).state).toBe('partial');
  });

  it('keeps a thin-baseline net_negative row a verdict', () => {
    // The preserved epic rule: triggered wins before any unevaluated check.
    expect(cacheRowVerdict(row({
      anomaly_triggered: true,
      anomaly_reasons: ['net_negative'],
      anomaly_unevaluated: ['cache_drop'],
    })).state).toBe('anomalous');
  });

  it('resolves triggered-and-unobserved to anomalous rather than throwing', () => {
    // Not producible by the current Claude builder, but the helper accepts
    // observed and anomaly_triggered independently, so totality matters.
    expect(cacheRowVerdict(row({
      anomaly_triggered: true,
      anomaly_reasons: ['net_negative'],
      observed: false,
    })).state).toBe('anomalous');
  });
});

describe('cacheRowFlagGlyph', () => {
  it('maps the four states onto three glyphs', () => {
    expect(cacheRowFlagGlyph('anomalous')).toBe('⚠');
    expect(cacheRowFlagGlyph('clean')).toBe('✓');
    expect(cacheRowFlagGlyph('partial')).toBe('·');
    expect(cacheRowFlagGlyph('unevaluated')).toBe('·');
  });
});

describe('cacheRowFlagClass', () => {
  it('maps the four states onto three classes', () => {
    // Lives beside the glyph map so a fifth state is one edit, not two.
    expect(cacheRowFlagClass('anomalous')).toBe('flag-warn');
    expect(cacheRowFlagClass('clean')).toBe('flag-ok');
    expect(cacheRowFlagClass('partial')).toBe('flag-none');
    expect(cacheRowFlagClass('unevaluated')).toBe('flag-none');
  });
});

describe('cacheRowFlagLabel', () => {
  // partial and unevaluated share a glyph, so the accessible text is the ONLY
  // thing that distinguishes them. Without these the label could rot as dead
  // code while every glyph assertion still passed.
  it('names the predicate that did not run for a partial row', () => {
    expect(cacheRowFlagLabel('partial', ['cache_drop'])).toContain('cache_drop');
  });

  it('names both predicates for a fully unevaluated observed row', () => {
    const label = cacheRowFlagLabel(
      'unevaluated', ['net_negative', 'cache_drop'], true,
    );
    expect(label).toContain('net_negative');
    expect(label).toContain('cache_drop');
  });

  it('says nothing was measured when the row is unobserved', () => {
    expect(cacheRowFlagLabel('unevaluated', ['net_negative', 'cache_drop'], false))
      .toBe('no activity — nothing measured to evaluate');
  });

  it('distinguishes anomalous from clean', () => {
    expect(cacheRowFlagLabel('anomalous', [])).toBe('anomaly');
    expect(cacheRowFlagLabel('clean', [])).toBe('evaluated, no anomaly');
  });
});

describe('cacheReportVerdict', () => {
  const report = (over = {}) => ({
    today: {
      baseline_daily_row_count: 9, anomaly_triggered: false,
      anomaly_reasons: [], ...over,
    },
  } as never);

  it('flags insufficient below the baseline floor', () => {
    expect(cacheReportVerdict(report({ baseline_daily_row_count: 3 })).insufficient).toBe(true);
  });

  it('suppresses chromeAmber while the baseline is still building', () => {
    const v = cacheReportVerdict(report({
      baseline_daily_row_count: 3, anomaly_triggered: true,
      anomaly_reasons: ['net_negative'],
    }));
    expect(v.insufficient).toBe(true);
    expect(v.chromeAmber).toBe(false);
  });

  it('raises chromeAmber once the baseline exists', () => {
    expect(cacheReportVerdict(report({
      anomaly_triggered: true, anomaly_reasons: ['cache_drop'],
    })).chromeAmber).toBe(true);
  });

  it('reports todayObserved from the spotlight', () => {
    expect(cacheReportVerdict(report({ observed: false })).todayObserved).toBe(false);
  });

  it('defaults todayObserved to true when the field is absent', () => {
    expect(cacheReportVerdict(report()).todayObserved).toBe(true);
  });
});
