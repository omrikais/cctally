import { render, screen, fireEvent, act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PermalinkButton } from './PermalinkButton';

describe('PermalinkButton', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    window.history.replaceState(null, '', '/'); // clean hash for reflect assertions
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    window.history.replaceState(null, '', '/');
  });

  it('copies the absolute deep-link and reflects the address bar to the turn', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const replace = vi.spyOn(window.history, 'replaceState');
    render(<PermalinkButton sessionId="s" uuid="u" />);
    const btn = screen.getByRole('button', { name: 'Copy link to this turn' });
    fireEvent.click(btn);
    // The absolute permalink is origin + pathname + hash; derive the origin
    // from the runtime so the assertion is robust to jsdom's default host
    // (this version reports http://localhost:3000, pathname '/').
    expect(writeText).toHaveBeenCalledWith(
      `${window.location.origin}/#/conversations/s/u`,
    );
    expect(replace).toHaveBeenCalledWith(null, '', '#/conversations/s/u');
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByRole('button', { name: 'Link copied' })).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(1200);
    });
    expect(screen.getByRole('button', { name: 'Copy link to this turn' })).toBeInTheDocument();
  });

  it('falls back to execCommand when navigator.clipboard is absent', () => {
    // @ts-expect-error force non-secure-context shape
    delete navigator.clipboard;
    const exec = vi.fn().mockReturnValue(true);
    document.execCommand = exec as typeof document.execCommand;
    render(<PermalinkButton sessionId="s" uuid="u" />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy link to this turn' }));
    expect(exec).toHaveBeenCalledWith('copy');
  });

  it('stops propagation so an enclosing <details> does not toggle', () => {
    const onToggle = vi.fn();
    render(
      <details onClick={onToggle}>
        <summary>s</summary>
        <PermalinkButton sessionId="s" uuid="u" />
      </details>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Copy link to this turn' }));
    expect(onToggle).not.toHaveBeenCalled();
  });

  it('appends an optional className alongside the base class', () => {
    render(<PermalinkButton sessionId="s" uuid="u" className="conv-chip-permalink" />);
    const btn = screen.getByRole('button', { name: 'Copy link to this turn' });
    expect(btn.className).toBe('conv-copy-btn conv-chip-permalink');
  });

  it('renders the base class alone when no className is passed', () => {
    render(<PermalinkButton sessionId="s" uuid="u" />);
    const btn = screen.getByRole('button', { name: 'Copy link to this turn' });
    expect(btn.className).toBe('conv-copy-btn');
  });
});
