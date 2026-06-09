import { render, screen, fireEvent, act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CopyButton } from './CopyButton';

describe('CopyButton', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('copies via navigator.clipboard and shows then reverts the copied state', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(<CopyButton text="hello" />);
    const btn = screen.getByRole('button', { name: 'Copy' });
    fireEvent.click(btn);
    expect(writeText).toHaveBeenCalledWith('hello');
    // writeText resolves on a microtask, not a timer; flush it under act so the
    // copied-state setState commits (fake timers freeze findBy's polling).
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByRole('button', { name: 'Copied' })).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(1200);
    });
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument();
  });

  it('falls back to execCommand when navigator.clipboard is absent', () => {
    // @ts-expect-error force non-secure-context shape
    delete navigator.clipboard;
    const exec = vi.fn().mockReturnValue(true);
    document.execCommand = exec as typeof document.execCommand;
    render(<CopyButton text="x" />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }));
    expect(exec).toHaveBeenCalledWith('copy');
  });

  it('stops propagation so an enclosing <details> does not toggle', () => {
    const onToggle = vi.fn();
    render(
      <details onClick={onToggle}>
        <summary>s</summary>
        <CopyButton text="x" />
      </details>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }));
    // click did not bubble to the details onClick
    expect(onToggle).not.toHaveBeenCalled();
  });
});
