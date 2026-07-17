// #294 S5 §6.3 — the source-aware Sessions grid columns (Codex + All), over the
// presentation `SessionDisplayRow` shape. Distinct from ALL_SESSIONS_COLUMNS
// (the legacy Claude `SessionRow` set) because Codex rows carry provider-native
// vocabulary (label / last_activity / the five token counters) and do NOT share
// the legacy identities.
//
// SORTABLE columns (§6.3, enumerated): last activity (recency, default sort),
// Session (label), Total tokens, Cost. Every other column is display-only
// (`sortable: false`) — the Source chip (All only), Models chips, and the four
// non-total token cells. The `all` column is spliced in only for the All-mode
// interleave; single-source Codex omits it.

import type { SessionDisplayRow } from './sourceRows';
import type { TableColumn } from './tableSort';

export function sessionRowTotalTokens(r: SessionDisplayRow): number {
  return r.tokens.kind === 'codex' ? r.tokens.total : 0;
}

function recencyMs(r: SessionDisplayRow): number {
  return r.recencyUtc ? Date.parse(r.recencyUtc) || 0 : 0;
}

// The full source-grid column set. `includeSource` splices the leading Source
// chip column (All-mode interleave); single-source Codex passes false.
export function sourceSessionsColumns(
  { includeSource }: { includeSource: boolean },
): TableColumn<SessionDisplayRow>[] {
  const cols: TableColumn<SessionDisplayRow>[] = [];
  if (includeSource) {
    cols.push({
      id: 'source', label: 'Source', defaultDirection: 'asc', sortable: false,
      compare: () => 0,
    });
  }
  cols.push(
    { id: 'label', label: 'Session', defaultDirection: 'asc',
      compare: (a, b) => (a.title || '').localeCompare(b.title || ''),
    },
    { id: 'recency', label: 'Last activity', defaultDirection: 'desc',
      // Rows without a recency timestamp park at the END (direction-invariant).
      nullKey: (r) => r.recencyUtc ?? null,
      compare: (a, b) => recencyMs(a) - recencyMs(b),
    },
    { id: 'models', label: 'Models', defaultDirection: 'asc', sortable: false,
      compare: () => 0,
    },
    { id: 'input', label: 'Input', defaultDirection: 'desc', numeric: true, sortable: false,
      compare: () => 0,
    },
    { id: 'cached', label: 'Cached', defaultDirection: 'desc', numeric: true, sortable: false,
      compare: () => 0,
    },
    { id: 'output', label: 'Output', defaultDirection: 'desc', numeric: true, sortable: false,
      compare: () => 0,
    },
    { id: 'reasoning', label: 'Reasoning', defaultDirection: 'desc', numeric: true, sortable: false,
      compare: () => 0,
    },
    { id: 'total', label: 'Total', defaultDirection: 'desc', numeric: true,
      compare: (a, b) => sessionRowTotalTokens(a) - sessionRowTotalTokens(b),
    },
    { id: 'cost', label: 'Cost', defaultDirection: 'desc', numeric: true,
      compare: (a, b) => (a.costUsd ?? 0) - (b.costUsd ?? 0),
    },
  );
  return cols;
}

// The default recency-desc order (no header override active): newest first,
// null recency last. This is the §6.3 "default sort desc" on last_activity, and
// the shared comparator that interleaves the two providers' rows in All mode.
export function sourceRecencyDescCompare(a: SessionDisplayRow, b: SessionDisplayRow): number {
  const na = a.recencyUtc == null;
  const nb = b.recencyUtc == null;
  if (na && nb) return 0;
  if (na) return 1;
  if (nb) return -1;
  return recencyMs(b) - recencyMs(a);
}
