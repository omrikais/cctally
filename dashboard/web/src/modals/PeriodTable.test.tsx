import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest';
import { render, fireEvent, screen } from '@testing-library/react';
import { PeriodTable } from './PeriodTable';
import { _resetForTests } from '../store/store';
import type { PeriodRow } from '../types/envelope';

function periodRow(over: Partial<PeriodRow>): PeriodRow {
  return {
    label: '2026-W26', cost_usd: 10, total_tokens: 0, input_tokens: 0,
    output_tokens: 0, cache_creation_tokens: 0, cache_read_tokens: 0,
    used_pct: 5, dollar_per_pct: 2, delta_cost_pct: null, is_current: false,
    models: [], ...over,
  };
}

// Rows carry no week_start_at, so keyOf(row, 'week') falls back to label
// — the row key equals the label here.
const ROWS: PeriodRow[] = [
  periodRow({ label: '2026-W26' }),
  periodRow({ label: '2026-W27', cost_usd: 20, used_pct: 8, dollar_per_pct: 2.5, delta_cost_pct: 1 }),
];

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});
afterEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('PeriodTable keyboard row selection (SH-3, key-based)', () => {
  it('discloses every account behind a pooled weekly row', () => {
    render(
      <PeriodTable
        rows={[periodRow({
          account_labels: ['work@example.com', 'personal@example.com'],
        })]}
        variant="weekly"
        accentClass="accent-cyan"
        selectedKey={null}
        onSelect={vi.fn()}
      />,
    );
    expect(
      Array.from(document.querySelectorAll('.period-account-chip')).map(
        (chip) => chip.textContent,
      ),
    ).toEqual(['work@example.com', 'personal@example.com']);
  });

  it('rows are focusable and Enter/Space selects by key like a click', () => {
    const onSelect = vi.fn();
    const { container } = render(
      <PeriodTable rows={ROWS} variant="weekly" accentClass="accent-cyan" selectedKey="2026-W26" onSelect={onSelect} />,
    );
    const rows = container.querySelectorAll('tbody tr');
    expect((rows[1] as HTMLElement).tabIndex).toBe(0);
    fireEvent.keyDown(rows[1], { key: 'Enter' });
    expect(onSelect).toHaveBeenCalledWith('2026-W27');
    fireEvent.keyDown(rows[0], { key: ' ' });
    expect(onSelect).toHaveBeenCalledWith('2026-W26');
  });

  it('clicking a row selects it by key', () => {
    const onSelect = vi.fn();
    const { container } = render(
      <PeriodTable rows={ROWS} variant="weekly" accentClass="accent-cyan" selectedKey="2026-W26" onSelect={onSelect} />,
    );
    const rows = container.querySelectorAll('tbody tr');
    fireEvent.click(rows[1]);
    expect(onSelect).toHaveBeenCalledWith('2026-W27');
  });
});

describe('PeriodTable header (WM-1)', () => {
  it('labels the delta column "Δ cost" (not bare "Δ")', () => {
    render(<PeriodTable rows={ROWS} variant="weekly" accentClass="accent-cyan" selectedKey={null} onSelect={vi.fn()} />);
    expect(screen.getByRole('columnheader', { name: 'Δ cost' })).toBeInTheDocument();
    expect(screen.queryByRole('columnheader', { name: 'Δ' })).toBeNull();
  });

  it('renders the weekly-only Used % / $/1% headers; monthly omits them', () => {
    const { unmount } = render(
      <PeriodTable rows={ROWS} variant="weekly" accentClass="accent-cyan" selectedKey={null} onSelect={vi.fn()} />,
    );
    expect(screen.getByRole('columnheader', { name: 'Used %' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: '$/1%' })).toBeInTheDocument();
    unmount();
    render(<PeriodTable rows={ROWS} variant="monthly" accentClass="accent-pink" selectedKey={null} onSelect={vi.fn()} />);
    expect(screen.queryByRole('columnheader', { name: 'Used %' })).toBeNull();
    expect(screen.queryByRole('columnheader', { name: '$/1%' })).toBeNull();
  });
});

