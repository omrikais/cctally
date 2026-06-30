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
    // 25 table rows: 10 panel keys (1-9 + 0 for position 10 — #248 removed
    // 'current-week' from the grid, so the order is exactly 10 long with no
    // ≥11 em-dash row) + 12 data-driven HELP_ROWS single-key rows (#207 D1:
    // r, s, d, S, B, f, /, c, n/N, q, ?, Esc) + 3 combo/gesture rows
    // (Hold+drag, Shift+arrows, ↑/↓).
    const rows = card?.querySelectorAll('table tr');
    expect(rows?.length).toBe(25);
    // Meta line with server URL
    const meta = card?.querySelector('p.meta');
    expect(meta?.textContent).toMatch(/cctally/);
    expect(meta?.querySelector('#help-server-url')).not.toBeNull();
  });

  it('lists the 5 -> Weekly binding', async () => {
    render(<HelpOverlay />);
    const user = userEvent.setup();
    await user.keyboard('?');
    expect(screen.getByText(/open weekly modal/i)).toBeInTheDocument();
  });

  it('lists the 6 -> Monthly binding', async () => {
    render(<HelpOverlay />);
    const user = userEvent.setup();
    await user.keyboard('?');
    expect(screen.getByText(/open monthly modal/i)).toBeInTheDocument();
  });

  it('lists the 7 -> Blocks binding', async () => {
    render(<HelpOverlay />);
    const user = userEvent.setup();
    await user.keyboard('?');
    expect(screen.getByText(/open blocks modal/i)).toBeInTheDocument();
  });

  it('lists the 8 → Daily modal binding', async () => {
    render(<HelpOverlay />);
    const user = userEvent.setup();
    await user.keyboard('?');
    expect(screen.getByText(/open daily modal/i)).toBeInTheDocument();
  });

  it('lists the up/down select-period binding', async () => {
    render(<HelpOverlay />);
    const user = userEvent.setup();
    await user.keyboard('?');
    expect(screen.getByText(/select period/i)).toBeInTheDocument();
  });

  it('renders 0 as the 10th panel shortcut (not "10")', async () => {
    render(<HelpOverlay />);
    const user = userEvent.setup();
    await user.keyboard('?');
    // First 10 panel-row <kbd>s should read '1'..'9' then '0' (per
    // main.tsx's '0' → openPanelByPosition(10) binding).
    const kbds = Array.from(document.querySelectorAll('table kbd'))
      .map((el) => el.textContent)
      .filter((t): t is string => !!t)
      .slice(0, 10);
    expect(kbds).toEqual(['1','2','3','4','5','6','7','8','9','0']);
    // No "10" anywhere — would point at an unbound key.
    const tens = Array.from(document.querySelectorAll('table kbd'))
      .filter((el) => el.textContent === '10');
    expect(tens).toHaveLength(0);
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
