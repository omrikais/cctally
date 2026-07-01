import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
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

// S6 (#252): the old instant "Reset table sorting" button (in a `sorting-fs`
// fieldset) is now the deferred "Table column sorting" toggle inside the
// consolidated "Restore defaults" fieldset. Clicking it STAGES the reset
// (aria-pressed) and does NOT close the overlay; CLEAR_TABLE_SORTS is applied
// only on Save, and the disabled predicate now checks all three overrides
// (trend + sessions + projects) plus the staged flag.
describe('SettingsOverlay — Table column sorting reset (deferred)', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
    _resetKeymap();
    installGlobalKeydown();
  });

  afterEach(() => {
    uninstallGlobalKeydown();
  });

  it('renders a "Table column sorting" button when overlay is open', async () => {
    const user = userEvent.setup();
    render(<SettingsOverlay />);
    await openSettings(user);
    const btn = screen.getByRole('button', { name: /Table column sorting/i });
    expect(btn).not.toBeNull();
  });

  it('button is disabled when no override exists and nothing is staged', async () => {
    const user = userEvent.setup();
    render(<SettingsOverlay />);
    await openSettings(user);
    const btn = screen.getByRole('button', {
      name: /Table column sorting/i,
    }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it('button is enabled when at least one override exists (any of the three)', async () => {
    act(() => {
      dispatch({
        type: 'SET_TABLE_SORT',
        table: 'projects',
        override: { column: 'cost', direction: 'desc' },
      });
    });
    const user = userEvent.setup();
    render(<SettingsOverlay />);
    await openSettings(user);
    const btn = screen.getByRole('button', {
      name: /Table column sorting/i,
    }) as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
  });

  it('clicking stages the reset (deferred) — overlay stays open, overrides untouched until Save', async () => {
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
    const btn = screen.getByRole('button', {
      name: /Table column sorting/i,
    }) as HTMLButtonElement;
    await user.click(btn);
    // Staged, not applied: aria-pressed flips, override survives, overlay open.
    expect(btn.getAttribute('aria-pressed')).toBe('true');
    expect(getState().prefs.sessionsSortOverride).toEqual({
      column: 'cost', direction: 'desc',
    });
    expect(document.getElementById('settings-root')).toBeTruthy();
  });

  it('Save applies the staged reset: CLEAR_TABLE_SORTS clears all three overrides', async () => {
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
      dispatch({
        type: 'SET_TABLE_SORT',
        table: 'projects',
        override: { column: 'used', direction: 'desc' },
      });
    });
    const user = userEvent.setup();
    render(<SettingsOverlay />);
    await openSettings(user);
    // Stage the table-sort reset (this alone dirties the form → Save enabled).
    await user.click(screen.getByRole('button', { name: /Table column sorting/i }));
    await user.click(screen.getByRole('button', { name: /^Save/ }));
    expect(getState().prefs.trendSortOverride).toBeNull();
    expect(getState().prefs.sessionsSortOverride).toBeNull();
    expect(getState().prefs.projectsSortOverride).toBeNull();
    // Applied on Save → overlay closed.
    expect(document.getElementById('settings-root')).toBeNull();
  });

  it('Save does NOT clear column sorts when nothing is staged and the sort default is unchanged', async () => {
    // The old code unconditionally dispatched SET_TABLE_SORT sessions null on
    // every Save (the Codex blocker). Now an unrelated / no-op Save must not
    // touch the user's column-click sorts. With only overrides present and no
    // dirty field, Save is disabled-when-clean, so the override survives.
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
    const save = screen.getByRole('button', { name: /^Save/ }) as HTMLButtonElement;
    expect(save.disabled).toBe(true); // nothing dirty → disabled
    await user.click(save);
    expect(getState().prefs.sessionsSortOverride).toEqual({
      column: 'cost', direction: 'desc',
    });
  });
});
