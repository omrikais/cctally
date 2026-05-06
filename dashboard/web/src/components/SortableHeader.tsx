import type { KeyboardEvent, MouseEvent } from 'react';
import {
  nextSortOverride,
  type SortOverride,
  type TableColumn,
} from '../lib/tableSort';

interface SortableHeaderProps<T> {
  columns: TableColumn<T>[];
  override: SortOverride | null;
  onChange: (next: SortOverride | null) => void;
  accentVar?: string;
}

function caret(active: boolean, dir: 'asc' | 'desc' | null): string {
  if (!active || !dir) return '';
  return dir === 'asc' ? '▲' : '▼';
}

function ariaSort(active: boolean, dir: 'asc' | 'desc' | null): 'ascending' | 'descending' | 'none' {
  if (!active || !dir) return 'none';
  return dir === 'asc' ? 'ascending' : 'descending';
}

export function SortableHeader<T>({ columns, override, onChange, accentVar }: SortableHeaderProps<T>) {
  const fire = (col: TableColumn<T>) => onChange(nextSortOverride(override, col));

  const onClick = (col: TableColumn<T>) => (e: MouseEvent<HTMLTableCellElement>) => {
    e.stopPropagation();
    fire(col);
  };

  const onKey = (col: TableColumn<T>) => (e: KeyboardEvent<HTMLTableCellElement>) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      e.stopPropagation();
      fire(col);
    }
  };

  return (
    <thead>
      <tr>
        {columns.map((col) => {
          const active = override?.column === col.id;
          const dir = active ? override!.direction : null;
          const cls = [
            'th-sortable',
            col.className ?? '',
            col.numeric ? 'num' : '',
            active ? 'is-sorted' : '',
          ].filter(Boolean).join(' ');
          const style = accentVar && active
            ? ({ ['--th-accent' as string]: `var(${accentVar})` } as React.CSSProperties)
            : undefined;
          return (
            <th
              key={col.id}
              className={cls}
              role="columnheader"
              aria-sort={ariaSort(active, dir)}
              tabIndex={0}
              data-col={col.id}
              style={style}
              onClick={onClick(col)}
              onKeyDown={onKey(col)}
            >
              <button type="button" className="th-sort-btn" tabIndex={-1}>
                <span className="th-label">{col.label}</span>
                <span className="th-caret" aria-hidden="true">{caret(active, dir)}</span>
              </button>
            </th>
          );
        })}
      </tr>
    </thead>
  );
}
