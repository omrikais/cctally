import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { ProgressBar } from './ProgressBar';

// A5 — the headline 7d gauge is the one true single-value meter, so it
// carries `role="progressbar"` with a rounded value + a contextual label.
describe('<ProgressBar/>', () => {
  it('exposes progressbar semantics with the rounded value and a label', () => {
    render(<ProgressBar percent={42.6} label="7-day usage" />);
    const bar = screen.getByRole('progressbar', { name: '7-day usage' });
    expect(bar).toHaveAttribute('aria-valuenow', '43');
    expect(bar).toHaveAttribute('aria-valuemin', '0');
    expect(bar).toHaveAttribute('aria-valuemax', '100');
  });

  it('clamps a null percent to 0 without crashing', () => {
    render(<ProgressBar percent={null} label="7-day usage" />);
    const bar = screen.getByRole('progressbar', { name: '7-day usage' });
    expect(bar).toHaveAttribute('aria-valuenow', '0');
  });
});
