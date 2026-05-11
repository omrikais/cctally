// Knobs — plan §M1.13 contract: each control toggles the right field
// of ShareOptions. Particularly: "Anon on export" maps INVERSELY to
// reveal_projects (checked ⇒ reveal_projects=false).
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { Knobs } from './Knobs';
import type { ShareOptions } from './types';

// `reveal_projects: true` here diverges from production
// `defaultShareOptions()` (which is `false` per spec Q7 / §6.3) so the
// inverse-mapping tests below can exercise the checkbox from BOTH
// states; the explicit-inverse variant builds `reveal_projects: false`
// inline.
function defaults(): ShareOptions {
  return {
    format: 'md',
    theme: 'light',
    reveal_projects: true,
    no_branding: false,
    top_n: 5,
    period: { kind: 'current' },
    project_allowlist: null,
    show_chart: true,
    show_table: true,
  };
}

describe('<Knobs>', () => {
  it('changing Period select dispatches onChange with new period.kind', () => {
    const onChange = vi.fn();
    render(<Knobs options={defaults()} onChange={onChange} />);
    fireEvent.change(screen.getByLabelText(/period/i), {
      target: { value: 'previous' },
    });
    expect(onChange).toHaveBeenCalledTimes(1);
    const next = onChange.mock.calls[0][0] as ShareOptions;
    expect(next.period.kind).toBe('previous');
  });

  it('selecting Dark theme dispatches theme=dark', () => {
    const onChange = vi.fn();
    render(<Knobs options={defaults()} onChange={onChange} />);
    fireEvent.click(screen.getByLabelText(/dark/i));
    const next = onChange.mock.calls[0][0] as ShareOptions;
    expect(next.theme).toBe('dark');
  });

  it('Top-N number input updates top_n', () => {
    const onChange = vi.fn();
    render(<Knobs options={defaults()} onChange={onChange} />);
    fireEvent.change(screen.getByLabelText(/top-n rows/i), {
      target: { value: '12' },
    });
    const next = onChange.mock.calls[0][0] as ShareOptions;
    expect(next.top_n).toBe(12);
  });

  it('clamps Top-N to a minimum of 1 (client-side validation)', () => {
    const onChange = vi.fn();
    render(<Knobs options={defaults()} onChange={onChange} />);
    fireEvent.change(screen.getByLabelText(/top-n rows/i), {
      target: { value: '0' },
    });
    const next = onChange.mock.calls[0][0] as ShareOptions;
    expect(next.top_n).toBe(1);
  });

  it('Show chart toggle flips show_chart', () => {
    const onChange = vi.fn();
    render(<Knobs options={defaults()} onChange={onChange} />);
    fireEvent.click(screen.getByLabelText(/show chart/i));
    const next = onChange.mock.calls[0][0] as ShareOptions;
    expect(next.show_chart).toBe(false);
  });

  it('Anon on export (checked) sets reveal_projects=false', () => {
    const onChange = vi.fn();
    render(<Knobs options={defaults()} onChange={onChange} />);
    // Default reveal_projects=true ⇒ checkbox unchecked. Click checks it.
    fireEvent.click(screen.getByLabelText(/anonymize project names on export/i));
    const next = onChange.mock.calls[0][0] as ShareOptions;
    expect(next.reveal_projects).toBe(false);
  });

  it('Anon on export (unchecked) sets reveal_projects=true', () => {
    const opts: ShareOptions = { ...defaults(), reveal_projects: false };
    const onChange = vi.fn();
    render(<Knobs options={opts} onChange={onChange} />);
    fireEvent.click(screen.getByLabelText(/anonymize project names on export/i));
    const next = onChange.mock.calls[0][0] as ShareOptions;
    expect(next.reveal_projects).toBe(true);
  });

  it('Custom period reveals start/end date inputs', () => {
    const opts: ShareOptions = { ...defaults(), period: { kind: 'custom' } };
    const onChange = vi.fn();
    render(<Knobs options={opts} onChange={onChange} />);
    expect(screen.getByLabelText(/custom period start date/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/custom period end date/i)).toBeInTheDocument();
  });
});
