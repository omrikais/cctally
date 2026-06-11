import { render, screen, fireEvent } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ExitPlanModeCard } from './ExitPlanModeCard';
import type { ConversationBlock } from '../types/conversation';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;
const base = (over: Partial<Call> = {}): Call => ({
  kind: 'tool_call', name: 'ExitPlanMode', input_summary: '{}', preview: 'plan',
  tool_use_id: 't1',
  result: { text: 'User has approved your plan.', truncated: false, is_error: false },
  input: { plan: '## Plan\n\n- step one\n- step two' }, ...over,
});

// A plan long enough to trip the clamp heuristic (>24 lines).
const LONG_PLAN = '## Plan\n\n' + Array.from({ length: 30 }, (_, i) => `- step ${i + 1}`).join('\n');
const long = (over: Partial<Call> = {}): Call => base({ input: { plan: LONG_PLAN }, ...over });

describe('ExitPlanModeCard', () => {
  it('renders the plan markdown (heading + list) default-expanded', () => {
    const { container } = render(<ExitPlanModeCard call={base()} />);
    expect(container.querySelector('details')?.open).toBe(true);
    expect(screen.getByRole('heading', { name: 'Plan' })).toBeInTheDocument();
    expect(screen.getByText('step one')).toBeInTheDocument();
  });
  it('shows Approved on an approval result', () => {
    render(<ExitPlanModeCard call={base()} />);
    expect(screen.getByText(/approved/i)).toBeInTheDocument();
  });
  it('shows Rejected on an error/reject result', () => {
    render(<ExitPlanModeCard call={base({
      result: { text: "The user doesn't want to proceed.", truncated: false, is_error: true } })} />);
    expect(screen.getByText(/rejected/i)).toBeInTheDocument();
  });
  it('shows a neutral Responded for ambiguous, never Approved', () => {
    render(<ExitPlanModeCard call={base({
      result: { text: 'ok', truncated: false, is_error: false } })} />);
    expect(screen.queryByText(/approved/i)).toBeNull();
    expect(screen.getByText(/responded/i)).toBeInTheDocument();
  });
  it('no badge when there is no result (awaiting)', () => {
    render(<ExitPlanModeCard call={base({ result: null })} />);
    expect(screen.queryByText(/approved|rejected|responded/i)).toBeNull();
  });
  it('shows the input_truncated hint', () => {
    render(<ExitPlanModeCard call={base({ input_truncated: true })} />);
    expect(screen.getByText(/plan input truncated/i)).toBeInTheDocument();
  });

  it('a SHORT plan renders with no clamp class and no "Show full plan" button', () => {
    const { container } = render(<ExitPlanModeCard call={base()} />);
    expect(screen.queryByRole('button', { name: /show full plan/i })).toBeNull();
    const md = container.querySelector('.conv-plan-md');
    expect(md).toBeTruthy();
    expect(md?.className).not.toContain('conv-plan-md--clamp');
  });

  it('a LONG plan renders the clamp class and the "Show full plan" button', () => {
    const { container } = render(<ExitPlanModeCard call={long()} />);
    expect(screen.getByRole('button', { name: /show full plan/i })).toBeInTheDocument();
    expect(container.querySelector('.conv-plan-md')?.className).toContain('conv-plan-md--clamp');
  });

  it('clicking "Show full plan" on a long plan removes the clamp and the button', () => {
    const { container } = render(<ExitPlanModeCard call={long()} />);
    fireEvent.click(screen.getByRole('button', { name: /show full plan/i }));
    expect(container.querySelector('.conv-plan-md')?.className).not.toContain('conv-plan-md--clamp');
    expect(screen.queryByRole('button', { name: /show full plan/i })).toBeNull();
  });
});
