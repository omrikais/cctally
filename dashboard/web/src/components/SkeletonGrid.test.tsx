import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { SkeletonGrid } from './SkeletonGrid';

describe('<SkeletonGrid /> (#264 S1)', () => {
  it('echoes the bento shape: three height-class rows with 3 / 2 / 3 cards', () => {
    const { container } = render(<SkeletonGrid />);
    const tall = container.querySelector('.bento-row.row-tall');
    const medium = container.querySelector('.bento-row.row-medium');
    const short = container.querySelector('.bento-row.row-short');
    expect(tall).not.toBeNull();
    expect(medium).not.toBeNull();
    expect(short).not.toBeNull();
    expect(tall!.querySelectorAll('.panel.is-skeleton')).toHaveLength(3);
    expect(medium!.querySelectorAll('.panel.is-skeleton')).toHaveLength(2);
    expect(short!.querySelectorAll('.panel.is-skeleton')).toHaveLength(3);
  });

  it('carries a data-span on each placeholder so the bento CSS can size it', () => {
    const { container } = render(<SkeletonGrid />);
    // Sessions leads the tall row at span 6; every placeholder has some span.
    const hosts = Array.from(container.querySelectorAll('.bento-row .panel-host'));
    expect(hosts).toHaveLength(8);
    expect(hosts.every((h) => (h as HTMLElement).dataset.span)).toBe(true);
    const tallSpans = Array.from(
      container.querySelectorAll('.bento-row.row-tall .panel-host'),
    ).map((h) => (h as HTMLElement).dataset.span);
    expect(tallSpans).toEqual(['6', '3', '3']);
  });
});
