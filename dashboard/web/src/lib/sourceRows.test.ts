import { describe, expect, it } from 'vitest';
import {
  adaptClaudeSessionRows,
  adaptCodexSessionRows,
  joinCodexQuotaLabels,
} from './sourceRows';
import { makeClaudeSourceData, makeCodexSourceData } from '../test-utils/sourceEnvelope';
import type { ClaudeSessionSourceRow } from '../types/envelope';

describe('joinCodexQuotaLabels (§6.1)', () => {
  it('attaches label + window_minutes to each active window by opaque key', () => {
    const data = makeCodexSourceData();
    const joined = joinCodexQuotaLabels(data.hero, data.quota);
    expect(joined).toHaveLength(2);
    const byKey = Object.fromEntries(joined.map((j) => [j.key, j]));
    expect(byKey['quota:codex-5h'].label).toBe('5-hour limit');
    expect(byKey['quota:codex-5h'].windowMinutes).toBe(300);
    expect(byKey['quota:codex-weekly'].label).toBe('Weekly limit');
    expect(byKey['quota:codex-5h'].current.current_percent).toBe(42.0);
  });

  it("falls back to the generic 'Codex quota' label when no history matches", () => {
    const data = makeCodexSourceData();
    // Drop the histories so the active rows have no match.
    const joined = joinCodexQuotaLabels(data.hero, { ...data.quota, histories: [] });
    expect(joined.every((j) => j.label === 'Codex quota')).toBe(true);
    expect(joined.every((j) => j.windowMinutes === null)).toBe(true);
  });

  it('joins only active rows (never block-namespace keys)', () => {
    const data = makeCodexSourceData();
    const joined = joinCodexQuotaLabels(data.hero, data.quota);
    // The block key (block:codex-5h) must NOT appear in the join output.
    expect(joined.some((j) => j.key.startsWith('block:'))).toBe(false);
    expect(joined.map((j) => j.key)).toEqual(['quota:codex-5h', 'quota:codex-weekly']);
  });
});

describe('session display-row adapters (§6.3)', () => {
  it('Claude rows preserve the legacy fields', () => {
    const data = makeClaudeSourceData();
    const rows = adaptClaudeSessionRows(data.sessions.rows as ClaudeSessionSourceRow[]);
    expect(rows).toHaveLength(1);
    expect(rows[0].source).toBe('claude');
    expect(rows[0].legacy).toBe(data.sessions.rows[0]);
    expect(rows[0].recencyUtc).toBe('2026-04-24T10:00:00Z'); // started_utc
    expect(rows[0].tokens.kind).toBe('claude');
  });

  it('Codex rows carry native vocabulary (label / last_activity / token cells)', () => {
    const data = makeCodexSourceData();
    const rows = adaptCodexSessionRows(data.sessions.rows);
    expect(rows).toHaveLength(2);
    const first = rows[0];
    expect(first.source).toBe('codex');
    expect(first.title).toBe('Session 1'); // label
    expect(first.recencyUtc).toBe('2026-04-24T12:30:00Z'); // last_activity
    expect(first.models).toEqual(['gpt-5']);
    expect(first.tokens.kind).toBe('codex');
    if (first.tokens.kind === 'codex') {
      expect(first.tokens.input).toBe(240000);
      expect(first.tokens.cachedInput).toBe(60000);
      expect(first.tokens.reasoning).toBe(4000);
      expect(first.tokens.total).toBe(276000);
    }
  });
});
