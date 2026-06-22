import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { CumulativeCostChip } from './CumulativeCostChip';

describe('CumulativeCostChip', () => {
  it('shows $cumulative / $total with a progress fraction', () => {
    const { container } = render(<CumulativeCostChip cumulative={0.60} total={2.40} approx={false} />);
    expect(container.textContent).toContain('$0.60');
    expect(container.textContent).toContain('$2.40');
    expect(container.textContent).not.toContain('~');
    const bar = container.querySelector('.conv-cumcost-fill') as HTMLElement;
    // 0.60 / 2.40 = 0.25 exactly — assert the exact frac, not a loose substring
    // (#226: a wrong-but-prefix-matching value like 0.259 must NOT pass).
    expect(bar.style.getPropertyValue('--conv-cumcost-frac')).toBe('0.25');
  });
  it('prefixes ~ when approx (earlier pages unloaded)', () => {
    const { container } = render(<CumulativeCostChip cumulative={0.83} total={2.41} approx={true} />);
    expect(container.textContent).toContain('~$0.83');
  });
  it('renders nothing when total is 0', () => {
    const { container } = render(<CumulativeCostChip cumulative={0} total={0} approx={false} />);
    expect(container.firstChild).toBeNull();
  });
  // #226 (#217 S6 I-1 P3) — suppress the transient $0.00 chip before the
  // scroll-sync establishes a current turn (pending), but keep the honest
  // $0.00 once a turn IS established (a genuinely $0 turn scrolled past).
  it('hides the transient $0 chip while pending (no current turn yet)', () => {
    const { container } = render(<CumulativeCostChip cumulative={0} total={2.40} approx={false} pending={true} />);
    expect(container.firstChild).toBeNull();
  });
  it('still renders an honest $0.00 once a turn is established (not pending)', () => {
    const { container } = render(<CumulativeCostChip cumulative={0} total={2.40} approx={false} pending={false} />);
    expect(container.textContent).toContain('$0.00');
    expect(container.textContent).toContain('$2.40');
  });
});
