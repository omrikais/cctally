import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { CumulativeCostChip } from './CumulativeCostChip';

describe('CumulativeCostChip', () => {
  it('shows $cumulative / $total with a progress fraction', () => {
    const { container } = render(<CumulativeCostChip cumulative={0.83} total={2.41} approx={false} />);
    expect(container.textContent).toContain('$0.83');
    expect(container.textContent).toContain('$2.41');
    expect(container.textContent).not.toContain('~');
    const bar = container.querySelector('.conv-cumcost-fill') as HTMLElement;
    expect(bar.style.getPropertyValue('--conv-cumcost-frac')).toMatch(/0\.34/); // 0.83/2.41
  });
  it('prefixes ~ when approx (earlier pages unloaded)', () => {
    const { container } = render(<CumulativeCostChip cumulative={0.83} total={2.41} approx={true} />);
    expect(container.textContent).toContain('~$0.83');
  });
  it('renders nothing when total is 0', () => {
    const { container } = render(<CumulativeCostChip cumulative={0} total={0} approx={false} />);
    expect(container.firstChild).toBeNull();
  });
});
