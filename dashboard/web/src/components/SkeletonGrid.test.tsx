import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { SkeletonGrid } from './SkeletonGrid';

describe('<SkeletonGrid /> (#264 S1)', () => {
  it('echoes the bento shape: three height-class rows with 3 / 6 / 1 cards', () => {
    const { container } = render(<SkeletonGrid mode="bento" />);
    const tall = container.querySelector('.bento-row.row-tall');
    const medium = container.querySelector('.bento-row.row-medium');
    const short = container.querySelector('.bento-row.row-short');
    expect(tall).not.toBeNull();
    expect(medium).not.toBeNull();
    expect(short).not.toBeNull();
    expect(tall!.querySelectorAll('.panel.is-skeleton')).toHaveLength(3);
    // #266 — the medium row is now a 6-card 3×2 (daily·cache / weekly·monthly /
    // blocks·forecast); the short row holds only Alerts (full width).
    expect(medium!.querySelectorAll('.panel.is-skeleton')).toHaveLength(6);
    expect(short!.querySelectorAll('.panel.is-skeleton')).toHaveLength(1);
  });

  it('carries a data-span on each placeholder so the bento CSS can size it', () => {
    const { container } = render(<SkeletonGrid mode="bento" />);
    // Sessions leads the tall row at span 6; every placeholder has some span.
    const hosts = Array.from(container.querySelectorAll('.bento-row .panel-host'));
    expect(hosts).toHaveLength(10);
    expect(hosts.every((h) => (h as HTMLElement).dataset.span)).toBe(true);
    const tallSpans = Array.from(
      container.querySelectorAll('.bento-row.row-tall .panel-host'),
    ).map((h) => (h as HTMLElement).dataset.span);
    expect(tallSpans).toEqual(['6', '3', '3']);
  });

  // #293 S1 — the skeleton must tile identically to the live grid so the
  // load→ready swap can't reflow. At intermediate the tall row is
  // Sessions-full (12) + Trend·Projects paired (6/6), and the grid carries
  // data-board-mode so the dense-flow CSS rule applies.
  it('matches the intermediate live-grid contract (Sessions 12 / Trend·Projects 6, data-board-mode)', () => {
    const { container } = render(<SkeletonGrid mode="intermediate" />);
    const grid = container.querySelector('.dash-grid');
    expect(grid).toHaveAttribute('data-board-mode', 'intermediate');
    const tallSpans = Array.from(
      container.querySelectorAll('.bento-row.row-tall .panel-host'),
    ).map((h) => (h as HTMLElement).dataset.span);
    expect(tallSpans).toEqual(['12', '6', '6']);
  });
});
