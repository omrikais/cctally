import { describe, expect, it } from 'vitest';
import { metricsFromOutline } from './comparisonMetricsCalc';
import type { ConversationOutline } from '../types/conversation';

const outline = (
  over: Partial<ConversationOutline['stats']>,
  files = 0,
): ConversationOutline => ({
  session_id: 's',
  stats: {
    turns: { total: 0, human: 99, assistant: 0, tool_result: 0, meta: 0 },
    tool_counts: {},
    error_count: 3,
    models: { sonnet: 1 },
    duration_seconds: 600,
    tokens: { input: 10, output: 20, cache_creation: 30, cache_read: 40 },
    cost_usd: 0.42,
    cache_saved_usd: 0,
    ...over,
  } as ConversationOutline['stats'],
  files: Array.from({ length: files }, (_, i) => ({ path: `f${i}`, add: 1, del: 0, touches: [] })),
  turns: [],
});

describe('metricsFromOutline', () => {
  it('tokens is the SUM of the token object, prompts is the spine length, files is files[].length', () => {
    const m = metricsFromOutline(outline({}, 9), /* promptSpineLength */ 7);
    expect(m.tokens).toBe(100); // 10+20+30+40, NOT stats.turns.human
    expect(m.prompts).toBe(7); // spine length, NOT stats.turns.human (99)
    expect(m.cost).toBeCloseTo(0.42);
    expect(m.errors).toBe(3);
    expect(m.durationSeconds).toBe(600);
    expect(m.files).toBe(9);
  });

  it('null duration is preserved', () => {
    const m = metricsFromOutline(outline({ duration_seconds: null }), 0);
    expect(m.durationSeconds).toBeNull();
  });

  it('a missing files[] array (older server) counts as 0', () => {
    const o = outline({});
    delete o.files;
    const m = metricsFromOutline(o, 3);
    expect(m.files).toBe(0);
  });
});
