// HelpOverlay — positional hotkey rule (spec 2026-05-21 §1).
// Positions 1..9 render <kbd>1..9</kbd>, position 10 renders <kbd>0</kbd>,
// positions ≥ 11 have NO digit binding (the spec defers multi-key
// chord support to F9) — render an em-dash instead of a literal "11".
import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { HelpOverlay } from './HelpOverlay';
import { _resetForTests, dispatch } from '../store/store';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  registerKeymap,
  _resetForTests as _resetKeymapForTests,
} from '../store/keymap';

function openHelp() {
  // The overlay toggles on the '?' global key — see useKeymap in
  // HelpOverlay.tsx, registered via the keymap module which listens on
  // document. Mirrors the actual user flow.
  fireEvent.keyDown(document, { key: '?' });
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  _resetKeymapForTests();
  // useKeymap only registers bindings — production code wires the
  // listener via installGlobalKeydown(). Tests need to attach it
  // explicitly so dispatched keydown events trigger bound handlers.
  installGlobalKeydown();
});

afterEach(() => {
  uninstallGlobalKeydown();
});

describe('<HelpOverlay /> positional hotkey rule', () => {
  it('renders <kbd>1..9</kbd> for positions 1..9 and <kbd>0</kbd> for position 10', () => {
    render(<HelpOverlay />);
    openHelp();
    // After Task B3, DEFAULT_PANEL_ORDER has 11 entries so the help table
    // includes the 11th row.
    const kbds = screen.getAllByText(
      (_, el) => el?.tagName === 'KBD',
    );
    // We don't compare the full set (the table includes 'r', 's', '?',
    // 'Esc', 'Shift', '↑', '↓' etc.); just check that the digit keys are
    // exactly 1..9, 0 — none of them say "11".
    const digitKbds = kbds.filter((k) => /^(0|[1-9])$/.test(k.textContent ?? ''));
    const digitLabels = digitKbds.map((k) => k.textContent).sort();
    expect(digitLabels).toEqual(
      ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9'].sort(),
    );
    // And no <kbd>11</kbd> anywhere.
    expect(kbds.find((k) => k.textContent === '11')).toBeUndefined();
  });

  it('renders an em-dash (not a <kbd>) for the 11th panel row', () => {
    render(<HelpOverlay />);
    openHelp();
    // Find the row whose label cell says "Open Cache Report modal" — the
    // 11th panel by default after Task B3 + B8. (B4 ships before B8, so
    // the label cell at this point will still read "Open undefined modal"
    // if PANEL_REGISTRY hasn't been extended. To stay decoupled from B8's
    // ordering we instead probe by structure: find the 11th <tr> inside
    // the first <table> and assert its first cell renders an em-dash
    // span, not a <kbd>.)
    const table = document.querySelector('#help-overlay table');
    expect(table).not.toBeNull();
    const rows = Array.from(table?.querySelectorAll('tbody > tr') ?? []);
    // First 11 rows are the panel slots (DEFAULT_PANEL_ORDER.length).
    const eleventh = rows[10];
    expect(eleventh).toBeTruthy();
    const firstCell = eleventh?.querySelector('td');
    expect(firstCell).not.toBeNull();
    // The cell should contain an em-dash and NOT a <kbd>.
    expect(firstCell?.querySelector('kbd')).toBeNull();
    expect(firstCell?.textContent).toBe('—');
  });
});

describe('<HelpOverlay /> Esc layering is deterministic (#156)', () => {
  it('Help Esc (overlay) beats a conversations-view global Esc registered earlier', () => {
    const convEsc = vi.fn();
    // Register a conversations-style global Esc BEFORE HelpOverlay mounts, so
    // insertion order favours it. The fix must let HelpOverlay's overlay-scope
    // Esc win anyway. (Non-vacuity: reverting HelpOverlay's Esc to scope
    // 'global' makes convEsc win — both 'global', earlier insertion.)
    registerKeymap([
      { key: 'Escape', scope: 'global', view: 'conversations', when: () => true, action: convEsc },
    ]);
    render(<HelpOverlay />);
    openHelp();                 // '?' toggles it open
    expect(document.querySelector('#help-overlay')).not.toBeNull();
    dispatch({ type: 'SET_VIEW', view: 'conversations' });
    fireEvent.keyDown(document, { key: 'Escape' });
    // Help closed (overlay gone), conversations Esc never fired.
    expect(document.querySelector('#help-overlay')).toBeNull();
    expect(convEsc).not.toHaveBeenCalled();
  });
});
