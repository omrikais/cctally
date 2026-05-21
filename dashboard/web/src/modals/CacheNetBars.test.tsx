// CacheNetBars regression tests for issue #77 P2-4 (mini variant) +
// guards on the existing large variant.
import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { CacheNetBars } from './CacheNetBars';
import type { CacheReportDailyRow } from '../types/envelope';

function row(date: string, net: number): CacheReportDailyRow {
  return {
    date,
    cache_hit_percent: 80,
    input_tokens: 1_000,
    output_tokens: 100,
    cache_creation_tokens: 50,
    cache_read_tokens: 800,
    saved_usd: Math.max(0, net) + 0.1,
    wasted_usd: 0.1,
    net_usd: net,
    anomaly_triggered: false,
    anomaly_reasons: [] as string[],
  };
}

function sampleDays(): CacheReportDailyRow[] {
  // Newest-first (envelope convention). Mix positives + negatives so
  // we can verify per-bar sign attribution.
  return [
    row('2026-05-20', 1.20),  // today
    row('2026-05-19', 0.80),
    row('2026-05-18', -0.30),
    row('2026-05-17', 1.50),
    row('2026-05-16', 0.60),
    row('2026-05-15', 0.40),
    row('2026-05-14', -0.10),
    row('2026-05-13', 0.90),
    row('2026-05-12', 0.70),
    row('2026-05-11', 0.50),
    row('2026-05-10', 0.20),
    row('2026-05-09', 0.30),
    row('2026-05-08', 0.10),
    row('2026-05-07', 0.05),
  ];
}

describe('<CacheNetBars size="mini" /> (issue #77 P2-4)', () => {
  it('renders 14 bars when given 14 daily rows', () => {
    const { container } = render(
      <CacheNetBars days={sampleDays()} size="mini" />,
    );
    const bars = container.querySelectorAll('[data-testid="crm-netbar-mini"]');
    expect(bars.length).toBe(14);
  });

  it('renders width=100% / height=100% with preserveAspectRatio=none for edge-to-edge fill', () => {
    const { container } = render(
      <CacheNetBars days={sampleDays()} size="mini" />,
    );
    const svg = container.querySelector('svg.cr-netbars-mini') as SVGSVGElement;
    expect(svg).toBeTruthy();
    expect(svg.getAttribute('width')).toBe('100%');
    expect(svg.getAttribute('height')).toBe('100%');
    expect(svg.getAttribute('preserveAspectRatio')).toBe('none');
    // ViewBox preserves the bar-math coordinates (0..272 x, 0..28 y).
    expect(svg.getAttribute('viewBox')).toBe('0 0 272 28');
  });

  it('omits the section/chart-frame chrome the modal uses', () => {
    const { container } = render(
      <CacheNetBars days={sampleDays()} size="mini" />,
    );
    expect(container.querySelector('.crm-section')).toBeNull();
    expect(container.querySelector('.crm-chart-frame')).toBeNull();
    expect(container.querySelector('.crm-section-head')).toBeNull();
  });

  it('omits SVG axis-label <text> nodes (no M-D / "Today" labels)', () => {
    const { container } = render(
      <CacheNetBars days={sampleDays()} size="mini" />,
    );
    expect(container.querySelectorAll('svg.cr-netbars-mini text').length).toBe(0);
  });

  it('colors positive-net bars green and negative-net bars amber', () => {
    const { container } = render(
      <CacheNetBars days={sampleDays()} size="mini" />,
    );
    const bars = container.querySelectorAll<SVGRectElement>(
      '[data-testid="crm-netbar-mini"]',
    );
    // Sample fixture has 12 positives + 2 negatives.
    const positives = Array.from(bars).filter(
      (b) => b.getAttribute('data-sign') === 'pos',
    );
    const negatives = Array.from(bars).filter(
      (b) => b.getAttribute('data-sign') === 'neg',
    );
    expect(positives.length).toBe(12);
    expect(negatives.length).toBe(2);
    positives.forEach((b) =>
      expect(b.getAttribute('fill')).toBe('var(--accent-green)'),
    );
    negatives.forEach((b) =>
      expect(b.getAttribute('fill')).toBe('var(--accent-amber)'),
    );
  });

  it('renders an empty SVG (no bars) when days is empty', () => {
    const { container } = render(
      <CacheNetBars days={[]} size="mini" />,
    );
    const svg = container.querySelector('svg.cr-netbars-mini') as SVGSVGElement;
    expect(svg).toBeTruthy();
    expect(svg.getAttribute('aria-label')).toBe('no data');
    expect(container.querySelectorAll('[data-testid="crm-netbar-mini"]').length).toBe(0);
  });
});

describe('<CacheNetBars size="large" /> still wraps in section chrome', () => {
  it('renders the section header and chart-frame for the modal', () => {
    const { container } = render(
      <CacheNetBars days={sampleDays()} size="large" />,
    );
    expect(container.querySelector('.crm-section')).toBeTruthy();
    expect(container.querySelector('.crm-section-head.crm-sh-net')).toBeTruthy();
    expect(container.querySelector('.crm-chart-frame.netbars')).toBeTruthy();
  });

  it('renders 14 bars with M-D / "Today" axis labels', () => {
    const { container } = render(
      <CacheNetBars days={sampleDays()} size="large" />,
    );
    expect(container.querySelectorAll('[data-testid="crm-netbar"]').length).toBe(14);
    const texts = Array.from(
      container.querySelectorAll('svg text'),
    ).map((t) => t.textContent);
    expect(texts).toContain('Today');
    // First (oldest) day in the reversed order is 2026-05-07 → "05-07".
    expect(texts).toContain('05-07');
  });

  it('shows the empty placeholder copy when days is empty', () => {
    const { container } = render(<CacheNetBars days={[]} size="large" />);
    expect(container.textContent).toMatch(/No daily activity to render/i);
  });
});
