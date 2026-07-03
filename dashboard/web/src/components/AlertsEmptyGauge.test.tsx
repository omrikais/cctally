import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { AlertsEmptyGauge } from './AlertsEmptyGauge';

describe('AlertsEmptyGauge (#265 A)', () => {
  it('renders the .panel-empty fallback (no gauge) when usedPct is null', () => {
    const { container } = render(<AlertsEmptyGauge usedPct={null} thresholds={[90, 95]} />);
    const empty = container.querySelector('.panel-empty');
    expect(empty).not.toBeNull();
    expect(container.querySelector('.ra-gauge')).toBeNull();
    expect(empty?.textContent).toContain('90% / 95%');
  });

  it('shows the ✓-head only when usedPct is below the lowest threshold', () => {
    const under = render(<AlertsEmptyGauge usedPct={40} thresholds={[90, 95]} />);
    expect(under.container.querySelector('.ra-gauge-head')).not.toBeNull();
    expect(under.container.textContent).toContain('well under the line');
    expect(under.container.querySelector('.ra-gauge-hero')?.textContent).toBe('40%');

    const over = render(<AlertsEmptyGauge usedPct={92} thresholds={[90, 95]} />);
    expect(over.container.querySelector('.ra-gauge-head')).toBeNull();
    expect(over.container.querySelector('.ra-gauge-hero')?.textContent).toBe('92%');
  });

  it('renders one tick per threshold (amber floor / red ceiling) and a fill at used%', () => {
    const { container } = render(<AlertsEmptyGauge usedPct={40} thresholds={[90, 95]} />);
    const ticks = container.querySelectorAll('.ra-gauge-tick');
    expect(Array.from(ticks).map((t) => (t as HTMLElement).style.left)).toEqual(['90%', '95%']);
    expect(container.querySelector('.ra-gauge-tick.tick-amber')).not.toBeNull();
    expect(container.querySelector('.ra-gauge-tick.tick-red')).not.toBeNull();
    expect((container.querySelector('.ra-gauge-fill') as HTMLElement).style.width).toBe('40%');
  });

  it('adds the ra-gauge--compact modifier only when compact', () => {
    const plain = render(<AlertsEmptyGauge usedPct={40} thresholds={[90, 95]} />);
    expect(plain.container.querySelector('.ra-gauge--compact')).toBeNull();
    const compact = render(<AlertsEmptyGauge usedPct={40} thresholds={[90, 95]} compact />);
    expect(compact.container.querySelector('.ra-gauge.ra-gauge--compact')).not.toBeNull();
  });
});
