import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, act, type RenderResult } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { PanelHost } from '../src/components/PanelHost';
import {
  PanelGridDnd,
  handleDragStartAction,
  handleDragEndAction,
  handleDragCancelAction,
} from '../src/components/PanelGridDnd';
import {
  armClickSuppression,
  shouldSuppressNextClick,
  _resetClickSuppressionForTests,
} from '../src/lib/clickSuppression';
import { _resetForTests, getState, updateSnapshot } from '../src/store/store';
import fixture from './fixtures/envelope.json';
import type { Envelope } from '../src/types/envelope';
import type { PanelId } from '../src/lib/panelRegistry';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  _resetClickSuppressionForTests();
  updateSnapshot(fixture as unknown as Envelope);
});

function renderInDnd(items: PanelId[], ui: ReactNode): RenderResult {
  return render(<PanelGridDnd items={items}>{ui}</PanelGridDnd>);
}

describe('<PanelHost />', () => {
  it('renders the registered component for the given id', () => {
    renderInDnd(['current-week'], <PanelHost id="current-week" index={0} />);
    expect(document.querySelector('[data-panel-kind="current-week"]')).toBeTruthy();
  });

  it('attaches data-panel-host + data-panel-index for hit-testing', () => {
    renderInDnd(['current-week'], <PanelHost id="current-week" index={3} />);
    const host = document.querySelector('[data-panel-host="current-week"]') as HTMLElement;
    expect(host).toBeTruthy();
    expect(host.dataset.panelIndex).toBe('3');
  });

  it('Shift+ArrowDown swaps the panel with its successor', async () => {
    const user = userEvent.setup();
    renderInDnd(['current-week'], <PanelHost id="current-week" index={0} />);
    // The wrapper carries the onKeyDown handler; focus the inner panel — the
    // keydown bubbles up to the wrapper.
    const inner = document.querySelector('[data-panel-kind="current-week"]') as HTMLElement;
    inner.focus();
    await user.keyboard('{Shift>}{ArrowDown}{/Shift}');
    expect(getState().prefs.panelOrder[1]).toBe('current-week');
  });

  it('a quick click on the inner panel still opens its modal', async () => {
    const user = userEvent.setup();
    renderInDnd(['current-week'], <PanelHost id="current-week" index={0} />);
    const inner = document.querySelector('[data-panel-kind="current-week"]') as HTMLElement;
    await user.click(inner);
    expect(getState().openModal).toBe('current-week');
  });

  it('a click is suppressed while the post-drag flag is armed', () => {
    renderInDnd(['current-week'], <PanelHost id="current-week" index={0} />);
    const inner = document.querySelector('[data-panel-kind="current-week"]') as HTMLElement;

    // Simulate the dnd-kit drag-end path arming click suppression. The flag
    // clears on the next macrotask (setTimeout 0) but the synchronous click
    // dispatched right after still observes it.
    armClickSuppression();
    expect(shouldSuppressNextClick()).toBe(true);

    act(() => { inner.click(); });

    // openModal must remain null — the panel-host's onClickCapture stopped
    // propagation before the inner panel's onClick fired.
    expect(getState().openModal).toBeNull();
  });

  it('does not introduce an extra tab stop alongside the inner panel', () => {
    renderInDnd(['current-week'], <PanelHost id="current-week" index={0} />);
    const host = document.querySelector('[data-panel-host="current-week"]') as HTMLElement;
    // The wrapper must NOT be focusable — the inner panel section owns the
    // tab stop and Enter/Space modal-open behavior. Without this guard,
    // dnd-kit's sortable attributes would put role=button + tabIndex=0 on
    // the wrapper, doubling up keyboard navigation.
    expect(host.hasAttribute('tabindex')).toBe(false);
    expect(host.getAttribute('role')).toBeNull();
  });

  it('omits the inline transition when prefers-reduced-motion is set', () => {
    vi.stubGlobal('matchMedia', () => ({
      matches: true,
      media: '(prefers-reduced-motion: reduce)',
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
      onchange: null,
    } as unknown as MediaQueryList));
    try {
      renderInDnd(['current-week'], <PanelHost id="current-week" index={0} />);
      const host = document.querySelector('[data-panel-host="current-week"]') as HTMLElement;
      // The wrapper's inline style.transition must be empty; otherwise the
      // sortable transform would still animate even with reduced-motion on.
      expect(host.style.transition).toBe('');
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('Shift+Arrow inside an editable descendant does not reorder', async () => {
    const user = userEvent.setup();
    renderInDnd(['current-week'], <PanelHost id="current-week" index={0} />);
    const host = document.querySelector('[data-panel-host="current-week"]') as HTMLElement;
    // Inject a text input and dispatch Shift+Arrow from there. The wrapper's
    // onKeyDown must skip the swap so the user's text-selection gesture
    // works normally.
    const input = document.createElement('input');
    input.type = 'text';
    input.value = 'hello world';
    host.appendChild(input);
    const before = [...getState().prefs.panelOrder];
    input.focus();
    await user.keyboard('{Shift>}{ArrowRight}{/Shift}');
    expect(getState().prefs.panelOrder).toEqual(before);
  });
});

describe('<PanelGridDnd /> store wiring', () => {
  it('handleDragEndAction with a valid drop reorders panelOrder', () => {
    const before = [...getState().prefs.panelOrder];
    handleDragEndAction(before[0], before[1]);
    expect(getState().prefs.panelOrder[0]).toBe(before[1]);
    expect(getState().prefs.panelOrder[1]).toBe(before[0]);
  });

  it('handleDragEndAction with no drop target leaves panelOrder unchanged', () => {
    const before = [...getState().prefs.panelOrder];
    handleDragEndAction(before[0], null);
    expect(getState().prefs.panelOrder).toEqual(before);
  });

  it('handleDragEndAction with active === over leaves panelOrder unchanged', () => {
    const before = [...getState().prefs.panelOrder];
    handleDragEndAction(before[0], before[0]);
    expect(getState().prefs.panelOrder).toEqual(before);
  });

  it('handleDragCancelAction leaves panelOrder unchanged', () => {
    const before = [...getState().prefs.panelOrder];
    handleDragCancelAction();
    expect(getState().prefs.panelOrder).toEqual(before);
  });

  it('handleDragStartAction arms click suppression', () => {
    expect(shouldSuppressNextClick()).toBe(false);
    handleDragStartAction();
    expect(shouldSuppressNextClick()).toBe(true);
  });

  it('handleDragEndAction arms click suppression', () => {
    handleDragEndAction('current-week', null);
    expect(shouldSuppressNextClick()).toBe(true);
  });

  it('handleDragCancelAction arms click suppression', () => {
    handleDragCancelAction();
    expect(shouldSuppressNextClick()).toBe(true);
  });
});
