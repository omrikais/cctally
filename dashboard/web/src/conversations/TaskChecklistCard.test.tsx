import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { TaskChecklistCard } from './TaskChecklistCard';
import type { ConversationBlock } from '../types/conversation';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

const base = (over: Partial<Call> = {}): Call => ({
  kind: 'tool_call', name: 'TaskCreate', input_summary: '{}', preview: 'x',
  tool_use_id: 't1', result: { text: 'ok', truncated: false, is_error: false },
  task_snapshot: [
    { content: 'Alpha', status: 'completed', activeForm: 'Alphaing' },
    { content: 'Beta', status: 'in_progress', activeForm: 'Betaing' },
    { content: 'Gamma', status: 'pending', activeForm: 'Gammaing' },
  ],
  ...over,
});

describe('TaskChecklistCard (Task* adapter)', () => {
  it('renders the snapshot with the "Tasks" label, progress, and status classes', () => {
    const { container } = render(<TaskChecklistCard call={base()} />);
    expect(container.querySelector('.conv-chip-name')?.textContent).toBe('Tasks');
    // 1 of 3 done => "1 / 3" and a 33% bar
    expect(container.querySelector('.conv-todo-frac')?.textContent?.replace(/\s+/g, ' '))
      .toContain('1 / 3');
    const bar = container.querySelector('.conv-todo-bar i') as HTMLElement | null;
    expect(bar?.style.width).toBe('33%');
    expect(container.querySelector('.conv-todo-item--done')).toBeTruthy();
    expect(container.querySelector('.conv-todo-item--cur')).toBeTruthy();
    expect(container.querySelector('.conv-todo-item--todo')).toBeTruthy();
  });

  it('an undefined task_snapshot renders "no todos" without crashing', () => {
    const { container } = render(<TaskChecklistCard call={base({ task_snapshot: undefined })} />);
    expect(container.querySelector('.conv-chip-name')?.textContent).toBe('Tasks');
    expect(container.querySelector('.conv-todo-preview')?.textContent).toContain('no todos');
    expect(container.querySelectorAll('.conv-todo-item')).toHaveLength(0);
  });

  it('an empty task_snapshot renders "no todos" (0 / 0)', () => {
    const { container } = render(<TaskChecklistCard call={base({ task_snapshot: [] })} />);
    expect(container.querySelector('.conv-todo-frac')?.textContent?.replace(/\s+/g, ' '))
      .toContain('0 / 0');
    expect(container.querySelectorAll('.conv-todo-item')).toHaveLength(0);
  });
});
