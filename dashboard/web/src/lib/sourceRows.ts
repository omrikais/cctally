// #294 S5 — presentation-only display-row adapters for the source panels.
//
// These join / reshape ONE provider's own rows by opaque key. They derive
// nothing cross-provider (that is the envelope's job) — combined values come
// only from the bundle's `combined` object.

import type {
  AllSourceData,
  CodexHero,
  CodexQuotaActiveRow,
  CodexQuotaDomain,
  CodexSessionRow,
  CodexSourceData,
  ClaudeSessionSourceRow,
  SourceName,
} from '../types/envelope';
import type { SourceView } from '../store/sourceView';

// §6.1 quota label join. Hero `quota.active` rows carry the opaque key,
// percentages, reset, and freshness — but NO label or duration. Labels and
// `window_minutes` live in `quota.histories` rows keyed by the same opaque key.
// This attaches each active window's label + duration by that key; an active row
// with no matching history renders the generic "Codex quota" label. Joins one
// provider's own rows and derives nothing.
export interface JoinedCodexQuotaWindow {
  key: string;
  label: string;
  windowMinutes: number | null;
  current: CodexQuotaActiveRow;
}

export function joinCodexQuotaLabels(
  hero: CodexHero,
  quota: CodexQuotaDomain,
): JoinedCodexQuotaWindow[] {
  const byKey = new Map(
    (quota?.histories ?? []).map((h) => [h.key, h] as const),
  );
  const active = hero?.quota?.active ?? [];
  return active.map((row) => {
    const history = byKey.get(row.key);
    return {
      key: row.key,
      label: history?.label ?? 'Codex quota',
      windowMinutes: history?.window_minutes ?? null,
      current: row,
    };
  });
}

// ---- Sessions display-row adapters (§6.3) -----------------------------

export interface CodexTokenCells {
  kind: 'codex';
  input: number;
  cachedInput: number;
  output: number;
  reasoning: number;
  total: number;
}

export interface ClaudeTokenCells {
  kind: 'claude';
  // Claude source rows keep their legacy columns via `legacy`; there is no
  // native per-token breakdown on the source row itself.
}

export interface SessionDisplayRow {
  source: SourceName;
  key: string;
  title: string;
  recencyUtc: string | null;
  models: string[];
  costUsd: number | null;
  tokens: CodexTokenCells | ClaudeTokenCells;
  // Claude rows keep their legacy fields for the existing columns.
  legacy?: ClaudeSessionSourceRow;
}

export function adaptClaudeSessionRows(rows: ClaudeSessionSourceRow[]): SessionDisplayRow[] {
  return (rows ?? []).map((row) => ({
    source: 'claude',
    key: row.key,
    title: row.project,
    recencyUtc: row.started_utc,
    models: row.model ? [row.model] : [],
    costUsd: row.cost_usd,
    tokens: { kind: 'claude' },
    legacy: row,
  }));
}

export function adaptCodexSessionRows(rows: CodexSessionRow[]): SessionDisplayRow[] {
  return (rows ?? []).map((row) => ({
    source: 'codex',
    key: row.key,
    title: row.label,
    recencyUtc: row.last_activity,
    models: [...(row.models ?? [])],
    costUsd: row.cost_usd,
    tokens: {
      kind: 'codex',
      input: row.input_tokens,
      cachedInput: row.cached_input_tokens,
      output: row.output_tokens,
      reasoning: row.reasoning_output_tokens,
      total: row.total_tokens,
    },
  }));
}

// Collect the Sessions display rows for the active source view (§6.3). Codex →
// its own native rows; All → the Claude + Codex provider children CONCATENATED
// (the caller sorts by the shared recency comparator to interleave — no merging
// of labels or native keys, each row keeps its own `source`). Claude single-
// source is rendered by the legacy `getRenderedRows` path, so it returns [] here.
export function collectSourceSessionRows(view: SourceView): SessionDisplayRow[] {
  if (view.selection === 'codex') {
    const data = view.entry?.data as CodexSourceData | null | undefined;
    return adaptCodexSessionRows(data?.sessions?.rows ?? []);
  }
  if (view.selection === 'all') {
    const providers = (view.entry?.data as AllSourceData | null | undefined)?.providers;
    const claudeRows = providers?.claude
      ? adaptClaudeSessionRows((providers.claude.sessions?.rows ?? []) as ClaudeSessionSourceRow[])
      : [];
    const codexRows = providers?.codex
      ? adaptCodexSessionRows(providers.codex.sessions?.rows ?? [])
      : [];
    return [...claudeRows, ...codexRows];
  }
  return [];
}

// The filter (`f`) + search (`/`) haystack for a source session row: label +
// models (§6.3, enumerated). Lowercased; whitespace inside the needle is a
// literal char (mirrors the legacy Claude haystack contract).
export function sourceSessionHaystack(r: SessionDisplayRow): string {
  return [r.title || '', ...r.models].join(' ').toLowerCase();
}

export function applySourceSessionFilter(
  rows: SessionDisplayRow[],
  text: string,
): SessionDisplayRow[] {
  const t = (text || '').toLowerCase();
  if (!t) return rows;
  return rows.filter((r) => sourceSessionHaystack(r).includes(t));
}

// Search-match indices into the passed (already-filtered+sorted+sliced) row
// list, so the highlighted rows align with rendered DOM positions and n/N
// navigation. Mirrors `computeSearchMatches` for the legacy Claude rows.
export function computeSourceSessionMatches(
  rows: SessionDisplayRow[],
  searchText: string,
): number[] {
  const q = (searchText || '').toLowerCase();
  if (!q) return [];
  const out: number[] = [];
  for (let i = 0; i < rows.length; i++) {
    if (sourceSessionHaystack(rows[i]).includes(q)) out.push(i);
  }
  return out;
}
