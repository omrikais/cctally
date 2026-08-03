// #294 S5 Task 7 — stable toast identity + source-aware alert presentation
// (§6.7). Pure functions only; no store imports beyond the SourceView type.
//
// Toast identity uses NORMALIZED, source-qualified ids:
//   - Claude: `claude:${accountKey}:${row.id}` when decorated, otherwise the
//     legacy `claude:${row.id}` — the preserved `id` itself IS stable
//     (the projection's opaque `key` embeds the row ordinal and must NEVER be
//     used for dedup).
//   - Codex:  `codex:${row.key}` — the stable native-identity key.
// The seen-set also carries the bare legacy form (`row.id` / `row.key`) for one
// release of continuity so an in-flight cold-start seeded under the old bare-id
// scheme doesn't re-toast after upgrade.

import type {
  Envelope,
  SourceAlertRow,
  SourceName,
} from '../types/envelope';
import type { SourceView } from '../store/sourceView';
import { AXIS_CHIP_LABEL, alertSeverity } from './alertAxis';

export type AlertSeverity = 'info' | 'warn' | 'critical';

export interface AlertAccount {
  key: string;
  label: string;
}

export function alertAccount(row: SourceAlertRow): AlertAccount | null {
  const fields = row as SourceAlertRow & {
    accountKey?: string;
    accountLabel?: string;
    account_key?: string;
  };
  const key = fields.accountKey ?? fields.account_key;
  if (key == null) return null;
  return { key, label: fields.accountLabel ?? (key === '*' ? 'All accounts' : key) };
}

export function toastAlertId(row: SourceAlertRow): string {
  const base = row.source === 'claude' ? `claude:${row.id}` : `codex:${row.key}`;
  const account = alertAccount(row);
  return account == null
    ? base
    : `${row.source}:${account.key}:${row.source === 'claude' ? row.id : row.key}`;
}

// Normalized identity + the bare legacy form (continuity — see header).
export function seedFormsForRow(row: SourceAlertRow): string[] {
  const priorNormalized = row.source === 'claude' ? `claude:${row.id}` : `codex:${row.key}`;
  const bare = row.source === 'claude' ? row.id : row.key;
  const current = toastAlertId(row);
  return current === priorNormalized
    ? [priorNormalized, bare]
    : [current, priorNormalized, bare];
}

// Focused alert rows are selected directly from their conditional #345 wire.
// A vendor-wide `*` crossing remains visible. An old/undecorated envelope with
// no account fields is returned untouched so compatibility never becomes data
// loss disguised as filtering.
export function filterAlertRowsForAccount(
  rows: SourceAlertRow[],
  accountKey: string | null,
): SourceAlertRow[] {
  if (accountKey == null || !rows.some((row) => alertAccount(row) != null)) {
    return rows;
  }
  return rows.filter((row) => {
    const key = alertAccount(row)?.key;
    return key === accountKey || key === '*';
  });
}

// The toast-pipeline input: the union of the two PROVIDER projections only
// (`sources.claude` + `sources.codex`). The legacy top-level `alerts` array is
// never consumed here, so a codex_budget row can't double-toast (§6.7 Toasts).
export function collectToastAlertRows(env: Envelope | null): SourceAlertRow[] {
  const sources = env?.sources;
  if (sources == null) return [];
  const claude = (sources.claude?.data?.alerts?.rows ?? []) as unknown as SourceAlertRow[];
  const codex = (sources.codex?.data?.alerts?.rows ?? []) as unknown as SourceAlertRow[];
  return [...claude, ...codex];
}

// The active-source projection for the Recent Alerts panel/modal: the active
// entry's own `data.alerts.rows` (Claude rows, Codex rows, or — under `all` —
// the server-built provider-qualified union). Returns [] when the entry has no
// alert projection (hydrating / pre-S4).
export function selectSourceAlertRows(view: SourceView): SourceAlertRow[] {
  const data = view.entry?.data as { alerts?: { rows?: unknown[] } } | null | undefined;
  const rows = data?.alerts?.rows;
  return Array.isArray(rows) ? (rows as SourceAlertRow[]) : [];
}

// Severity for a bare threshold — byte-identical bands with the Python kernel
// (info <90 / warn 90-99 / critical >=100).
function severityForThreshold(threshold: number | null | undefined): AlertSeverity {
  const t = threshold ?? 0;
  return t >= 100 ? 'critical' : t >= 90 ? 'warn' : 'info';
}

function normalizeSeverity(
  token: string | undefined,
  threshold: number | null | undefined,
): AlertSeverity {
  if (token === 'info' || token === 'warn' || token === 'critical') return token;
  if (token === 'amber') return 'warn';
  if (token === 'red') return 'critical';
  return severityForThreshold(threshold);
}

export const SOURCE_LABEL: Record<SourceName, string> = {
  claude: 'Claude',
  codex: 'Codex',
};

export interface SourceAlertDisplay {
  source: SourceName;
  sourceLabel: string;
  threshold: number | null;
  severity: AlertSeverity;
  chipClass: string;
  chipLabel: string;
  whenIso: string | null;
}

// One display shape for both the Claude legacy-field rows and the lean Codex
// rows. The panel/modal render from THIS so the two providers can't drift.
export function alertDisplay(row: SourceAlertRow): SourceAlertDisplay {
  if (row.source === 'claude') {
    return {
      source: 'claude',
      sourceLabel: SOURCE_LABEL.claude,
      threshold: row.threshold,
      severity: alertSeverity(row),
      chipClass: `chip--${row.axis}`,
      chipLabel: AXIS_CHIP_LABEL[row.axis],
      whenIso: row.alerted_at ?? null,
    };
  }
  // Codex source rows (lean _alerts_wire shapes).
  if (row.axis === 'quota') {
    return {
      source: 'codex',
      sourceLabel: SOURCE_LABEL.codex,
      threshold: row.threshold,
      severity: normalizeSeverity(row.severity, row.threshold),
      chipClass: 'chip--quota',
      chipLabel: 'QUOTA',
      whenIso: row.created_at ?? null,
    };
  }
  const chipClass = row.axis === 'projected' ? 'chip--projected' : 'chip--codex_budget';
  const chipLabel = row.axis === 'projected' ? 'PROJECTED' : 'CODEX';
  return {
    source: 'codex',
    sourceLabel: SOURCE_LABEL.codex,
    threshold: row.threshold,
    severity: severityForThreshold(row.threshold),
    chipClass,
    chipLabel,
    whenIso: row.created_at ?? null,
  };
}
