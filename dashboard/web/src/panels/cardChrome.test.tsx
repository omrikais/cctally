// #247 S1 — card-chrome contract. The de-colored panels must let their
// header <h2> inherit the neutral `.panel-header h2` color instead of
// pinning a decorative accent inline. (The chrome goes neutral; accent is
// reserved for state/signal — see the design spec D1/D4/D5.) JSDOM can
// read inline `element.style.color`, so a surviving `style={{ color:
// 'var(--accent-*)' }}` shows up as a non-empty string here.
import { render } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { CurrentWeekPanel } from './CurrentWeekPanel';
import { ForecastPanel } from './ForecastPanel';
import { _resetForTests } from '../store/store';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('#247 S1 card-chrome contract', () => {
  it('header <h2> carries no inline decorative accent color (Current Week)', () => {
    const { container } = render(<CurrentWeekPanel />);
    const h2 = container.querySelector('.panel-header h2')!;
    expect((h2 as HTMLElement).style.color).toBe('');
  });
  it('header <h2> carries no inline decorative accent color (Forecast)', () => {
    const { container } = render(<ForecastPanel />);
    const h2 = container.querySelector('.panel-header h2')!;
    expect((h2 as HTMLElement).style.color).toBe('');
  });
  it('header icon carries no inline decorative accent color (Current Week)', () => {
    const { container } = render(<CurrentWeekPanel />);
    const icon = container.querySelector('.panel-header svg.icon')!;
    expect((icon as unknown as SVGElement & { style: CSSStyleDeclaration }).style.color).toBe('');
  });
});
