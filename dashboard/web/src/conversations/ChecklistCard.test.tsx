import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ChecklistCard } from './ChecklistCard';
import type { ChecklistTodo } from '../types/conversation';

const todos: ChecklistTodo[] = [
  { content: 'Scaffold', status: 'completed', activeForm: 'Scaffolding' },
  { content: 'Wire cache', status: 'completed', activeForm: 'Wiring' },
  { content: 'Create types', status: 'in_progress', activeForm: 'Creating types' },
  { content: 'Resolver', status: 'pending', activeForm: 'Resolving' },
];

describe('ChecklistCard (shared approved checklist visual)', () => {
  it('collapsed by default with a progress + current-item preview, labelled by `label`', () => {
    const { container } = render(<ChecklistCard todos={todos} label="Tasks" />);
    expect(container.querySelector('details')?.open).toBe(false);
    expect(container.querySelector('.conv-chip-name')?.textContent).toBe('Tasks');
    expect(container.querySelector('.conv-todo-frac')?.textContent?.replace(/\s+/g, ' '))
      .toContain('2 / 4');
    expect(container.querySelector('.conv-todo-preview')?.textContent)
      .toContain('Create types'); // current in_progress item
  });

  it('expanded list marks completed + in-progress + pending with the status classes', () => {
    const { container } = render(<ChecklistCard todos={todos} label="Tasks" />);
    expect(container.querySelector('.conv-todo-item--done')).toBeTruthy();
    expect(container.querySelector('.conv-todo-item--cur')).toBeTruthy();
    expect(container.querySelector('.conv-todo-item--todo')).toBeTruthy();
    // the progress bar tracks done/total (2 of 4 => 50%)
    const bar = container.querySelector('.conv-todo-bar i') as HTMLElement | null;
    expect(bar?.style.width).toBe('50%');
  });

  it('unknown status is treated as pending (defensive)', () => {
    const { container } = render(
      <ChecklistCard todos={[{ content: 'weird', status: 'bogus' }]} label="Tasks" />,
    );
    expect(container.querySelector('.conv-todo-item--todo')).toBeTruthy();
  });

  it('empty list renders "no todos" without crashing (0 / 0)', () => {
    const { container } = render(<ChecklistCard todos={[]} label="Tasks" />);
    expect(container.querySelector('.conv-todo-preview')?.textContent).toContain('no todos');
    expect(container.querySelector('.conv-todo-frac')?.textContent?.replace(/\s+/g, ' '))
      .toContain('0 / 0');
    expect(container.querySelectorAll('.conv-todo-item')).toHaveLength(0);
  });

  it('renders the supplied label verbatim (parameterized chip name)', () => {
    const { container } = render(<ChecklistCard todos={todos} label="TodoWrite" />);
    expect(container.querySelector('.conv-chip-name')?.textContent).toBe('TodoWrite');
  });
});
