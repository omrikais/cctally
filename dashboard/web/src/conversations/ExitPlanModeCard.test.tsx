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

// #217 S3 E10#6 — hardened (still client-side) approve/reject/responded
// detection. The pre-hardening regex matched the bare substrings `approv` /
// `reject` ANYWHERE in result.text, so a free-text user RESPONSE that merely
// mentioned those words was mis-badged. The table below proves the canonical
// Claude Code strings still classify AND that the previously-misclassified
// free-text vector now falls through to a neutral `Responded`.
describe('ExitPlanModeCard — outcome detection table (#217 S3 E10#6)', () => {
  const result = (text: string, is_error = false) => ({ text, truncated: false, is_error });
  // The badge label rendered for a given result.text.
  const badgeOf = (text: string, is_error = false): string => {
    render(<ExitPlanModeCard call={base({ result: result(text, is_error) })} />);
    if (screen.queryByText('Approved')) return 'Approved';
    if (screen.queryByText('Rejected')) return 'Rejected';
    if (screen.queryByText('Responded')) return 'Responded';
    return 'none';
  };

  it('canonical approval string → Approved', () => {
    expect(badgeOf('User has approved your plan. You can now start coding.')).toBe('Approved');
  });

  it('canonical rejection string → Rejected', () => {
    expect(badgeOf("The user doesn't want to proceed with this tool use.")).toBe('Rejected');
  });

  it('is_error always short-circuits to Rejected', () => {
    expect(badgeOf('anything at all', true)).toBe('Rejected');
  });

  // The smoking-gun vector: a genuine free-text user response that contains the
  // word "reject" (or "approve") as prose. Pre-hardening this was mis-badged
  // Rejected/Approved; it must now be neutral Responded.
  it('free-text response mentioning "rejected" in prose → Responded (was mis-badged Rejected)', () => {
    expect(
      badgeOf("I rejected that earlier idea, but let's keep this one — go ahead and refactor utils.ts."),
    ).toBe('Responded');
  });

  it('free-text response mentioning "approve" in prose → Responded (was mis-badged Approved)', () => {
    expect(badgeOf('Can you get my manager to approve the new endpoint name first?')).toBe('Responded');
  });
});
