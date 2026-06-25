import { render, screen, fireEvent, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { FocusCompactMenu } from './FocusCompactMenu';

// #228 S3 C2 — the mobile compact focus picker. One "Focus: <active> ▾" dropdown
// that lists the four primary modes AND the Edits/Bash/per-Subagent sub-options
// in a single flat list. `onSelect` is the dispatch boundary (the reader wires
// SET_CONV_FOCUS_MODE).
afterEach(() => {
  vi.restoreAllMocks();
});

describe('FocusCompactMenu', () => {
  it('the trigger label reflects the active mode', () => {
    const { rerender } = render(<FocusCompactMenu focusMode="all" subagents={[]} onSelect={vi.fn()} />);
    expect(screen.getByRole('button', { name: /focus: all/i })).not.toBeNull();
    rerender(<FocusCompactMenu focusMode="errors" subagents={[]} onSelect={vi.fn()} />);
    expect(screen.getByRole('button', { name: /focus: errors/i })).not.toBeNull();
  });

  it('lists the four primary modes plus Edits and Bash', () => {
    render(<FocusCompactMenu focusMode="all" subagents={[]} onSelect={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /focus:/i }));
    const menu = screen.getByRole('menu', { name: /focus mode/i });
    for (const name of ['All', 'Chat', 'Prompts', 'Errors', 'Edits', 'Bash']) {
      expect(within(menu).getByRole('menuitemradio', { name: new RegExp(name) })).not.toBeNull();
    }
  });

  it('selecting a primary mode invokes onSelect with that mode and closes', () => {
    const onSelect = vi.fn();
    render(<FocusCompactMenu focusMode="all" subagents={[]} onSelect={onSelect} />);
    fireEvent.click(screen.getByRole('button', { name: /focus:/i }));
    fireEvent.click(screen.getByRole('menuitemradio', { name: /Prompts/ }));
    expect(onSelect).toHaveBeenCalledWith('prompts');
    expect(screen.queryByRole('menu', { name: /focus mode/i })).toBeNull();
  });

  it('selecting Edits invokes onSelect with edits', () => {
    const onSelect = vi.fn();
    render(<FocusCompactMenu focusMode="all" subagents={[]} onSelect={onSelect} />);
    fireEvent.click(screen.getByRole('button', { name: /focus:/i }));
    fireEvent.click(screen.getByRole('menuitemradio', { name: /Edits/ }));
    expect(onSelect).toHaveBeenCalledWith('edits');
  });

  it('lists per-subagent options and emits subagent:<key>', () => {
    const onSelect = vi.fn();
    render(
      <FocusCompactMenu
        focusMode="all"
        subagents={[{ key: 'k1', label: 'Explore' }, { key: 'k2', label: '' }]}
        onSelect={onSelect}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /focus:/i }));
    fireEvent.click(screen.getByRole('menuitemradio', { name: /Explore/ }));
    expect(onSelect).toHaveBeenCalledWith('subagent:k1');
  });

  it('marks the active mode aria-checked', () => {
    render(<FocusCompactMenu focusMode="chat" subagents={[]} onSelect={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /focus:/i }));
    const chat = screen.getByRole('menuitemradio', { name: /Chat/ });
    expect(chat.getAttribute('aria-checked')).toBe('true');
    const all = screen.getByRole('menuitemradio', { name: /All/ });
    expect(all.getAttribute('aria-checked')).toBe('false');
  });

  it('shows the error-count badge on Errors when > 0', () => {
    render(<FocusCompactMenu focusMode="all" subagents={[]} onSelect={vi.fn()} errorCount={3} />);
    fireEvent.click(screen.getByRole('button', { name: /focus:/i }));
    const errors = screen.getByRole('menuitemradio', { name: /Errors/ });
    expect(within(errors).getByText('3')).not.toBeNull();
  });

  it('Escape closes and restores focus to the trigger', () => {
    render(<FocusCompactMenu focusMode="all" subagents={[]} onSelect={vi.fn()} />);
    const trigger = screen.getByRole('button', { name: /focus:/i });
    // Focus the trigger before opening (a JSDOM click does not move focus) so
    // restoreRef captures it for the Escape focus-return.
    trigger.focus();
    fireEvent.click(trigger);
    const menu = screen.getByRole('menu', { name: /focus mode/i });
    fireEvent.keyDown(menu, { key: 'Escape' });
    expect(screen.queryByRole('menu', { name: /focus mode/i })).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it('#238 R3 — dismisses on outside pointerdown without refocusing the trigger', () => {
    render(
      <div>
        <FocusCompactMenu focusMode="all" subagents={[]} onSelect={vi.fn()} />
        <button data-testid="outside">outside</button>
      </div>,
    );
    fireEvent.click(screen.getByRole('button', { name: /focus:/i }));
    expect(screen.getByRole('button', { name: /focus:/i })).toHaveAttribute('aria-expanded', 'true');
    fireEvent.pointerDown(screen.getByTestId('outside'));
    expect(screen.getByRole('button', { name: /focus:/i })).toHaveAttribute('aria-expanded', 'false');
    // Silent dismiss: focus must NOT have been forced back onto the trigger.
    expect(document.activeElement).not.toBe(screen.getByRole('button', { name: /focus:/i }));
  });

  it('#238 R3 — a pointerdown INSIDE the menu does not dismiss it', () => {
    render(<FocusCompactMenu focusMode="all" subagents={[]} onSelect={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /focus:/i }));
    fireEvent.pointerDown(screen.getByRole('menuitemradio', { name: /All/ }));
    expect(screen.getByRole('button', { name: /focus:/i })).toHaveAttribute('aria-expanded', 'true');
  });
});
