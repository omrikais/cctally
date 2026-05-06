import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SortableHeader } from '../src/components/SortableHeader';
import type { SortOverride, TableColumn } from '../src/lib/tableSort';

interface Row { id: string; n: number }

const COLS: TableColumn<Row>[] = [
  { id: 'n', label: 'N', defaultDirection: 'desc',
    compare: (a, b) => a.n - b.n },
  { id: 's', label: 'S', defaultDirection: 'asc',
    compare: () => 0 },
];

function setup(override: SortOverride | null) {
  const onChange = vi.fn();
  const outerClick = vi.fn();
  const utils = render(
    <div data-testid="outer" onClick={outerClick}>
      <table>
        <SortableHeader columns={COLS} override={override} onChange={onChange} />
        <tbody><tr><td>x</td></tr></tbody>
      </table>
    </div>,
  );
  // The outer onClick is a React synthetic handler — mirroring the
  // real-world panel/.panel-body case where the parent uses React onClick
  // (which obeys synthetic stopPropagation, unlike native bubble listeners).
  return { ...utils, onChange, outerClick };
}

describe('<SortableHeader />', () => {
  it('renders one <th> per column with the column label', () => {
    const { container } = setup(null);
    const ths = container.querySelectorAll('th');
    expect(ths.length).toBe(2);
    expect(ths[0].textContent).toContain('N');
    expect(ths[1].textContent).toContain('S');
  });

  it('aria-sort is "none" on every column when override is null', () => {
    const { container } = setup(null);
    const ths = container.querySelectorAll('th');
    expect(ths[0].getAttribute('aria-sort')).toBe('none');
    expect(ths[1].getAttribute('aria-sort')).toBe('none');
  });

  it('aria-sort reflects the active override direction', () => {
    const { container } = setup({ column: 'n', direction: 'asc' });
    const ths = container.querySelectorAll('th');
    expect(ths[0].getAttribute('aria-sort')).toBe('ascending');
    expect(ths[1].getAttribute('aria-sort')).toBe('none');
  });

  it('renders ▲ caret when active column is asc, ▼ when desc', () => {
    const a = setup({ column: 'n', direction: 'asc' }).container;
    expect(a.querySelector('th[data-col="n"] .th-caret')?.textContent).toBe('▲');

    const d = setup({ column: 'n', direction: 'desc' }).container;
    expect(d.querySelector('th[data-col="n"] .th-caret')?.textContent).toBe('▼');
  });

  it('first click invokes onChange with column.defaultDirection', async () => {
    const user = userEvent.setup();
    const { container, onChange } = setup(null);
    await user.click(container.querySelector('th[data-col="n"]')!);
    expect(onChange).toHaveBeenCalledWith({ column: 'n', direction: 'desc' });
  });

  it('second click on same column flips direction', async () => {
    const user = userEvent.setup();
    const { container, onChange } = setup({ column: 'n', direction: 'desc' });
    await user.click(container.querySelector('th[data-col="n"]')!);
    expect(onChange).toHaveBeenCalledWith({ column: 'n', direction: 'asc' });
  });

  it('third click on same column clears (passes null)', async () => {
    const user = userEvent.setup();
    const { container, onChange } = setup({ column: 'n', direction: 'asc' });
    await user.click(container.querySelector('th[data-col="n"]')!);
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it('Enter on focused <th> behaves like click', async () => {
    const user = userEvent.setup();
    const { container, onChange } = setup(null);
    const th = container.querySelector('th[data-col="n"]') as HTMLElement;
    th.focus();
    await user.keyboard('{Enter}');
    expect(onChange).toHaveBeenCalledWith({ column: 'n', direction: 'desc' });
  });

  it('Space on focused <th> behaves like click', async () => {
    const user = userEvent.setup();
    const { container, onChange } = setup(null);
    const th = container.querySelector('th[data-col="n"]') as HTMLElement;
    th.focus();
    await user.keyboard(' ');
    expect(onChange).toHaveBeenCalledWith({ column: 'n', direction: 'desc' });
  });

  it('click on <th> stops propagation (outer listener does not fire)', async () => {
    const user = userEvent.setup();
    const { container, outerClick } = setup(null);
    await user.click(container.querySelector('th[data-col="n"]')!);
    expect(outerClick).not.toHaveBeenCalled();
  });

  it('active column gets is-sorted class', () => {
    const { container } = setup({ column: 'n', direction: 'desc' });
    expect(container.querySelector('th[data-col="n"]')?.classList.contains('is-sorted'))
      .toBe(true);
    expect(container.querySelector('th[data-col="s"]')?.classList.contains('is-sorted'))
      .toBe(false);
  });

  it('th has tabindex=0; inner button has tabindex=-1 (single tab stop)', () => {
    const { container } = setup(null);
    const th = container.querySelector('th[data-col="n"]') as HTMLElement;
    const btn = th.querySelector('.th-sort-btn') as HTMLElement;
    expect(th.getAttribute('tabindex')).toBe('0');
    expect(btn.getAttribute('tabindex')).toBe('-1');
  });
});
