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
