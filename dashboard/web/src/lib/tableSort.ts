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
  return rows.slice().sort((a, b) => sign * col.compare(a, b));
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
