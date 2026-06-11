import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { AskUserQuestionCard } from './AskUserQuestionCard';
import type { ConversationBlock } from '../types/conversation';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;
const base = (over: Partial<Call> = {}): Call => ({
  kind: 'tool_call', name: 'AskUserQuestion', input_summary: '{}', preview: 'Q?',
  tool_use_id: 't1',
  result: { text: '', truncated: false, is_error: false },
  input: { questions: [{ question: 'Pick one?', header: 'Ambition', multiSelect: false,
    options: [{ label: 'Comprehensive', description: 'all of it' },
              { label: 'Frontend-only', description: 'less' }] }] },
  ...over,
});

describe('AskUserQuestionCard', () => {
  it('renders the question, options, and highlights the structured answer', () => {
    render(<AskUserQuestionCard call={base({ answers: { 'Pick one?': 'Comprehensive' } })} />);
    expect(screen.getByText('Pick one?')).toBeInTheDocument();
    expect(screen.getByText('Ambition')).toBeInTheDocument();
    const chosen = screen.getByText('Comprehensive').closest('.conv-ask-opt');
    expect(chosen?.className).toContain('conv-ask-opt--chosen');
    expect(screen.getByText('Frontend-only').closest('.conv-ask-opt')?.className)
      .not.toContain('conv-ask-opt--chosen');
  });

  it('falls back to parsing result.text when answers absent', () => {
    render(<AskUserQuestionCard call={base({
      answers: undefined,
      result: { text: 'Your questions have been answered: "Pick one?"="Frontend-only".',
                truncated: false, is_error: false } })} />);
    expect(screen.getByText('Frontend-only').closest('.conv-ask-opt')?.className)
      .toContain('conv-ask-opt--chosen');
  });

  it('renders a custom "Other" answer block when no option matches', () => {
    render(<AskUserQuestionCard call={base({ answers: { 'Pick one?': 'my own idea' } })} />);
    expect(screen.getByText(/my own idea/)).toBeInTheDocument();
    expect(screen.getByText(/your answer/i)).toBeInTheDocument();
  });

  it('renders default-expanded as a <details open>', () => {
    const { container } = render(<AskUserQuestionCard call={base()} />);
    const d = container.querySelector('details');
    expect(d).toBeTruthy();
    expect(d?.open).toBe(true);
  });
});