describe('PeriodTable provider model labels', () => {
  it('keeps same-family releases separate and exact-model colored', () => {
    render(
      <PeriodTable
        rows={[periodRow({
          models: [
            {
              model: 'claude-opus-4-8', display: 'opus-4-8', chip: 'opus',
              cost_usd: 7, cost_pct: 70,
            },
            {
              model: 'claude-opus-5', display: 'opus-5', chip: 'opus',
              cost_usd: 3, cost_pct: 30,
            },
          ],
        })]}
        variant="weekly"
        accentClass="accent-cyan"
        selectedKey={null}
        onSelect={vi.fn()}
      />,
    );
    const chips = Array.from(
      document.querySelectorAll('.models-chips .chip'),
    ) as HTMLElement[];
    expect(chips.map((chip) => chip.textContent)).toEqual([
      'opus-4-8',
      'opus-5',
    ]);
    expect(chips[0].style.backgroundColor)
      .not.toBe(chips[1].style.backgroundColor);
  });

  it('keeps distinct Codex model identities instead of collapsing them to other', () => {
    const codexRows = [periodRow({
      models: [
        { model: 'gpt-5.6-sol', display: '5.6-sol', chip: 'other', cost_usd: 7, cost_pct: 70 },
        { model: 'gpt-5.6-terra', display: '5.6-terra', chip: 'other', cost_usd: 3, cost_pct: 30 },
      ],
    })];
    render(
      <PeriodTable rows={codexRows} variant="weekly" accentClass="accent-cyan" selectedKey={null} onSelect={vi.fn()} />,
    );
    expect(screen.getByText('5.6-sol')).toBeInTheDocument();
    expect(screen.getByText('5.6-terra')).toBeInTheDocument();
    expect(screen.queryByText('other')).toBeNull();
  });
});

describe('PeriodTable sortable headers', () => {
  const firstRowLabel = (container: HTMLElement) =>
    container.querySelector('tbody tr td')?.textContent ?? '';

  it('clicking "Cost (USD)" reorders rows (default envelope order → cost desc)', () => {
    const { container } = render(
      <PeriodTable rows={ROWS} variant="weekly" accentClass="accent-cyan" selectedKey={null} onSelect={vi.fn()} />,
    );
    // Envelope order: W26 (10), W27 (20).
    expect(firstRowLabel(container)).toContain('2026-W26');
    fireEvent.click(screen.getByRole('columnheader', { name: 'Cost (USD)' }));
    // cost desc → W27 (20) leads.
    expect(firstRowLabel(container)).toContain('2026-W27');
  });
});

// #556 S2 §5.3 / §5.4 — the monthly variant under All: its own sort scope, its
// own provider column, and no cross-provider interleaving.
describe('#556 S2 — All monthly table', () => {
  const ALL_ROWS: PeriodRow[] = [
    periodRow({ label: '2026-04', source: 'claude', cost_usd: 5, used_pct: null, dollar_per_pct: null }),
    periodRow({ label: '2026-03', source: 'claude', cost_usd: 30, used_pct: null, dollar_per_pct: null }),
    periodRow({ label: '2026-04', source: 'codex', cost_usd: 20, used_pct: null, dollar_per_pct: null }),
    periodRow({ label: '2026-03', source: 'codex', cost_usd: 1, used_pct: null, dollar_per_pct: null }),
  ];

  // DELETED: 'renders the provider column for monthly'. It passed unchanged on
  // `main`, because it handed `showSource` to the component explicitly and
  // `PeriodTable` has always rendered the column when told to. What this
  // session changed is that `PeriodModal` now PASSES it for the monthly
  // variant, and that is not observable from here. The discriminating version
  // lives one level up, in ProviderModalParity.test.tsx's
  // 'keeps two same-labelled provider rows distinct instead of merging them',
  // which renders <MonthlyModal /> and reads the chips out of the real table.

  it('sorts INSIDE each provider section rather than interleaving them', () => {
    // Sorting the union by cost would produce 30, 20, 5, 1 — one ranked list
    // over two independent reset axes, which is exactly the blend the unmerge
    // exists to prevent.
    const { container } = render(
      <PeriodTable
        rows={ALL_ROWS} variant="monthly" accentClass="accent-pink"
        selectedKey={null} onSelect={vi.fn()} showSource
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /Cost \(USD\)/ }));
    const sources = [...container.querySelectorAll('tbody tr')].map(
      (row) => row.querySelector('.source-chip')!.textContent,
    );
    expect(sources).toEqual(['Claude', 'Claude', 'Codex', 'Codex']);
    const costs = [...container.querySelectorAll('tbody tr')].map(
      (row) => row.querySelectorAll('td')[3].textContent,
    );
    expect(costs).toEqual(['$30.00', '$5.00', '$20.00', '$1.00']);
  });

  it('keeps a monthly sort out of the weekly table', () => {
    const { unmount } = render(
      <PeriodTable
        rows={ALL_ROWS} variant="monthly" accentClass="accent-pink"
        selectedKey={null} onSelect={vi.fn()} showSource
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /Cost \(USD\)/ }));
    unmount();

    // The weekly table has its own scope, so it renders in envelope order.
    const { container } = render(
      <PeriodTable rows={ROWS} variant="weekly" accentClass="accent-cyan"
        selectedKey={null} onSelect={vi.fn()} />,
    );
    const labels = [...container.querySelectorAll('tbody tr td:first-child')].map(
      (cell) => cell.textContent,
    );
    expect(labels).toEqual(['2026-W26', '2026-W27']);
  });
});

