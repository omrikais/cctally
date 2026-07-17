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
  // #299 — Sessions renders its table as role="grid"; emit the matching
  // thead/header-row roles only then. The other 4 consumers stay native tables.
  grid?: boolean;
}

function caret(active: boolean, dir: 'asc' | 'desc' | null): string {
  if (!active || !dir) return '↕';        // rest: advertise sortability (dimmed via CSS)
  return dir === 'asc' ? '▲' : '▼';
}

function ariaSort(active: boolean, dir: 'asc' | 'desc' | null): 'ascending' | 'descending' | 'none' {
  if (!active || !dir) return 'none';
  return dir === 'asc' ? 'ascending' : 'descending';
}

export function SortableHeader<T>({ columns, override, onChange, accentVar, grid }: SortableHeaderProps<T>) {
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
    <thead role={grid ? 'rowgroup' : undefined}>
      <tr role={grid ? 'row' : undefined}>
        {columns.map((col) => {
          // #294 S5 — a display-only column (`sortable: false`) renders a plain,
          // non-interactive header cell: no sort button, caret, aria-sort, or
          // tab stop. Used by the source-sessions grid for the models / token
          // cells / source chip columns (only recency/label/total/cost sort).
          if (col.sortable === false) {
            const staticCls = [
              'th-static',
              col.className ?? '',
              col.numeric ? 'num' : '',
            ].filter(Boolean).join(' ');
            return (
              <th
                key={col.id}
                className={staticCls}
                role={grid ? 'columnheader' : undefined}
                data-col={col.id}
                title={col.title}
              >
                {col.label}
              </th>
            );
          }
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
              title={col.title}
              style={style}
              onClick={onClick(col)}
              onKeyDown={onKey(col)}
            >
              <button type="button" className="th-sort-btn" tabIndex={-1}>
                <span className="th-label">{col.label}</span>
                <span
                  className={'th-caret' + (active ? '' : ' th-caret--rest')}
                  aria-hidden="true"
                >{caret(active, dir)}</span>
              </button>
            </th>
          );
        })}
      </tr>
    </thead>
  );
}
