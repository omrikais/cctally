import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { AlertsEmptyGauge } from './AlertsEmptyGauge';

describe('AlertsEmptyGauge (#265 A)', () => {
  it('keeps the canonical gauge anatomy when usedPct is null', () => {
    const { container } = render(<AlertsEmptyGauge usedPct={null} thresholds={[90, 95]} />);
    expect(container.querySelector('.panel-empty')).toBeNull();
    expect(container.querySelector('.ra-gauge')).not.toBeNull();
    expect(container.querySelector('.ra-gauge-hero')?.textContent).toBe('—');
    expect((container.querySelector('.ra-gauge-fill') as HTMLElement).style.width).toBe('0%');
    expect(container.textContent).toContain('90% / 95%');
  });

  it('keeps the canonical head and changes its copy at the lowest threshold', () => {
    const under = render(<AlertsEmptyGauge usedPct={40} thresholds={[90, 95]} />);
    expect(under.container.querySelector('.ra-gauge-head')).not.toBeNull();
    expect(under.container.textContent).toContain('well under the line');
    expect(under.container.querySelector('.ra-gauge-hero')?.textContent).toBe('40%');

    const over = render(<AlertsEmptyGauge usedPct={92} thresholds={[90, 95]} />);
    expect(over.container.querySelector('.ra-gauge-head')).not.toBeNull();
    expect(over.container.querySelector('.ra-gauge-head')?.textContent).toContain('No alerts yet');
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
