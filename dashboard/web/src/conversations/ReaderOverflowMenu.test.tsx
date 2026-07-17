import { render, screen, fireEvent, within } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ReaderOverflowMenu } from './ReaderOverflowMenu';
import { ANON_MODE_KEY, loadAnonMode, saveAnonMode } from '../store/anonPrefs';

// #228 S3 C2 — the mobile reader-header "⋯" overflow menu. These tests assert the
// menu items RENDER and INVOKE the passed callbacks (modal-level wiring, not a
// child-callback unit), plus the two read-only summary rows + the embedded
// Export popover. Built on the shared menu primitive (Escape-to-close,
// focus-return), so closing is asserted too.

const baseProps = {
  sessionId: 'sess-1',
  exportTitle: 'My session',
  anonMode: true,
  onToggleAnon: vi.fn(),
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

  it('#238 R3 — dismisses on outside pointerdown without refocusing the trigger', () => {
    render(
      <div>
        <ReaderOverflowMenu {...baseProps} />
        <button data-testid="outside">outside</button>
      </div>,
    );
    fireEvent.click(screen.getByRole('button', { name: /more actions/i }));
    expect(screen.getByRole('button', { name: /more actions/i })).toHaveAttribute('aria-expanded', 'true');
    fireEvent.pointerDown(screen.getByTestId('outside'));
    expect(screen.getByRole('button', { name: /more actions/i })).toHaveAttribute('aria-expanded', 'false');
    // Silent dismiss: focus must NOT have been forced back onto the trigger.
    expect(document.activeElement).not.toBe(screen.getByRole('button', { name: /more actions/i }));
  });

  it('#238 R3 — a pointerdown INSIDE the menu does not dismiss it', () => {
    render(<ReaderOverflowMenu {...baseProps} />);
    fireEvent.click(screen.getByRole('button', { name: /more actions/i }));
    fireEvent.pointerDown(screen.getByRole('menuitem', { name: /compare with/i }));
    expect(screen.getByRole('button', { name: /more actions/i })).toHaveAttribute('aria-expanded', 'true');
  });

  it('#238 R3 — nested: a pointerdown inside the overflow menu but outside the open Export popover closes only Export; outside both closes the overflow menu too', () => {
    render(
      <div>
        <ReaderOverflowMenu {...baseProps} />
        <button data-testid="outside">outside</button>
      </div>,
    );
    // Open the overflow menu, then its embedded Export popover (its own nested
    // useOutsideDismiss + its own .conv-export rootRef inside .conv-overflow).
    fireEvent.click(screen.getByRole('button', { name: /more actions/i }));
    const exportTrigger = screen.getByRole('button', { name: /export transcript/i });
    fireEvent.click(exportTrigger);
    expect(exportTrigger).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('menu', { name: /export transcript/i })).not.toBeNull();

    // pointerdown INSIDE the overflow menu but OUTSIDE the Export popup (a sibling
    // overflow menuitem) → Export's hook fires (target outside its ref) and closes
    // only Export; the overflow menu's hook sees an inside target and stays open.
    fireEvent.pointerDown(screen.getByRole('menuitem', { name: /compare with/i }));
    expect(exportTrigger).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('menu', { name: /export transcript/i })).toBeNull();
    expect(screen.queryByRole('menu', { name: /more actions/i })).not.toBeNull();
    expect(screen.getByRole('button', { name: /more actions/i })).toHaveAttribute('aria-expanded', 'true');

    // pointerdown OUTSIDE both → the overflow menu closes too.
    fireEvent.pointerDown(screen.getByTestId('outside'));
    expect(screen.queryByRole('menu', { name: /more actions/i })).toBeNull();
    expect(screen.getByRole('button', { name: /more actions/i })).toHaveAttribute('aria-expanded', 'false');
  });

  // #281 S4 — the Anonymize toggle row: the mobile menu was missing any anon
  // control, so a desktop OFF silently produced raw exports on mobile. The row is
  // wired to the SAME store state as the desktop chip.
  describe('#281 S4 anonymize toggle', () => {
    it('renders the Anonymize row and reflects anonMode=true (pressed, On)', () => {
      render(<ReaderOverflowMenu {...baseProps} anonMode />);
      const menu = openMenu();
      const row = within(menu).getByRole('menuitemcheckbox', { name: /anonymize/i });
      expect(row).toHaveAttribute('aria-checked', 'true');
      expect(row.getAttribute('aria-pressed')).toBeNull();   // #304 S2 — APG checkbox semantics replace the pressed-button hybrid
      expect(within(row).getByText('On')).not.toBeNull();
    });

    it('reflects anonMode=false (not pressed, Off)', () => {
      render(<ReaderOverflowMenu {...baseProps} anonMode={false} />);
      const menu = openMenu();
      const row = within(menu).getByRole('menuitemcheckbox', { name: /anonymize/i });
      expect(row).toHaveAttribute('aria-checked', 'false');
      expect(within(row).getByText('Off')).not.toBeNull();
    });

    it('clicking the row invokes onToggleAnon and keeps the menu OPEN (in-place flip, unlike the action rows)', () => {
      const onToggleAnon = vi.fn();
      render(<ReaderOverflowMenu {...baseProps} onToggleAnon={onToggleAnon} />);
      openMenu();
      fireEvent.click(screen.getByRole('menuitemcheckbox', { name: /anonymize/i }));
      expect(onToggleAnon).toHaveBeenCalledTimes(1);
      // Unlike Compare/Expand/etc., the toggle does NOT close the menu.
      expect(screen.queryByRole('menu', { name: /more actions/i })).not.toBeNull();
    });

    it('forwards anonMode to the embedded Export menu (anonymized note shows only when ON)', () => {
      // anonMode ON → the Export popover carries the "Anonymized …" note.
      render(<ReaderOverflowMenu {...baseProps} anonMode />);
      openMenu();
      fireEvent.click(screen.getByRole('button', { name: /export transcript/i }));
      expect(screen.getByText(/anonymized —/i)).not.toBeNull();
    });

    it('does NOT forward the anonymized note when anonMode is OFF', () => {
      render(<ReaderOverflowMenu {...baseProps} anonMode={false} />);
      openMenu();
      fireEvent.click(screen.getByRole('button', { name: /export transcript/i }));
      expect(screen.queryByText(/anonymized —/i)).toBeNull();
    });

    it('toggling flips the persisted store via the same guarded pref write (single source of truth)', () => {
      // A faithful mirror of the reader wiring: onToggleAnon flips React state AND
      // persists through saveAnonMode (the guarded localStorage write). Proves a
      // menu click updates the store the desktop chip also reads.
      localStorage.removeItem(ANON_MODE_KEY);
      function Harness() {
        const [anon, setAnon] = useState<boolean>(loadAnonMode); // default ON
        return (
          <ReaderOverflowMenu
            {...baseProps}
            anonMode={anon}
            onToggleAnon={() =>
              setAnon((v) => {
                const next = !v;
                saveAnonMode(next);
                return next;
              })
            }
          />
        );
      }
      render(<Harness />);
      const menu = openMenu();
      const row = within(menu).getByRole('menuitemcheckbox', { name: /anonymize/i });
      expect(row).toHaveAttribute('aria-checked', 'true'); // default ON
      fireEvent.click(row);
      // Persisted OFF ('0') and the row re-renders reflecting the new state.
      expect(localStorage.getItem(ANON_MODE_KEY)).toBe('0');
      expect(loadAnonMode()).toBe(false);
      expect(
        within(screen.getByRole('menu', { name: /more actions/i })).getByRole('menuitemcheckbox', {
          name: /anonymize/i,
        }),
      ).toHaveAttribute('aria-checked', 'false');
    });
  });

  // #304 S3 (Codex F3) — folding the desktop strip must not drop the ✓ Complete
  // JUMP. When the reader passes onCompletionJump, the read-only completion
  // summary row becomes a real actionable menuitem (the ≤1100 compact band
  // gains the repaired jump too). Without the callback, today's read-only row
  // is byte-identical.
  describe('#304 S3 actionable completion jump (Codex F3)', () => {
    it('with onCompletionJump: completion is the LAST roving menuitem, fires the jump + closes, and is NOT a read-only summary row', () => {
      const onCompletionJump = vi.fn();
      render(
        <ReaderOverflowMenu
          {...baseProps}
          completionTotal={12}
          costCumulative={1.2}
          costTotal={3.4}
          onCompletionJump={onCompletionJump}
        />,
      );
      const menu = openMenu();
      const item = within(menu).getByRole('menuitem', { name: /✓ Complete · 12/i });
      expect(item).not.toBeNull();
      // Appended after Collapse-all → it is the LAST roving menuitem.
      const menuitems = within(menu).getAllByRole('menuitem');
      expect(menuitems[menuitems.length - 1]).toBe(item);
      // No read-only completion summary row (the "✓ 12" summary value is gone).
      expect(within(menu).queryByText('✓ 12')).toBeNull();
      // The cost summary row still renders.
      expect(within(menu).getByText(/\$1\.20 \/ \$3\.40/)).not.toBeNull();
      // Clicking fires the jump once and closes the menu (focus-return path).
      fireEvent.click(item);
      expect(onCompletionJump).toHaveBeenCalledTimes(1);
      expect(screen.queryByRole('menu', { name: /more actions/i })).toBeNull();
    });

    it('without onCompletionJump: completion renders as the read-only summary row (unchanged)', () => {
      render(<ReaderOverflowMenu {...baseProps} completionTotal={12} />);
      const menu = openMenu();
      // Read-only summary value, NOT a menuitem.
      expect(within(menu).getByText('✓ 12')).not.toBeNull();
      expect(within(menu).queryByRole('menuitem', { name: /✓ Complete/i })).toBeNull();
    });

    it('with onCompletionJump but no completion (completionTotal null): no completion menuitem and no summary row', () => {
      const onCompletionJump = vi.fn();
      render(<ReaderOverflowMenu {...baseProps} completionTotal={null} onCompletionJump={onCompletionJump} />);
      const menu = openMenu();
      expect(within(menu).queryByRole('menuitem', { name: /✓ Complete/i })).toBeNull();
      expect(within(menu).queryByText(/✓/)).toBeNull();
    });
  });
});
