// #294 S5 Task 7 — source-aware alert identity + presentation adapters (§6.7).
import { describe, expect, it } from 'vitest';
import {
  alertDisplay,
  collectToastAlertRows,
  seedFormsForRow,
  selectAlertRowsForView,
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
  it('qualifies otherwise-identical crossings by account when decorated', () => {
    const work = {
      ...claudeRow,
      accountKey: 'a'.repeat(32),
      accountLabel: 'work',
    } as SourceAlertRow;
    const personal = {
      ...claudeRow,
      accountKey: 'b'.repeat(32),
      accountLabel: 'personal',
    } as SourceAlertRow;
    expect(toastAlertId(work)).toBe(`claude:${'a'.repeat(32)}:${claudeRow.id}`);
    expect(toastAlertId(personal)).toBe(`claude:${'b'.repeat(32)}:${claudeRow.id}`);
    expect(toastAlertId(work)).not.toBe(toastAlertId(personal));
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
  it('decorated rows seed account-qualified plus pre-decoration forms', () => {
    const row = {
      ...claudeRow,
      accountKey: 'a'.repeat(32),
      accountLabel: 'work',
    } as SourceAlertRow;
    expect(seedFormsForRow(row)).toEqual([
      `claude:${'a'.repeat(32)}:${claudeRow.id}`,
      `claude:${claudeRow.id}`,
      claudeRow.id,
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
    // All = the server-built union, ordered by the canonical FIRING instant
    // across both providers. #556 S3 §5.2: this used to assert set membership
    // and explicitly decline to check order, which is why no client test could
    // catch E1. The Claude row fired at 12:00Z on the 16th and the Codex row at
    // 00:00Z on the 20th, so Codex is genuinely first.
    const allRows = selectSourceAlertRows(resolveSourceView(env, 'all'));
    expect(allRows.map((r) => r.source)).toEqual(['codex', 'claude']);
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
  it('Codex budget row → BUDGET chip, threshold-derived severity, created_at', () => {
    const d = alertDisplay(codexBudgetRow);
    expect(d.source).toBe('codex');
    expect(d.sourceLabel).toBe('Codex');
    expect(d.chipClass).toBe('chip--codex_budget');
    // #556 S3 §4.2 — from AXIS_CHIP_LABEL, not from a hardcode beside it.
    expect(d.chipLabel).toBe('BUDGET');
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

// #556 S3 §2.8 — one accessor, so the instant a row is ORDERED by and the
// instant it PRINTS are the same value by construction rather than by two
// independent choices that happened to agree.
describe('alertDisplay — the firing instant is one accessor for both providers', () => {
  // The two values differ here ON PURPOSE. In a v7 payload they are equal, so
  // a test built from a real row cannot tell which field the accessor read —
  // it would pass whether or not the accessor changed at all.
  it('prefers alerted_at on a v7 Codex row', () => {
    const row: SourceAlertRow = {
      ...codexBudgetRow,
      alerted_at: '2026-04-20T00:05:00Z',
      created_at: '2026-04-19T00:00:00Z',
    } as SourceAlertRow;
    expect(alertDisplay(row).whenIso).toBe('2026-04-20T00:05:00Z');
  });

  it('prefers alerted_at on a v7 Codex quota row too', () => {
    const row: SourceAlertRow = {
      ...codexQuotaRow,
      alerted_at: '2026-04-21T00:07:00Z',
      created_at: '2026-04-19T00:00:00Z',
    } as SourceAlertRow;
    expect(alertDisplay(row).whenIso).toBe('2026-04-21T00:07:00Z');
  });

  it('falls back to created_at on a pre-v7 Codex row', () => {
    expect(alertDisplay(codexBudgetRow).whenIso).toBe('2026-04-20T00:00:00Z');
    expect(alertDisplay(codexQuotaRow).whenIso).toBe('2026-04-21T00:00:00Z');
  });

  it('orders by the same instant it prints, for both providers', () => {
    const rows: SourceAlertRow[] = [
      { ...codexBudgetRow, alerted_at: '2026-04-16T11:00:00Z' } as SourceAlertRow,
      claudeRow,
    ];
    const printed = rows.map((row) => alertDisplay(row).whenIso);
    const ordered = [...rows]
      .sort((a, b) => Date.parse(alertDisplay(b).whenIso ?? '') - Date.parse(alertDisplay(a).whenIso ?? ''))
      .map((row) => alertDisplay(row).whenIso);
    expect(printed).toEqual(['2026-04-16T11:00:00Z', '2026-04-16T12:00:00Z']);
    expect(ordered).toEqual(['2026-04-16T12:00:00Z', '2026-04-16T11:00:00Z']);
  });
});

describe('selectAlertRowsForView (§3.3)', () => {
  it('reads the active source projection whenever a bundle exists', () => {
    const claude = makeClaudeSourceEntry({
      data: {
        ...makeClaudeSourceData(),
        alerts: { rows: [claudeRow] as unknown as Record<string, unknown>[] },
      },
    });
    const codex = makeCodexSourceEntry();
    const all = makeAllSourceEntry(claude, codex);
    const env = bundleWith({ sources: { claude, codex, all } });
    const legacy: SourceAlertRow[] = [
      { ...claudeRow, id: 'legacy-only', key: 'legacy-only' } as SourceAlertRow,
    ];
    const rows = selectAlertRowsForView(
      resolveSourceView(env, 'claude'), legacy, true,
    );
    // The populated-legacy preference is GONE: a bundle wins even when the
    // legacy array has rows of its own.
    expect(rows.map((r) => (r as { id?: string }).id)).toEqual([
      'weekly:2026-04-13:90:0',
    ]);
  });

  it('falls back to the legacy array only when there is no bundle', () => {
    const legacy: SourceAlertRow[] = [
      { ...claudeRow, id: 'legacy-only' } as SourceAlertRow,
    ];
    const rows = selectAlertRowsForView(
      resolveSourceView(null, 'claude'), legacy, false,
    );
    expect(rows.map((r) => (r as { id?: string }).id)).toEqual(['legacy-only']);
  });
});
