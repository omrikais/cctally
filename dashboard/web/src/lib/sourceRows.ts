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

// Provider labels are presentation text, not identity. Preserve a supplied
// nonempty label first; only absent labels receive deterministic duration copy.
export function nativeLimitLabel(
  label: string | null | undefined,
  windowMinutes: number | null | undefined,
): string {
  const trimmed = label?.trim();
  if (trimmed) return trimmed;
  if (windowMinutes === 300) return '5-hour limit';
  if (windowMinutes === 10_080) return '7-day limit';
  if (windowMinutes == null || windowMinutes <= 0) return 'Codex quota';
  if (windowMinutes % 1_440 === 0) return `${windowMinutes / 1_440}-day limit`;
  if (windowMinutes % 60 === 0) return `${windowMinutes / 60}-hour limit`;
  return `${windowMinutes}-minute limit`;
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
      label: nativeLimitLabel(history?.label, history?.window_minutes),
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
  durationMin: number | null;
  project: string;
  projectKey: string | null;
  cacheHitPct: number | null;
  tokens: CodexTokenCells | ClaudeTokenCells;
  // Claude rows keep their legacy fields for the existing columns.
  legacy?: ClaudeSessionSourceRow;
}

export function adaptClaudeSessionRows(rows: ClaudeSessionSourceRow[]): SessionDisplayRow[] {
  return (rows ?? []).map((row) => ({
    source: 'claude',
    key: row.key,
    title: row.title?.trim() ?? '',
    recencyUtc: row.started_utc,
    models: row.model ? [row.model] : [],
    costUsd: row.cost_usd,
    durationMin: row.duration_min ?? null,
    project: row.project,
    projectKey: row.project_key ?? null,
    cacheHitPct: row.cache_hit_pct ?? null,
    tokens: { kind: 'claude' },
    legacy: row,
  }));
}

export function adaptCodexSessionRows(rows: CodexSessionRow[]): SessionDisplayRow[] {
  return (rows ?? []).map((row) => ({
    source: 'codex',
    key: row.key,
    title: row.label?.trim() ?? '',
    recencyUtc: row.last_activity,
    models: [...(row.models ?? [])],
    costUsd: row.cost_usd,
    durationMin: row.duration_min ?? null,
    project: row.project ?? 'Project resolving',
    projectKey: row.project_key ?? null,
    cacheHitPct: row.input_tokens > 0 ? row.cached_input_tokens / row.input_tokens * 100 : null,
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

// Collect the Sessions display rows for the active source view (§6.3). Claude
// and Codex adapt their native rows; All concatenates both provider children
// (the caller sorts by the shared recency comparator to interleave — no merging
// of labels or native keys, each row keeps its own `source`).
export function collectSourceSessionRows(view: SourceView): SessionDisplayRow[] {
  if (view.selection === 'claude') {
    return adaptClaudeSessionRows((view.env?.sessions?.rows ?? []).map((row) => ({
      key: row.session_id,
      source: 'claude' as const,
      started_utc: row.started_utc,
      duration_min: row.duration_min,
      model: row.model,
      project: row.project,
      project_key: row.project_key,
      cost_usd: row.cost_usd,
      cache_hit_pct: row.cache_hit_pct,
      title: row.title,
    })));
  }
  if (view.selection === 'codex') {
    const data = view.entry?.data as CodexSourceData | null | undefined;
    return adaptCodexSessionRows(data?.sessions?.rows ?? []);
  }
  if (view.selection === 'all') {
    const providers = (view.entry?.data as AllSourceData | null | undefined)?.providers;
    // The production All envelope may intentionally omit nested provider
    // payloads and rely on the sibling source entries.  Keep All as one
    // chronological list by falling back at the adapter boundary instead of
    // rendering an empty board or duplicating provider sections.
    const claude = providers?.claude ?? view.env?.sources?.claude?.data ?? null;
    const codex = providers?.codex ?? view.env?.sources?.codex?.data ?? null;
    const claudeRows = claude
      ? adaptClaudeSessionRows((claude.sessions?.rows ?? []) as ClaudeSessionSourceRow[])
      : [];
    const codexRows = codex
      ? adaptCodexSessionRows(codex.sessions?.rows ?? [])
      : [];
    return [...claudeRows, ...codexRows];
  }
  return [];
}

// The filter (`f`) + search (`/`) haystack for a source session row: label +
// models (§6.3, enumerated). Lowercased; whitespace inside the needle is a
// literal char (mirrors the legacy Claude haystack contract).
export function sourceSessionHaystack(r: SessionDisplayRow): string {
  return [r.title || '', r.project, r.durationMin == null ? '' : `${r.durationMin}m`,
    r.costUsd == null ? '' : `$${r.costUsd.toFixed(2)}`, ...r.models].join(' ').toLowerCase();
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
