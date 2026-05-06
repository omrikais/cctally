import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HelpOverlay } from '../src/components/HelpOverlay';
import { _resetForTests, dispatch } from '../src/store/store';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  _resetForTests as _resetKeymapForTests,
} from '../src/store/keymap';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  _resetKeymapForTests();
  installGlobalKeydown();
});

afterEach(() => {
  uninstallGlobalKeydown();
});

async function open(user: ReturnType<typeof userEvent.setup>) {
  await user.keyboard('?');
}

describe('<HelpOverlay /> reflects panelOrder', () => {
  it('renders all 9 number keys with their default labels', async () => {
    const user = userEvent.setup();
    render(<HelpOverlay />);
    await open(user);
    for (const label of ['Current Week', 'Forecast', 'Trend', 'Sessions', 'Weekly', 'Monthly', 'Blocks', 'Daily', 'Recent alerts']) {
      expect(screen.getByText(`Open ${label} modal`)).toBeTruthy();
    }
  });

  it('updates the labels after a reorder', async () => {
    const user = userEvent.setup();
    render(<HelpOverlay />);
    await open(user);
    // Swap positions 0 and 1.
    act(() => dispatch({ type: 'REORDER_PANELS', from: 0, to: 1 }));
    // The row with key=1 should now show "Forecast".
    const rows = document.querySelectorAll('#help-overlay table tr');
    const row1 = Array.from(rows).find((r) => r.textContent?.trim().startsWith('1'));
    expect(row1?.textContent).toContain('Forecast');
  });

  it('includes the "Hold + drag a card → rearrange" row', async () => {
    const user = userEvent.setup();
    render(<HelpOverlay />);
    await open(user);
    expect(screen.getByText(/rearrange/i)).toBeTruthy();
  });
});
