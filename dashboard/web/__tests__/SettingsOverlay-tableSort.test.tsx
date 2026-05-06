import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { SettingsOverlay } from '../src/components/SettingsOverlay';
import { _resetForTests, dispatch, getState } from '../src/store/store';
import {
  _resetForTests as _resetKeymap,
  installGlobalKeydown,
  uninstallGlobalKeydown,
} from '../src/store/keymap';

async function openSettings(user: ReturnType<typeof userEvent.setup>) {
  await user.keyboard('s');
}

describe('SettingsOverlay — Reset table sorting', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
    _resetKeymap();
    installGlobalKeydown();
  });

  afterEach(() => {
    uninstallGlobalKeydown();
  });

  it('renders a "Reset table sorting" button when overlay is open', async () => {
    const user = userEvent.setup();
    render(<SettingsOverlay />);
    await openSettings(user);
    const btn = document.querySelector(
      'fieldset.sorting-fs button.settings-btn',
    ) as HTMLButtonElement;
    expect(btn).not.toBeNull();
    expect(btn.textContent).toMatch(/Reset table sorting/);
  });

  it('button is disabled when both overrides are null', async () => {
    const user = userEvent.setup();
    render(<SettingsOverlay />);
    await openSettings(user);
    const btn = document.querySelector(
      'fieldset.sorting-fs button.settings-btn',
    ) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it('button is enabled when at least one override exists', async () => {
    act(() => {
      dispatch({
        type: 'SET_TABLE_SORT',
        table: 'sessions',
        override: { column: 'cost', direction: 'desc' },
      });
    });
    const user = userEvent.setup();
    render(<SettingsOverlay />);
    await openSettings(user);
    const btn = document.querySelector(
      'fieldset.sorting-fs button.settings-btn',
    ) as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
  });

  it('clicking the button dispatches CLEAR_TABLE_SORTS and closes overlay', async () => {
    act(() => {
      dispatch({
        type: 'SET_TABLE_SORT',
        table: 'sessions',
        override: { column: 'cost', direction: 'desc' },
      });
      dispatch({
        type: 'SET_TABLE_SORT',
        table: 'trend',
        override: { column: 'week', direction: 'asc' },
      });
    });
    const user = userEvent.setup();
    render(<SettingsOverlay />);
    await openSettings(user);
    const btn = document.querySelector(
      'fieldset.sorting-fs button.settings-btn',
    ) as HTMLButtonElement;
    await user.click(btn);
    expect(getState().prefs.trendSortOverride).toBeNull();
    expect(getState().prefs.sessionsSortOverride).toBeNull();
    // Overlay closed → settings-root is unmounted.
    expect(document.getElementById('settings-root')).toBeNull();
  });

  it('Save (existing button) clears sessionsSortOverride only — Trend untouched', async () => {
    act(() => {
      dispatch({
        type: 'SET_TABLE_SORT',
        table: 'sessions',
        override: { column: 'cost', direction: 'desc' },
      });
      dispatch({
        type: 'SET_TABLE_SORT',
        table: 'trend',
        override: { column: 'week', direction: 'asc' },
      });
    });
    const user = userEvent.setup();
    render(<SettingsOverlay />);
    await openSettings(user);
    // Click the existing "Save" button.
    const saveBtn = Array.from(
      document.querySelectorAll('.settings-actions button'),
    ).find((b) => b.textContent === 'Save') as HTMLButtonElement;
    await user.click(saveBtn);
    expect(getState().prefs.sessionsSortOverride).toBeNull();
    expect(getState().prefs.trendSortOverride).toEqual({
      column: 'week', direction: 'asc',
    });
  });
});
