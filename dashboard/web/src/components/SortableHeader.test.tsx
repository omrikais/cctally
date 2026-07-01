import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/react';
import { SortableHeader } from './SortableHeader';
import type { TableColumn } from '../lib/tableSort';

interface Row { a: number; b: number; }
const COLUMNS: TableColumn<Row>[] = [
  { id: 'a', label: 'A', defaultDirection: 'asc', compare: (x, y) => x.a - y.a },
  { id: 'b', label: 'B', defaultDirection: 'asc', compare: (x, y) => x.b - y.b },
];

function carets(el: HTMLElement): string[] {
  return Array.from(el.querySelectorAll('.th-caret')).map((c) => c.textContent ?? '');
}

describe('SortableHeader rest-state glyph (SESS-4)', () => {
  it('shows a dim neutral glyph at rest (no active column)', () => {
    const { container } = render(
      <table><SortableHeader columns={COLUMNS} override={null} onChange={vi.fn()} /></table>,
    );
    // both headers advertise sortability at rest
    expect(carets(container).every((c) => c.length > 0)).toBe(true);
    const ths = container.querySelectorAll('th');
    expect(ths[0].getAttribute('aria-sort')).toBe('none');
  });
  it('shows the active arrow on the sorted column', () => {
    const { container } = render(
      <table>
        <SortableHeader columns={COLUMNS} override={{ column: 'a', direction: 'asc' }} onChange={vi.fn()} />
      </table>,
    );
    const ths = container.querySelectorAll('th');
    expect(ths[0].getAttribute('aria-sort')).toBe('ascending');
    expect(ths[0].querySelector('.th-caret')?.textContent).toBe('▲');
    expect(ths[1].getAttribute('aria-sort')).toBe('none');
  });
});
