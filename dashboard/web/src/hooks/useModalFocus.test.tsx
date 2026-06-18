import { useRef } from 'react';
import { render, fireEvent } from '@testing-library/react';
import { describe, it, expect, beforeEach } from 'vitest';
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
    const { rerender } = render(<Harness active={true} trapEnabled={true} />);
    expect(document.activeElement?.id).toBe('first');
    rerender(<Harness active={true} trapEnabled={false} />);
    expect(document.activeElement?.id).toBe('first'); // unchanged, not back to trigger
  });
});
