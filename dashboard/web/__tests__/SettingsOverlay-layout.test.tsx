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

describe('<SettingsOverlay /> Layout fieldset', () => {
  it('shows a "Reset card order" button under a Layout legend', async () => {
    const user = userEvent.setup();
    render(<SettingsOverlay />);
    await user.keyboard('s');
    expect(screen.getByText('Layout')).toBeTruthy();
    expect(screen.getByRole('button', { name: /reset card order/i })).toBeTruthy();
  });

  it('clicking "Reset card order" restores DEFAULT_PANEL_ORDER and does NOT touch other prefs', async () => {
    dispatch({ type: 'REORDER_PANELS', from: 0, to: 3 });
    dispatch({ type: 'SAVE_PREFS', patch: { sessionsPerPage: 250 } });
    const user = userEvent.setup();
    render(<SettingsOverlay />);
    await user.keyboard('s');
    await user.click(screen.getByRole('button', { name: /reset card order/i }));
    expect(getState().prefs.panelOrder).toEqual(DEFAULT_PANEL_ORDER);
    expect(getState().prefs.sessionsPerPage).toBe(250);
  });
});
