import { render, screen, fireEvent, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ReaderOverflowMenu } from './ReaderOverflowMenu';

// #228 S3 C2 — the mobile reader-header "⋯" overflow menu. These tests assert the
// menu items RENDER and INVOKE the passed callbacks (modal-level wiring, not a
// child-callback unit), plus the two read-only summary rows + the embedded
// Export popover. Built on the shared menu primitive (Escape-to-close,
// focus-return), so closing is asserted too.

const baseProps = {
  sessionId: 'sess-1',
  exportTitle: 'My session',
  onCompare: vi.fn(),
  onLatest: vi.fn(),
  onExpandAll: vi.fn(),
  onCollapseAll: vi.fn(),
};

beforeEach(() => {
  globalThis.fetch = vi.fn(async () => ({ ok: true, status: 200, text: async () => '# md' }) as Response);
});
afterEach(() => {
  vi.restoreAllMocks();
});

function openMenu() {
  fireEvent.click(screen.getByRole('button', { name: /more actions/i }));
  return screen.getByRole('menu', { name: /more actions/i });
}

describe('ReaderOverflowMenu', () => {
  it('the trigger opens a menu listing the secondary actions', () => {
    render(<ReaderOverflowMenu {...baseProps} onCompare={vi.fn()} onLatest={vi.fn()} onExpandAll={vi.fn()} onCollapseAll={vi.fn()} />);
    const menu = openMenu();
    expect(within(menu).getByRole('menuitem', { name: /compare with/i })).not.toBeNull();
    expect(within(menu).getByRole('menuitem', { name: /latest/i })).not.toBeNull();
    expect(within(menu).getByRole('menuitem', { name: /expand all/i })).not.toBeNull();
    expect(within(menu).getByRole('menuitem', { name: /collapse all/i })).not.toBeNull();
    // Export rides as its own nested popover trigger inside the menu.
    expect(within(menu).getByRole('button', { name: /export transcript/i })).not.toBeNull();
  });

  it('Compare with… invokes onCompare and closes the menu', () => {
    const onCompare = vi.fn();
    render(<ReaderOverflowMenu {...baseProps} onCompare={onCompare} />);
    openMenu();
    fireEvent.click(screen.getByRole('menuitem', { name: /compare with/i }));
    expect(onCompare).toHaveBeenCalledTimes(1);
    // The menu closes after a pick (focus-return; the menu unmounts).
    expect(screen.queryByRole('menu', { name: /more actions/i })).toBeNull();
  });

  it('Latest ↓ invokes onLatest', () => {
    const onLatest = vi.fn();
    render(<ReaderOverflowMenu {...baseProps} onLatest={onLatest} />);
    openMenu();
    fireEvent.click(screen.getByRole('menuitem', { name: /latest/i }));
    expect(onLatest).toHaveBeenCalledTimes(1);
  });

  it('Expand all / Collapse all invoke their callbacks', () => {
    const onExpandAll = vi.fn();
    const onCollapseAll = vi.fn();
    render(<ReaderOverflowMenu {...baseProps} onExpandAll={onExpandAll} onCollapseAll={onCollapseAll} />);
    openMenu();
    fireEvent.click(screen.getByRole('menuitem', { name: /expand all/i }));
    expect(onExpandAll).toHaveBeenCalledTimes(1);
    // Re-open (the previous pick closed it) for the second action.
    openMenu();
    fireEvent.click(screen.getByRole('menuitem', { name: /collapse all/i }));
    expect(onCollapseAll).toHaveBeenCalledTimes(1);
  });

  it('hides Latest ↓ when onLatest is null (empty conversation)', () => {
    render(<ReaderOverflowMenu {...baseProps} onLatest={null} />);
    openMenu();
    expect(screen.queryByRole('menuitem', { name: /latest/i })).toBeNull();
    // The other actions still render.
    expect(screen.getByRole('menuitem', { name: /compare with/i })).not.toBeNull();
  });

  it('surfaces the read-only completion + cumulative-cost rows', () => {
    render(
      <ReaderOverflowMenu
        {...baseProps}
        completionTotal={7}
        costCumulative={1.2}
        costTotal={3.4}
        costApprox
      />,
    );
    const menu = openMenu();
    expect(within(menu).getByText('✓ 7')).not.toBeNull();
    // Cumulative-cost row: ~$cum / $total (approx → leading ~).
    expect(within(menu).getByText(/~\$1\.20 \/ \$3\.40/)).not.toBeNull();
  });

  it('omits the cost row when the session total is zero', () => {
    render(<ReaderOverflowMenu {...baseProps} completionTotal={null} costTotal={0} />);
    const menu = openMenu();
    expect(within(menu).queryByText(/\$/)).toBeNull();
  });

  it('Escape closes the menu and restores focus to the trigger', () => {
    render(<ReaderOverflowMenu {...baseProps} />);
    const trigger = screen.getByRole('button', { name: /more actions/i });
    // Focus the trigger before opening (real interaction; a JSDOM click does not
    // move focus) so restoreRef captures it for the Escape focus-return.
    trigger.focus();
    fireEvent.click(trigger);
    const menu = screen.getByRole('menu', { name: /more actions/i });
    fireEvent.keyDown(menu, { key: 'Escape' });
    expect(screen.queryByRole('menu', { name: /more actions/i })).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });
});
