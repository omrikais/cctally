import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HelpOverlay } from '../src/components/HelpOverlay';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  _resetForTests,
} from '../src/store/keymap';

beforeEach(() => {
  _resetForTests();
  installGlobalKeydown();
});

afterEach(() => {
  uninstallGlobalKeydown();
});

describe('<HelpOverlay />', () => {
  it('is hidden by default', () => {
    render(<HelpOverlay />);
    expect(document.getElementById('help-overlay')).toBeNull();
  });

  it('opens on ? and renders the keybindings table + meta server-url line', async () => {
    render(<HelpOverlay />);
    const user = userEvent.setup();
    await user.keyboard('?');
    const overlay = document.getElementById('help-overlay');
    expect(overlay).not.toBeNull();
    const card = overlay?.querySelector('.help-card');
    expect(card).not.toBeNull();
    // h2 "Keybindings"
    expect(card?.querySelector('h2')?.textContent).toBe('Keybindings');
    // 24 table rows: 8 panel keys (1-8 — S8 #254 collapsed weekly/monthly/
    // daily into one History card, so the grid order is exactly 8 long) +
    // 12 data-driven HELP_ROWS single-key rows (#207 D1: r, s, d, S, B, f, /,
    // c, n/N, q, ?, Esc) + 4 combo/gesture rows (Hold+drag, Shift+arrows,
    // ↑/↓ select period, ←/→ switch Day/Week/Month).
    const rows = card?.querySelectorAll('table tr');
    expect(rows?.length).toBe(24);
    // Meta line with server URL
    const meta = card?.querySelector('p.meta');
    expect(meta?.textContent).toMatch(/cctally/);
    expect(meta?.querySelector('#help-server-url')).not.toBeNull();
  });

  it('lists the History modal binding (S8 #254 — one card replaces Weekly/Monthly/Daily)', async () => {
    render(<HelpOverlay />);
    const user = userEvent.setup();
    await user.keyboard('?');
    expect(screen.getByText(/open history modal/i)).toBeInTheDocument();
    // The three legacy period cards are gone from the grid → no rows for them.
    expect(screen.queryByText(/open weekly modal/i)).toBeNull();
    expect(screen.queryByText(/open monthly modal/i)).toBeNull();
    expect(screen.queryByText(/open daily modal/i)).toBeNull();
  });

  it('lists the Blocks modal binding', async () => {
    render(<HelpOverlay />);
    const user = userEvent.setup();
    await user.keyboard('?');
    expect(screen.getByText(/open blocks modal/i)).toBeInTheDocument();
  });

  it('lists the up/down select-period binding', async () => {
    render(<HelpOverlay />);
    const user = userEvent.setup();
    await user.keyboard('?');
    expect(screen.getByText(/select period/i)).toBeInTheDocument();
  });

  it('renders exactly 8 panel-digit shortcuts 1..8 (S8 #254 — no 0/9/10)', async () => {
    render(<HelpOverlay />);
    const user = userEvent.setup();
    await user.keyboard('?');
    // The 8-card grid binds digits 1..8; there is no position 9/10, so no
    // '9' and no '0' (the former position-10 key).
    const kbds = Array.from(document.querySelectorAll('table kbd'))
      .map((el) => el.textContent)
      .filter((t): t is string => !!t)
      .slice(0, 8);
    expect(kbds).toEqual(['1','2','3','4','5','6','7','8']);
    // No "0" / "9" / "10" digit-kbd anywhere — would point at an unbound key.
    for (const stray of ['0', '9', '10']) {
      expect(
        Array.from(document.querySelectorAll('table kbd')).filter((el) => el.textContent === stray),
      ).toHaveLength(0);
    }
  });

  it('lists a Projects modal binding (4 → Projects in default order)', async () => {
    // #248 — Projects sits at index 3 of DEFAULT_PANEL_ORDER → keyboard '4'.
    // The position-10 binding ('0') maps to whatever is last in the order
    // (cache-report in default), NOT specifically projects — this assertion
    // only confirms the Projects row appears in the overlay with some
    // shortcut, not that it's bound to '0'.
    render(<HelpOverlay />);
    const user = userEvent.setup();
    await user.keyboard('?');
    expect(screen.getByText(/open projects modal/i)).toBeInTheDocument();
  });

  it('closes on Escape when open', async () => {
    render(<HelpOverlay />);
    const user = userEvent.setup();
    await user.keyboard('?');
    expect(document.getElementById('help-overlay')).not.toBeNull();
    await user.keyboard('{Escape}');
    expect(document.getElementById('help-overlay')).toBeNull();
  });
});
