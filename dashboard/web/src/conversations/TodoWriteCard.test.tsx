import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { TodoWriteCard } from './TodoWriteCard';
import type { ConversationBlock } from '../types/conversation';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;
const todos = [
  { content: 'Scaffold', status: 'completed', activeForm: 'Scaffolding' },
  { content: 'Wire cache', status: 'completed', activeForm: 'Wiring' },
  { content: 'Create types', status: 'in_progress', activeForm: 'Creating types' },
  { content: 'Resolver', status: 'pending', activeForm: 'Resolving' },
];
const base = (over: Partial<Call> = {}): Call => ({
  kind: 'tool_call', name: 'TodoWrite', input_summary: '{}', preview: 'x',
  tool_use_id: 't1', result: { text: 'ok', truncated: false, is_error: false },
  input: { todos }, ...over,
});

describe('TodoWriteCard', () => {
  it('collapsed by default with a progress + current-item preview', () => {
    const { container } = render(<TodoWriteCard call={base()} />);
    expect(container.querySelector('details')?.open).toBe(false);
    // "2 / 4" appears in the summary frac AND the body title; "Create types"
    // appears in the summary preview AND the body item. Both are correct (the
    // body is in the DOM even while collapsed) — scope each assertion to the
    // collapsed-summary element so the queries aren't ambiguous.
    expect(container.querySelector('.conv-todo-frac')?.textContent?.replace(/\s+/g, ' '))
      .toContain('2 / 4');
    expect(container.querySelector('.conv-todo-preview')?.textContent)
      .toContain('Create types'); // current in_progress item
  });
  it('expanded list marks completed (strikethrough class) + in-progress + pending', () => {
    const { container } = render(<TodoWriteCard call={base()} />);
    expect(container.querySelector('.conv-todo-item--done')).toBeTruthy();
    expect(container.querySelector('.conv-todo-item--cur')).toBeTruthy();
    expect(container.querySelector('.conv-todo-item--todo')).toBeTruthy();
  });
  it('unknown status is treated as pending (defensive)', () => {
    const { container } = render(<TodoWriteCard call={base({
      input: { todos: [{ content: 'weird', status: 'bogus', activeForm: 'w' }] } })} />);
    expect(container.querySelector('.conv-todo-item--todo')).toBeTruthy();
  });
});
