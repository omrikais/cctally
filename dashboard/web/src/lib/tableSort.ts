export interface SortOverride {
  column: string;
  direction: 'asc' | 'desc';
}

export interface TableColumn<T> {
  id: string;
  label: string;
  defaultDirection: 'asc' | 'desc';
  compare: (a: T, b: T) => number;
  className?: string;
  numeric?: boolean;
  sortable?: boolean;
  // Optional null-detection hook. When present, `applyTableSort` parks
  // rows whose `nullKey(row)` is null/undefined at the END of the result
  // unconditionally — regardless of asc/desc direction. The column's
  // `compare` is only invoked for two non-null rows, so per-column
  // comparators don't need to (and must not) re-implement null-last
  // semantics: doing it in `compare` breaks under `desc` because
  // `applyTableSort` flips the comparator's return via `sign`, which
  // would flip the null parking too.
  nullKey?: (row: T) => unknown;
}

export function applyTableSort<T>(
  rows: T[],
  columns: TableColumn<T>[],
  override: SortOverride | null,
): T[] {
  if (!override) return rows;
  const col = columns.find((c) => c.id === override.column);
  if (!col) return rows;
  const sign = override.direction === 'desc' ? -1 : 1;
  const nullKey = col.nullKey;
  return rows.slice().sort((a, b) => {
    if (nullKey) {
      const an = nullKey(a) == null;
      const bn = nullKey(b) == null;
      // Null parking is direction-invariant — both directions place
      // null values after non-null. The non-null branch falls through
      // to the signed comparator below.
      if (an && bn) return 0;
      if (an) return 1;
      if (bn) return -1;
    }
    return sign * col.compare(a, b);
  });
}

// 3-state click cycle. Clicking a column header cycles:
//   null  →  column.defaultDirection  →  opposite direction  →  null  →  …
// `defaultDirection` is the column's "useful first" direction (typically
// desc for numeric/temporal columns, asc for text columns). Clicking a
// different column always seeds back to that column's defaultDirection.
export function nextSortOverride(
  cur: SortOverride | null,
  col: { id: string; defaultDirection: 'asc' | 'desc' },
): SortOverride | null {
  if (!cur || cur.column !== col.id) {
    return { column: col.id, direction: col.defaultDirection };
  }
  if (cur.direction === col.defaultDirection) {
    return { column: col.id, direction: cur.direction === 'asc' ? 'desc' : 'asc' };
  }
  return null;
}

// Coerces a JSON-parsed value into a SortOverride or null. Used by
// store.loadInitial to defensively handle hand-edited localStorage.
export function coerceSortOverride(v: unknown): SortOverride | null {
  if (v == null) return null;
  if (typeof v !== 'object' || Array.isArray(v)) return null;
  const obj = v as Record<string, unknown>;
  if (typeof obj.column !== 'string') return null;
  if (obj.direction !== 'asc' && obj.direction !== 'desc') return null;
  return { column: obj.column, direction: obj.direction };
}
