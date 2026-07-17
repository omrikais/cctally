// #294 S5 Task 7 — source-aware alert identity + presentation adapters (§6.7).
import { describe, expect, it } from 'vitest';
import {
  alertDisplay,
  collectToastAlertRows,
  seedFormsForRow,
  selectSourceAlertRows,
  toastAlertId,
} from './alertIdentity';
import { resolveSourceView } from '../store/sourceView';
import {
  makeClaudeSourceData,
  makeClaudeSourceEntry,
  makeCodexSourceEntry,
  makeAllSourceEntry,
  makeSourceEnvelope,
  type SourceEnvelopeSlice,
} from '../test-utils/sourceEnvelope';
import type { Envelope, SourceAlertRow } from '../types/envelope';

const claudeRow: SourceAlertRow = {
  source: 'claude',
  key: 'alert:claude:0:weekly:90',
  id: 'weekly:2026-04-13:90:0',
  axis: 'weekly',
  threshold: 90,
  crossed_at: '2026-04-16T12:00:00Z',
  alerted_at: '2026-04-16T12:00:00Z',
  context: { week_start_date: '2026-04-13' },
};

const codexBudgetRow: SourceAlertRow = {
  source: 'codex',
  key: 'alert:codex:codex_budget:calendar-month:90',
  axis: 'codex_budget',
  period: 'calendar-month',
  threshold: 90,
  value: 90.5,
  created_at: '2026-04-20T00:00:00Z',
};

const codexQuotaRow: SourceAlertRow = {
  source: 'codex',
  key: 'alert:codex:quota:root:limit:0:300:reset:90:t',
  axis: 'quota',
  threshold: 90,
  severity: 'warn',
  created_at: '2026-04-21T00:00:00Z',
};

function bundleWith(slice: Partial<SourceEnvelopeSlice>): Envelope {
  return makeSourceEnvelope(slice) as unknown as Envelope;
}

describe('toastAlertId (§6.7)', () => {
  it('normalizes Claude rows to claude:<id> (never the ordinal-unstable key)', () => {
    expect(toastAlertId(claudeRow)).toBe('claude:weekly:2026-04-13:90:0');
    // NOT the key (which embeds the row ordinal).
    expect(toastAlertId(claudeRow)).not.toContain(claudeRow.key);
  });
  it('normalizes Codex rows to codex:<key> (stable native identity)', () => {
    expect(toastAlertId(codexBudgetRow)).toBe(`codex:${codexBudgetRow.key}`);
    expect(toastAlertId(codexQuotaRow)).toBe(`codex:${codexQuotaRow.key}`);
  });
});

describe('seedFormsForRow — normalized + legacy bare form for continuity', () => {
  it('Claude seeds claude:<id> AND the bare legacy id', () => {
    expect(seedFormsForRow(claudeRow)).toEqual([
      'claude:weekly:2026-04-13:90:0',
      'weekly:2026-04-13:90:0',
    ]);
  });
  it('Codex seeds codex:<key> AND the bare key', () => {
    expect(seedFormsForRow(codexBudgetRow)).toEqual([
      `codex:${codexBudgetRow.key}`,
      codexBudgetRow.key,
    ]);
  });
});

describe('collectToastAlertRows — union of provider projections only', () => {
  it('unions sources.claude + sources.codex data.alerts.rows', () => {
    const claude = makeClaudeSourceEntry({
      data: { ...makeClaudeSourceData(), alerts: { rows: [claudeRow] as unknown as Record<string, unknown>[] } },
    });
    const codex = makeCodexSourceEntry();
    const env = bundleWith({
      sources: { claude, codex, all: makeAllSourceEntry(claude, codex) },
    });
    const rows = collectToastAlertRows(env);
    // Claude weekly + Codex codex_budget (from the codex fixture) — nothing else.
    expect(rows.map(toastAlertId)).toEqual([
      'claude:weekly:2026-04-13:90:0',
      'codex:alert:codex-budget-90',
    ]);
  });
  it('does NOT consume the legacy top-level alerts array (no double count)', () => {
    // A legacy top-level codex_budget row must never be fed to the toast pipeline
    // (that would double-toast alongside the source projection).
    const codex = makeCodexSourceEntry();
    const env = bundleWith({
      sources: { claude: makeClaudeSourceEntry(), codex, all: makeAllSourceEntry() },
    });
    (env as unknown as { alerts: unknown[] }).alerts = [
      { id: 'legacy', axis: 'codex_budget', threshold: 90, context: {} },
    ];
    const rows = collectToastAlertRows(env);
    expect(rows.map((r) => toastAlertId(r))).toEqual(['codex:alert:codex-budget-90']);
  });
  it('returns [] when the envelope has no sources bundle', () => {
    expect(collectToastAlertRows({} as unknown as Envelope)).toEqual([]);
    expect(collectToastAlertRows(null)).toEqual([]);
  });
});

describe('selectSourceAlertRows — active-source projection for the panel', () => {
  it('resolves the active source entry alerts rows', () => {
    const claude = makeClaudeSourceEntry({
      data: { ...makeClaudeSourceData(), alerts: { rows: [claudeRow] as unknown as Record<string, unknown>[] } },
    });
    const codex = makeCodexSourceEntry();
    const all = makeAllSourceEntry(claude, codex);
    const env = bundleWith({ sources: { claude, codex, all } });
    expect(selectSourceAlertRows(resolveSourceView(env, 'claude')).map(toastAlertId)).toEqual([
      'claude:weekly:2026-04-13:90:0',
    ]);
    expect(selectSourceAlertRows(resolveSourceView(env, 'codex')).map(toastAlertId)).toEqual([
      'codex:alert:codex-budget-90',
    ]);
    // All = the server-built union (both providers present, order is the
    // server's created_at-desc; Claude legacy-field rows carry no created_at so
    // they sort last). Assert set membership, not order.
    const allSources = selectSourceAlertRows(resolveSourceView(env, 'all')).map((r) => r.source);
    expect(allSources).toContain('claude');
    expect(allSources).toContain('codex');
    expect(allSources).toHaveLength(2);
  });
});

describe('alertDisplay — presentation adapter', () => {
  it('Claude row keeps the legacy axis chip + severity + alerted_at', () => {
    const d = alertDisplay(claudeRow);
    expect(d.source).toBe('claude');
    expect(d.sourceLabel).toBe('Claude');
    expect(d.chipClass).toBe('chip--weekly');
    expect(d.chipLabel).toBe('WEEKLY');
    expect(d.severity).toBe('warn'); // threshold 90 → warn
    expect(d.whenIso).toBe('2026-04-16T12:00:00Z');
  });
  it('Codex budget row → CODEX chip, threshold-derived severity, created_at', () => {
    const d = alertDisplay(codexBudgetRow);
    expect(d.source).toBe('codex');
    expect(d.sourceLabel).toBe('Codex');
    expect(d.chipClass).toBe('chip--codex_budget');
    expect(d.chipLabel).toBe('CODEX');
    expect(d.severity).toBe('warn');
    expect(d.whenIso).toBe('2026-04-20T00:00:00Z');
  });
  it('Codex quota row → native QUOTA chip label, severity from the row', () => {
    const d = alertDisplay(codexQuotaRow);
    expect(d.chipClass).toBe('chip--quota');
    expect(d.chipLabel).toBe('QUOTA');
    expect(d.severity).toBe('warn');
  });
});
