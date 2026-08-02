// #443 S2 — the two client-side halves of the Codex cache vocabulary
// contract: the nullable percent accessor (§4.3) and the source-keyed label
// map (§4.4). Both exist so no surface reaches for `cache_hit_percent` or a
// Claude label directly.
import { describe, expect, it } from 'vitest';
import {
  cachePercent,
  cachePercentText,
  cacheVocabulary,
  notApplicableReason,
} from './cacheReportVocabulary';

describe('cachePercent', () => {
  it('prefers the Codex authoritative key', () => {
    expect(cachePercent({ cached_input_percent: 61, cache_hit_percent: 61 })).toBe(61);
  });

  it('falls back to the legacy key for a pre-S2 server', () => {
    expect(cachePercent({ cache_hit_percent: 42 })).toBe(42);
  });

  it('returns null when neither key is present', () => {
    expect(cachePercent({})).toBeNull();
  });

  it('does not treat 0 as missing', () => {
    // `||` here would erase a genuinely measured zero-reuse day, which is
    // exactly the fabricated-versus-measured distinction this session exists
    // to keep.
    expect(cachePercent({ cache_hit_percent: 0 })).toBe(0);
    expect(cachePercent({ cached_input_percent: 0, cache_hit_percent: 71 })).toBe(0);
  });

  it('treats an explicit null as absent rather than as a value', () => {
    expect(cachePercent({ cached_input_percent: null, cache_hit_percent: 12 })).toBe(12);
    expect(cachePercent({ cached_input_percent: null, cache_hit_percent: null })).toBeNull();
  });
});

describe('cacheVocabulary', () => {
  it('labels Codex as cached input', () => {
    expect(cacheVocabulary('codex').percentLabel).toBe('Cached input');
  });

  it('leaves Claude unchanged', () => {
    expect(cacheVocabulary('claude').percentLabel).toBe('Cache hit');
  });

  it('renames every percent-bearing label under Codex', () => {
    const codex = cacheVocabulary('codex');
    const claude = cacheVocabulary('claude');
    for (const key of [
      'percentLabel', 'percentColumnHeader', 'timelineHeading',
      'sparklineLabel', 'netBarsHeading', 'netBarsCaption',
      'counterfactualLead', 'efficiencyLabel',
    ] as const) {
      expect(codex[key], `${key} still reads as Claude copy`).not.toBe(claude[key]);
      expect(codex[key].toLowerCase()).not.toMatch(/cache hit|cache %/);
    }
  });

  it('words the Codex reuse-drop predicate rather than printing its wire name', () => {
    expect(cacheVocabulary('codex').reasonLabel('cache_drop')).toBe('reuse drop');
    // Claude keeps the raw predicate name it ships today.
    expect(cacheVocabulary('claude').reasonLabel('cache_drop')).toBe('cache_drop');
    expect(cacheVocabulary('claude').reasonLabel('net_negative')).toBe('net_negative');
  });

  it('drops the net < 0 legend on Codex, where the predicate is not applicable', () => {
    expect(cacheVocabulary('claude').thresholdLegend(15)).toBe('15pp drop, net < 0');
    const codex = cacheVocabulary('codex').thresholdLegend(15);
    expect(codex).toContain('15pp');
    expect(codex).not.toContain('net < 0');
  });

  it('resolves the all-mode selection to the neutral Claude vocabulary', () => {
    // The all-mode composition passes each SECTION's own source, so `all`
    // never selects a provider's copy; pinning it keeps the fallback total.
    expect(cacheVocabulary('all')).toBe(cacheVocabulary('claude'));
  });

  it('returns a frozen record so no site can mutate the shared copy', () => {
    expect(Object.isFrozen(cacheVocabulary('codex'))).toBe(true);
  });
});

describe('notApplicableReason', () => {
  it('returns the wire reason for a field the provider marks not applicable', () => {
    const reason = notApplicableReason(
      { not_applicable: { wasted_usd: 'no cache-write premium' } },
      'wasted_usd',
    );
    expect(reason).toBe('no cache-write premium');
  });

  it('returns null when the map is absent, which is the Claude meaning', () => {
    expect(notApplicableReason({}, 'wasted_usd')).toBeNull();
  });

  it('returns null for a field the map does not name', () => {
    expect(notApplicableReason(
      { not_applicable: { wasted_usd: 'r' } },
      'fourteen_day_efficiency_ratio',
    )).toBeNull();
  });
});

// Review P3-2: `cachePercentText` and `percentLabelInline` were added to this
// module during Task 12-14 but never covered HERE — only indirectly, through
// component tests. The rendering twin is the chokepoint that keeps a missing
// percent from reaching a template literal, so it earns direct coverage.
describe('cachePercentText', () => {
  it('renders a measured value, including a genuine zero', () => {
    expect(cachePercentText({ cached_input_percent: 87.4 })).toBe('87%');
    expect(cachePercentText({ cached_input_percent: 0 })).toBe('0%');
  });

  it('returns null — never "0%" or "NaN%" — when nothing was measured', () => {
    // The whole point: a template literal on a missing value produced
    // `NaN%`, and a `?? 0` produced a confident `0%`. Both are lies.
    expect(cachePercentText({})).toBeNull();
    expect(cachePercentText({ cached_input_percent: null })).toBeNull();
    expect(cachePercentText({ cache_hit_percent: null })).toBeNull();
  });

  it('prefers the authoritative key and falls back to the transitional one', () => {
    expect(cachePercentText({ cache_hit_percent: 40 })).toBe('40%');
    expect(cachePercentText({
      cached_input_percent: 61, cache_hit_percent: 61,
    })).toBe('61%');
  });

  it('floors rather than rounds, matching fmt.pctFloor', () => {
    expect(cachePercentText({ cached_input_percent: 87.9 })).toBe('87%');
  });
});

describe('percentLabelInline', () => {
  it('is lower-case for mid-sentence use on both providers', () => {
    expect(cacheVocabulary('claude').percentLabelInline).toBe('cache hit');
    expect(cacheVocabulary('codex').percentLabelInline).toBe('cached input');
  });

  it('never carries Claude cache-hit wording on Codex', () => {
    expect(cacheVocabulary('codex').percentLabelInline).not.toMatch(/cache hit/i);
  });
});
