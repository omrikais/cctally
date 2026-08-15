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

// #556 S5 §5.11 — PROVIDER-AWARE alert focus.
//
// All's Recent Alerts reads the server-built union of both providers' rows, and
// under All each provider has its OWN focus slot. One account key applied to
// the whole row set cannot express that state: a Codex focus would delete every
// Claude row, because no Claude row carries a Codex key. So the filter takes
// one key per provider and decides each row against its own `source`.
//
// Three properties are preserved from the single-key form:
//   * a vendor-wide `*` crossing stays visible under any focus;
//   * a provider whose rows carry no account fields at all (an old or
//     undecorated envelope) is never filtered, so compatibility cannot become
//     data loss disguised as filtering — and that check is now PER PROVIDER,
//     because a decorated Codex beside an undecorated Claude is a real state;
//   * the input order is preserved, which under All is the canonical
//     `alerted_at` order S3 shipped.
export interface AlertAccountFocus {
  claude: string | null;
  codex: string | null;
}

export function filterAlertRowsForFocus(
  rows: SourceAlertRow[],
  focus: AlertAccountFocus,
): SourceAlertRow[] {
  if (focus.claude == null && focus.codex == null) return rows;
  const carries: Record<SourceName, boolean> = { claude: false, codex: false };
  for (const row of rows) {
    if (alertAccount(row) != null) carries[row.source] = true;
  }
  return rows.filter((row) => {
    const wanted = focus[row.source];
    if (wanted == null || !carries[row.source]) return true;
    const key = alertAccount(row)?.key;
    return key === wanted || key === '*';
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

// #556 S3 §3.1/§3.3. The panel and the modal carried this ternary
// independently; changing one and not the other would split them silently,
// which is E1's failure class. The populated-legacy preference is GONE:
// nothing in production dispatches INGEST_SNAPSHOT_ALERTS, so `legacyRows` is
// empty in normal operation and that branch was never taken.
export function selectAlertRowsForView(
  view: SourceView,
  legacyRows: SourceAlertRow[],
  hasBundle: boolean,
): SourceAlertRow[] {
  return hasBundle ? selectSourceAlertRows(view) : legacyRows;
}

// The canonical firing instant, from ONE accessor for both providers. A v7
// Codex row carries `alerted_at`; a pre-v7 one carries only `created_at`,
// which is why the fallback stays. Reading the same field the union is ordered
// by is what keeps the printed instant and the sort key from drifting apart.
function alertWhenIso(row: SourceAlertRow): string | null {
  const fields = row as SourceAlertRow & {
    alerted_at?: string;
    created_at?: string;
  };
  return fields.alerted_at ?? fields.created_at ?? null;
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
      whenIso: alertWhenIso(row),
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
      whenIso: alertWhenIso(row),
    };
  }
  const chipClass = row.axis === 'projected' ? 'chip--projected' : 'chip--codex_budget';
  // #556 S3 §4.2 — one map governs every rendered chip. This line used to
  // hardcode the same two strings, so a provider-sourced Codex row took its
  // chip from here and never from `AXIS_CHIP_LABEL`; renaming only the map
  // would have changed nothing on the panel or in the modal while the
  // Python↔TS parity test stayed green.
  const chipLabel = AXIS_CHIP_LABEL[row.axis];
  return {
    source: 'codex',
    sourceLabel: SOURCE_LABEL.codex,
    threshold: row.threshold,
    severity: severityForThreshold(row.threshold),
    chipClass,
    chipLabel,
    whenIso: alertWhenIso(row),
  };
}
