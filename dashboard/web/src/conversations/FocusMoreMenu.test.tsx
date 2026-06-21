import { render, screen, fireEvent } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { FocusMoreMenu } from './FocusMoreMenu';

// FocusMoreMenu dispatches via the injected onSelect (the reader wires this to
// SET_CONV_FOCUS_MODE). Tests assert the emitted mode value.
afterEach(() => {
  vi.restoreAllMocks();
});

describe('FocusMoreMenu', () => {
  it('selects the Edits mode', () => {
    const onSelect = vi.fn();
    render(<FocusMoreMenu focusMode="all" subagents={[]} onSelect={onSelect} />);
    fireEvent.click(screen.getByRole('button', { name: /more/i }));
    fireEvent.click(screen.getByRole('menuitem', { name: /edits/i }));
    expect(onSelect).toHaveBeenCalledWith('edits');
  });

  it('selects the Bash mode', () => {
    const onSelect = vi.fn();
    render(<FocusMoreMenu focusMode="all" subagents={[]} onSelect={onSelect} />);
    fireEvent.click(screen.getByRole('button', { name: /more/i }));
    fireEvent.click(screen.getByRole('menuitem', { name: /bash/i }));
    expect(onSelect).toHaveBeenCalledWith('bash');
  });

  it('lists the subagent submenu from the loaded keys + meta labels', () => {
    const onSelect = vi.fn();
    render(
      <FocusMoreMenu
        focusMode="all"
        subagents={[
          { key: 'k1', label: 'Explore' },
          { key: 'k2', label: 'code-reviewer' },
        ]}
        onSelect={onSelect}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /more/i }));
    // The Subagent submenu opens its children.
    fireEvent.click(screen.getByRole('menuitem', { name: /subagent/i }));
    fireEvent.click(screen.getByRole('menuitem', { name: /Explore/i }));
    expect(onSelect).toHaveBeenCalledWith('subagent:k1');
  });

  it('falls back to the key when a subagent label is empty (Codex P1-4)', () => {
    const onSelect = vi.fn();
    render(
      <FocusMoreMenu
        focusMode="all"
        subagents={[{ key: 'dddd4444', label: '' }]}
        onSelect={onSelect}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /more/i }));
    fireEvent.click(screen.getByRole('menuitem', { name: /subagent/i }));
    // The key (truncated for display) is shown as the fallback label.
    fireEvent.click(screen.getByRole('menuitem', { name: /dddd4444/i }));
    expect(onSelect).toHaveBeenCalledWith('subagent:dddd4444');
  });

  it('hides the Subagent entry when there are no subagents', () => {
    render(<FocusMoreMenu focusMode="all" subagents={[]} onSelect={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: /more/i }));
    expect(screen.queryByRole('menuitem', { name: /^subagent/i })).toBeNull();
  });

  it('marks the trigger active when a More-mode is selected', () => {
    render(<FocusMoreMenu focusMode="edits" subagents={[]} onSelect={() => {}} />);
    const trigger = screen.getByRole('button', { name: /more/i });
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    // The active label reflects the current More-mode.
    expect(trigger.textContent).toMatch(/edits/i);
  });

  it('reflects an active subagent:<key> mode label from the matching key', () => {
    render(
      <FocusMoreMenu
        focusMode="subagent:k2"
        subagents={[{ key: 'k2', label: 'code-reviewer' }]}
        onSelect={() => {}}
      />,
    );
    expect(screen.getByRole('button', { name: /more/i }).textContent).toMatch(/code-reviewer/i);
  });

  it('closes on container Escape', () => {
    render(<FocusMoreMenu focusMode="all" subagents={[]} onSelect={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: /more/i }));
    expect(screen.getByRole('menu')).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
    expect(screen.queryByRole('menu')).toBeNull();
  });
});
