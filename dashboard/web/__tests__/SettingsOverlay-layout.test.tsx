import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SettingsOverlay } from '../src/components/SettingsOverlay';
import { _resetForTests, dispatch, getState } from '../src/store/store';
import { DEFAULT_PANEL_ORDER } from '../src/lib/panelRegistry';
import {
  _resetForTests as _resetKeymap,
  installGlobalKeydown,
  uninstallGlobalKeydown,
} from '../src/store/keymap';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  _resetKeymap();
  installGlobalKeydown();
});

afterEach(() => {
  uninstallGlobalKeydown();
});

// S6 (#252): the old instant "Reset card order" button (under a "Layout"
// legend) is now the deferred "Card order" toggle inside the consolidated
// "Restore defaults" fieldset. RESET_PANEL_ORDER is applied only on Save.
describe('<SettingsOverlay /> Card order reset (deferred)', () => {
  it('shows a "Card order" button in the Restore defaults fieldset', async () => {
    const user = userEvent.setup();
    render(<SettingsOverlay />);
    await user.keyboard('s');
    expect(
      screen.getByText(/Restore defaults/i, { selector: 'legend' }),
    ).toBeTruthy();
    expect(screen.getByRole('button', { name: /Card order/i })).toBeTruthy();
  });

  it('staging "Card order" is deferred, then Save restores DEFAULT_PANEL_ORDER and leaves other prefs', async () => {
    dispatch({ type: 'REORDER_PANELS', from: 0, to: 3 });
    dispatch({ type: 'SAVE_PREFS', patch: { sessionsPerPage: 250 } });
    const reordered = [...getState().prefs.panelOrder];
    const user = userEvent.setup();
    render(<SettingsOverlay />);
    await user.keyboard('s');
    await user.click(screen.getByRole('button', { name: /Card order/i }));
    // Deferred: not applied yet.
    expect(getState().prefs.panelOrder).toEqual(reordered);
    // Save applies RESET_PANEL_ORDER; the untouched sessions-per-page survives.
    await user.click(screen.getByRole('button', { name: /^Save/ }));
    expect(getState().prefs.panelOrder).toEqual(DEFAULT_PANEL_ORDER);
    expect(getState().prefs.sessionsPerPage).toBe(250);
  });
});
