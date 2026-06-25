import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ComparisonDiff } from './ComparisonDiff';
import type { AlignmentRow } from './sessionAlign';

const rows: AlignmentRow[] = [
  { kind: 'match', a: { uuid: 'a1', label: 'shared' }, b: { uuid: 'b1', label: 'shared' }, divergence: false },
  { kind: 'replace', a: { uuid: 'a2', label: 'fix mock' }, b: { uuid: 'b2', label: 'use fixtures' }, divergence: true },
];

describe('ComparisonDiff', () => {
  it('renders two columns with a divergence marker, expands a row on click', () => {
    const onExpand = vi.fn();
    render(
      <ComparisonDiff
        rows={rows}
        wide={true}
        expandedKey={null}
        onToggleRow={onExpand}
        promptsA={{ a2: 'full A text' }}
        promptsB={{ b2: 'full B text' }}
        onOpenInReader={() => {}}
      />,
    );
    expect(screen.getByText('fix mock')).toBeInTheDocument();
    expect(screen.getByText('use fixtures')).toBeInTheDocument();
    expect(screen.getByText(/divergence/i)).toBeInTheDocument(); // ⚡ bar
    fireEvent.click(screen.getByText('fix mock'));
    expect(onExpand).toHaveBeenCalledWith('a2|b2'); // row key
  });

  it('unified mode renders one column with A/B-labelled replace rows', () => {
    render(
      <ComparisonDiff
        rows={rows}
        wide={false}
        expandedKey={null}
        onToggleRow={() => {}}
        promptsA={{}}
        promptsB={{}}
        onOpenInReader={() => {}}
      />,
    );
    expect(screen.getByText('fix mock')).toBeInTheDocument();
    expect(screen.getByText('use fixtures')).toBeInTheDocument();
  });

  it('renders the expanded panel with full text + open-in-reader when expandedKey matches', () => {
    const onOpen = vi.fn();
    render(
      <ComparisonDiff
        rows={rows}
        wide={true}
        expandedKey={'a2|b2'}
        onToggleRow={() => {}}
        promptsA={{ a2: 'full A text' }}
        promptsB={{ b2: 'full B text' }}
        onOpenInReader={onOpen}
      />,
    );
    expect(screen.getByText('full A text')).toBeInTheDocument();
    expect(screen.getByText('full B text')).toBeInTheDocument();
    const openButtons = screen.getAllByRole('button', { name: /open in reader/i });
    expect(openButtons.length).toBe(2);
    fireEvent.click(openButtons[0]);
    expect(onOpen).toHaveBeenCalledWith('a', 'a2');
  });

  it('renders a hatched gap for a one-sided row (aOnly) without a divergence bar', () => {
    const oneSided: AlignmentRow[] = [
      { kind: 'match', a: { uuid: 'a1', label: 'shared' }, b: { uuid: 'b1', label: 'shared' }, divergence: false },
      { kind: 'aOnly', a: { uuid: 'a2', label: 'extra A' }, b: null, divergence: false },
    ];
    const { container } = render(
      <ComparisonDiff
        rows={oneSided}
        wide={true}
        expandedKey={null}
        onToggleRow={() => {}}
        promptsA={{}}
        promptsB={{}}
        onOpenInReader={() => {}}
      />,
    );
    expect(screen.getByText('extra A')).toBeInTheDocument();
    expect(container.querySelector('.conv-cmp-cell--gap')).not.toBeNull();
    expect(screen.queryByText(/divergence/i)).toBeNull();
  });

  it('wide mode uses −/+/= markers (no ◆) and a match container', () => {
    const r: AlignmentRow[] = [
      { kind: 'match', a: { uuid: 'm1', label: 'shared' }, b: { uuid: 'm2', label: 'shared' }, divergence: false },
      { kind: 'replace', a: { uuid: 'a2', label: 'fix mock' }, b: { uuid: 'b2', label: 'use fixtures' }, divergence: true },
      { kind: 'aOnly', a: { uuid: 'a3', label: 'only in A' }, b: null, divergence: false },
    ];
    const { container } = render(
      <ComparisonDiff rows={r} wide expandedKey={null} onToggleRow={() => {}}
        promptsA={{}} promptsB={{}} onOpenInReader={() => {}} />,
    );
    const markers = [...container.querySelectorAll('.conv-cmp-cell-marker')].map((e) => e.textContent?.trim());
    expect(markers).toContain('=');
    expect(markers).toContain('−');
    expect(markers).toContain('+');
    expect(markers).not.toContain('◆');
    expect(container.querySelector('.conv-cmp-cell--match')).not.toBeNull();
  });
});
