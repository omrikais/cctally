import { useRef } from 'react';
import { render, fireEvent } from '@testing-library/react';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { useModalFocus } from './useModalFocus';

function Harness({ active, trapEnabled = true }: { active: boolean; trapEnabled?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useModalFocus(ref, { active, trapEnabled });
  return (
    <div>
      <button id="trigger">trigger</button>
      {active && (
        <div ref={ref} role="dialog">
          <button id="first">first</button>
          <input id="mid" />
          <select id="sel"><option>a</option></select>
        </div>
      )}
    </div>
  );
}

// Variant exercising `initialFocus: 'container'` — the dialog container is
// tabIndex=-1 (so it is NOT in the focusables list), and focus lands on the
// container itself on open. Models the Doctor modal, whose first control
// self-disables on open (#207 A1).
function ContainerFocusHarness({ active, trapEnabled = true }: { active: boolean; trapEnabled?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useModalFocus(ref, { active, trapEnabled, initialFocus: 'container' });
  return (
    <div>
      <button id="trigger">trigger</button>
      {active && (
        <div ref={ref} role="dialog" tabIndex={-1}>
          <button id="first">first</button>
          <input id="mid" />
          <select id="sel"><option>a</option></select>
        </div>
      )}
    </div>
  );
}

describe('useModalFocus', () => {
  beforeEach(() => { document.body.innerHTML = ''; });

  it('moves focus into the dialog on activate and restores to trigger on deactivate', () => {
    const { rerender } = render(<Harness active={false} />);
    document.getElementById('trigger')!.focus();
    rerender(<Harness active={true} />);
    expect(document.activeElement?.id).toBe('first');
    rerender(<Harness active={false} />);
    expect(document.activeElement?.id).toBe('trigger');
  });

  it('treats <select> as focusable (selector width) — Tab from the last focusable wraps to first', () => {
    render(<Harness active={true} />);
    document.getElementById('sel')!.focus(); // select is the last focusable
    fireEvent.keyDown(document, { key: 'Tab' });
    expect(document.activeElement?.id).toBe('first');
  });

  it('Shift+Tab on the first focusable wraps to the last', () => {
    render(<Harness active={true} />);
    document.getElementById('first')!.focus();
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true });
    expect(document.activeElement?.id).toBe('sel');
  });

  it('does not trap when trapEnabled=false (suspended under a higher layer)', () => {
    render(<Harness active={true} trapEnabled={false} />);
    document.getElementById('sel')!.focus();
    fireEvent.keyDown(document, { key: 'Tab' }); // no wrap because trap is off
    expect(document.activeElement?.id).toBe('sel');
  });

  it('toggling trapEnabled does not re-restore focus (only deactivation does)', () => {
    // Start closed with the trigger focused so it becomes the captured restore target.
    const { rerender } = render(<Harness active={false} trapEnabled={true} />);
    const trigger = document.getElementById('trigger')!;
    trigger.focus();
    rerender(<Harness active={true} trapEnabled={true} />);
    expect(document.activeElement?.id).toBe('first');
    // Spy AFTER focus-in so we only observe the toggle. If the focus-in effect wrongly
    // depended on trapEnabled, toggling it would run cleanup (restore→trigger.focus) then
    // re-run focus-in — the spy would fire. It must not.
    const focusSpy = vi.spyOn(trigger, 'focus');
    rerender(<Harness active={true} trapEnabled={false} />);
    expect(focusSpy).not.toHaveBeenCalled();
    expect(document.activeElement?.id).toBe('first'); // unchanged, not back to trigger
    focusSpy.mockRestore();
  });

  describe("initialFocus: 'container'", () => {
    it('focuses the dialog container itself on activate (not the first focusable)', () => {
      const { rerender } = render(<ContainerFocusHarness active={false} />);
      document.getElementById('trigger')!.focus();
      rerender(<ContainerFocusHarness active={true} />);
      // Focus is on the container (role=dialog, tabIndex=-1), NOT the first button.
      // This is what keeps focus inside the dialog even when the first control
      // self-disables (#207 A1) — the container can never be disabled.
      const active = document.activeElement as HTMLElement | null;
      expect(active?.getAttribute('role')).toBe('dialog');
      expect(active?.id).not.toBe('first');
    });

    it('Tab from the container moves to the first focusable', () => {
      render(<ContainerFocusHarness active={true} />);
      const dialog = document.querySelector('[role="dialog"]') as HTMLElement;
      dialog.focus(); // on the container itself (not among the focusables)
      fireEvent.keyDown(document, { key: 'Tab' });
      expect(document.activeElement?.id).toBe('first');
    });

    it('Shift+Tab from the container wraps to the LAST focusable (no backward escape)', () => {
      render(<ContainerFocusHarness active={true} />);
      const dialog = document.querySelector('[role="dialog"]') as HTMLElement;
      dialog.focus(); // on the container itself (not among the focusables)
      fireEvent.keyDown(document, { key: 'Tab', shiftKey: true });
      // Without the idx===-1 trap-edge branch, Shift+Tab from the container would
      // escape the dialog backwards. It must wrap to the last focusable instead.
      expect(document.activeElement?.id).toBe('sel');
    });
  });
});
