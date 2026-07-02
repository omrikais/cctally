import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { PeriodMiniBars, type PeriodNavRow } from './PeriodMiniBars';

const mk = (k: string, cost: number, cur = false): PeriodNavRow =>
  ({ key: k, label: k, cost, isCurrent: cur, isEmpty: cost === 0 });

function bars(): HTMLButtonElement[] {
  return Array.from(
    document.querySelectorAll<HTMLButtonElement>('.daily-modal-bars-grid .bar'),
  );
}
function axisSpans(): HTMLElement[] {
  return Array.from(
    document.querySelectorAll<HTMLElement>('.daily-modal-bars-axis span'),
  );
}

describe('PeriodMiniBars', () => {
  it('renders one bar per row (oldest-left → newest-right after internal reverse)', () => {
    render(<PeriodMiniBars unit="day" selectedKey="d2"
      rows={[mk('d2', 5, true), mk('d1', 3), mk('d0', 2)]} onSelect={vi.fn()} />);
    const b = bars();
    expect(b).toHaveLength(3);
    // rows are newest-first; internally reversed so oldest (d0) is leftmost.
    expect(b[0].getAttribute('data-key')).toBe('d0');
    expect(b[2].getAttribute('data-key')).toBe('d2');
  });

  it('does not fire onSelect for an empty (zero-cost) bar, but does for a non-empty one', () => {
    const onSelect = vi.fn();
    render(<PeriodMiniBars unit="day" selectedKey="d2"
      rows={[mk('d2', 5, true), mk('d1', 3), mk('d0', 0)]} onSelect={onSelect} />);
    const b = bars(); // [d0 (empty), d1, d2]
    expect(b[0].getAttribute('data-key')).toBe('d0');
    expect(b[0]).toBeDisabled();
    fireEvent.click(b[0]);
    expect(onSelect).not.toHaveBeenCalled();
    fireEvent.click(b[1]); // d1, non-empty
    expect(onSelect).toHaveBeenCalledWith('d1');
  });

  it('the current row bar gets the today class; the selected row bar gets sel + aria-pressed', () => {
    render(<PeriodMiniBars unit="day" selectedKey="d2"
      rows={[mk('d2', 5, true), mk('d1', 3), mk('d0', 2)]} onSelect={vi.fn()} />);
    const b = bars();
    const d2 = b[2];
    expect(d2.className).toContain('today');
    expect(d2.className).toContain('sel');
    expect(d2.getAttribute('aria-pressed')).toBe('true');
    expect(b[1].getAttribute('aria-pressed')).toBe('false');
  });

  it('DA-1: current == last bar drops the centre axis label and suffixes the right label (day → · today)', () => {
    render(<PeriodMiniBars unit="day" selectedKey="d2"
      rows={[mk('d2', 5, true), mk('d1', 3), mk('d0', 2)]} onSelect={vi.fn()} />);
    // d2 is current AND newest → after reverse it is rightmost (== last).
    const spans = axisSpans();
    expect(spans).toHaveLength(2); // first + right-with-suffix; NO centre
    expect(spans[spans.length - 1].textContent).toBe('d2 · today');
    // No bare centre/right label duplicating the current period.
    expect(screen.queryByText('d2')).toBeNull();
  });

  it('DA-1: current == last bar suffixes with · now for week/month', () => {
    render(<PeriodMiniBars unit="month" selectedKey="m2"
      rows={[mk('m2', 5, true), mk('m1', 3), mk('m0', 2)]} onSelect={vi.fn()} />);
    const spans = axisSpans();
    expect(spans).toHaveLength(2);
    expect(spans[spans.length - 1].textContent).toBe('m2 · now');
  });

  it('DA-1: an interior current period keeps the three-span layout with a centre label', () => {
    render(<PeriodMiniBars unit="day" selectedKey="d1"
      rows={[mk('d2', 5), mk('d1', 3, true), mk('d0', 2)]} onSelect={vi.fn()} />);
    const spans = axisSpans();
    expect(spans).toHaveLength(3); // first + centre(current) + last
    expect(spans[1].textContent).toBe('d1 · today');
    expect(spans[2].textContent).toBe('d2'); // right label NOT suffixed
  });

  it('step buttons: › (newer) disabled at the newest selection; ‹ (older) disabled at the oldest', () => {
    // rows newest-first: d2 (newest) … d0 (oldest).
    const { rerender } = render(<PeriodMiniBars unit="day" selectedKey="d2"
      rows={[mk('d2', 5, true), mk('d1', 3), mk('d0', 2)]} onSelect={vi.fn()} />);
    // Selected = newest → cannot step newer.
    expect(screen.getByRole('button', { name: /newer/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /older/i })).not.toBeDisabled();
    // Selected = oldest → cannot step older.
    rerender(<PeriodMiniBars unit="day" selectedKey="d0"
      rows={[mk('d2', 5, true), mk('d1', 3), mk('d0', 2)]} onSelect={vi.fn()} />);
    expect(screen.getByRole('button', { name: /older/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /newer/i })).not.toBeDisabled();
  });

  it('a step button click fires onSelect with the stepped key', () => {
    const onSelect = vi.fn();
    render(<PeriodMiniBars unit="day" selectedKey="d1"
      rows={[mk('d2', 5, true), mk('d1', 3), mk('d0', 2)]} onSelect={onSelect} />);
    fireEvent.click(screen.getByRole('button', { name: /older/i }));
    expect(onSelect).toHaveBeenCalledWith('d0');
    fireEvent.click(screen.getByRole('button', { name: /newer/i }));
    expect(onSelect).toHaveBeenCalledWith('d2');
  });
});
