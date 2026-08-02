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

// #443 S2 — the applicable predicate set is a PARAMETER, because "every
// predicate unevaluated" is a per-provider question. Codex can only ever
// evaluate cache_drop, so a Codex row carrying it alone is fully unevaluated,
// not half-evaluated; resolving it against the Claude pair pinned the Codex
// Flag column at `partial` in perpetuity (spec §2, §4.6).
describe('cacheRowVerdict provider-scoped predicates', () => {
  it('resolves an evaluable Codex row as clean, not partial', () => {
    const v = cacheRowVerdict(
      row({ anomaly_unevaluated: [] }),
      ['cache_drop'],
    );
    expect(v.state).toBe('clean');
  });

  it('resolves a thin-baseline Codex row as unevaluated, not partial', () => {
    const v = cacheRowVerdict(
      row({ anomaly_unevaluated: ['cache_drop'] }),
      ['cache_drop'],
    );
    expect(v.state).toBe('unevaluated');
  });

  it('defaults to the Claude pair when no set is given', () => {
    // The S1 compatibility seam: a pre-S2 envelope publishes no
    // anomaly_predicates and must resolve exactly as it does today.
    const v = cacheRowVerdict(row({ anomaly_unevaluated: ['cache_drop'] }));
    expect(v.state).toBe('partial');
  });

  it('keeps a triggered Codex row anomalous ahead of the unevaluated check', () => {
    expect(cacheRowVerdict(
      row({
        anomaly_triggered: true,
        anomaly_reasons: ['cache_drop'],
        anomaly_unevaluated: [],
      }),
      ['cache_drop'],
    ).state).toBe('anomalous');
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

  // The one that matters (#443 S2 §4.6): threading the set into the leaf
  // helper alone leaves this function deriving today's verdict against the
  // Claude pair, and the session's headline behaviour silently does not land.
  it('passes the report predicate set through cacheReportVerdict', () => {
    const cr = {
      anomaly_predicates: ['cache_drop'],
      today: {
        baseline_daily_row_count: 9, anomaly_triggered: false,
        anomaly_reasons: [], anomaly_unevaluated: ['cache_drop'],
      },
    } as never;
    expect(cacheReportVerdict(cr).today.state).toBe('unevaluated');
  });

  it('still resolves an absent predicate set against the Claude pair', () => {
    expect(cacheReportVerdict(report({
      anomaly_unevaluated: ['cache_drop'],
    })).today.state).toBe('partial');
  });
});

// #443 S2 QA P2. The earlier review concluded the predicate threading was
// structurally correct but behaviourally unobservable, because `partial` and
// `unevaluated` collapse to one class and one glyph. That is true of the
// class and the glyph — but NOT of the label, which names the predicates and
// is rendered into both `title` and `aria-label`. So the observable test spec
// §5 asked for does exist; it just lives here rather than on the glyph.
describe('cacheRowFlagLabel provider vocabulary', () => {
  const codexReason = (p: string) => (p === 'cache_drop' ? 'reuse drop' : p);

  it('renders the raw predicate name for Claude, unchanged', () => {
    expect(cacheRowFlagLabel('partial', ['cache_drop'])).toBe(
      'not evaluated — cache_drop could not be evaluated for this day');
  });

  it('renders the Codex wording, never the raw identifier', () => {
    const label = cacheRowFlagLabel('partial', ['cache_drop'], true, codexReason);
    expect(label).toContain('reuse drop');
    // The point of the finding: this string reaches a screen reader.
    expect(label).not.toContain('cache_drop');
  });

  it('translates every predicate in a multi-predicate label', () => {
    const label = cacheRowFlagLabel(
      'unevaluated', ['net_negative', 'cache_drop'], true, codexReason);
    expect(label).toContain('reuse drop');
    expect(label).not.toContain('cache_drop');
    // net_negative has no Codex translation and passes through untouched.
    expect(label).toContain('net_negative');
  });

  it('is byte-identical with an identity translator and with none', () => {
    expect(cacheRowFlagLabel('partial', ['cache_drop'], true, (p) => p))
      .toBe(cacheRowFlagLabel('partial', ['cache_drop']));
  });
});
