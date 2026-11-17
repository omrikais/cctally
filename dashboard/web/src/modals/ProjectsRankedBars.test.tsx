import { render } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { ProjectsRankedBars } from './ProjectsRankedBars';

const series = [
  { key: 'cctally-dev', bucket_path: '/repos/cctally-dev', weekly: [92], cost: 92 },
  { key: 'superpowers', bucket_path: '/repos/superpowers', weekly: [3], cost: 3 },
  { key: '(other)', bucket_path: '(other)', weekly: [5], cost: 5 },
];

describe('ProjectsRankedBars', () => {
  it('renders one row per series with a labeled value', () => {
    const { container } = render(<ProjectsRankedBars series={series} />);
    expect(container.querySelectorAll('[data-series-key]').length).toBe(3);
    expect(container.textContent).toContain('cctally-dev');
    expect(container.textContent).toContain('92%');
  });
  it('labels rows with the bucket_path basename, not the full path', () => {
    const { container } = render(<ProjectsRankedBars series={series} />);
    const row = container.querySelector('[data-series-key="cctally-dev"]')!;
    // Basename shows; the full path lives on the title attr.
    expect(row.querySelector('.rk-label')!.textContent).toBe('cctally-dev');
    expect(row.getAttribute('title')).toBe('/repos/cctally-dev');
  });
  it('drills a real project on click but not (other)', () => {
    const onSel = vi.fn();
    const { container } = render(<ProjectsRankedBars series={series} onProjectSelect={onSel} />);
    (container.querySelector('[data-series-key="cctally-dev"]') as HTMLElement).click();
    (container.querySelector('[data-series-key="(other)"]') as HTMLElement).click();
    expect(onSel).toHaveBeenCalledTimes(1);
    expect(onSel).toHaveBeenCalledWith('cctally-dev');
  });
});

// #923 S2 — source rows pass their display label explicitly; a genuine path
// still renders its basename. Both render through ProjectLabel.
describe('ProjectsRankedBars labels (#923 S2)', () => {
  it('renders an explicit display label whole, not its "/" basename', () => {
    const { container } = render(
      <ProjectsRankedBars
        series={[{ key: 'p1', bucket_path: 'app (a/b)', label: 'app (a/b)', weekly: [1], cost: 1 }]}
      />,
    );
    const label = container.querySelector('[data-series-key="p1"] .rk-label')!;
    expect(label.textContent).toBe('app (a/b)');
    expect(label.querySelector('.pl-suffix')?.textContent).toBe(' (a/b)');
    expect(container.querySelector('[data-series-key="p1"]')!.getAttribute('title')).toBe('app (a/b)');
  });

  it('keeps the basename of a genuine path whose name has parentheses', () => {
    const { container } = render(
      <ProjectsRankedBars series={[{ key: 'p2', bucket_path: '/x/app (old)', weekly: [1], cost: 1 }]} />,
    );
    const label = container.querySelector('[data-series-key="p2"] .rk-label')!;
    expect(label.textContent).toBe('app (old)');
    expect(label.querySelector('.pl-suffix')?.textContent).toBe(' (old)');
  });
});
