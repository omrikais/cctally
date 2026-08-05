import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { OutcomeBadge, OutcomeEvidence } from './OutcomeBadge';
import type { ToolOutcome } from '../types/conversation';

const outcome = (over: Partial<ToolOutcome> = {}): ToolOutcome => ({
  status: 'completed', exit_code: null, wall_time_seconds: null, ...over,
});

describe('OutcomeBadge', () => {
  it('emits a wide and a narrow wording for every published status', () => {
    for (const status of ['completed', 'failed', 'running', 'unknown'] as const) {
      const { container } = render(<OutcomeBadge outcome={outcome({ status })} />);
      expect(container.querySelector('.conv-status-wide')?.textContent).toBeTruthy();
      expect(container.querySelector('.conv-status-narrow')?.textContent).toBeTruthy();
    }
  });

  it('renders the neutral wording rather than crashing on an unmodelled status', () => {
    // The adapter normalizes today, but this component is the last stop before
    // the reader and an unguarded lookup here throws inside the render tree,
    // which blanks the whole conversation rather than one chip.
    const { container } = render(
      <OutcomeBadge outcome={{ status: 'future-state', exit_code: null, wall_time_seconds: null } as unknown as ToolOutcome} />,
    );
    expect(container.querySelector('.conv-outcome')).toBeTruthy();
    expect(container.querySelector('.conv-status-wide')?.textContent).toBe('outcome unknown');
  });
});

// #463 S3 F11a — the recovered evidence. A `title` attribute has no touch
// affordance, so on the 390px viewport the spec targets the exit code and wall
// time were unreachable. They ride the expanded card body as well.
describe('OutcomeEvidence', () => {
  it('states the exit code and the wall time', () => {
    const { container } = render(<OutcomeEvidence outcome={outcome({ exit_code: 3, wall_time_seconds: 1.5 })} />);
    expect(container.querySelector('.conv-outcome-evidence')?.textContent).toBe('exit 3 · 1.5s');
  });

  it('states whichever half the grammar supplied', () => {
    const codeOnly = render(<OutcomeEvidence outcome={outcome({ exit_code: 0 })} />);
    expect(codeOnly.container.querySelector('.conv-outcome-evidence')?.textContent).toBe('exit 0');
    const timeOnly = render(<OutcomeEvidence outcome={outcome({ wall_time_seconds: 0.5 })} />);
    expect(timeOnly.container.querySelector('.conv-outcome-evidence')?.textContent).toBe('0.5s');
  });

  it('renders nothing when the grammar supplied neither', () => {
    const { container } = render(<OutcomeEvidence outcome={outcome()} />);
    expect(container.querySelector('.conv-outcome-evidence')).toBeNull();
  });
});
