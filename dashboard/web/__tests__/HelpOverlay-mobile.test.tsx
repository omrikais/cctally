import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, act } from '@testing-library/react';
import { HelpOverlay } from '../src/components/HelpOverlay';
import { MOBILE_MEDIA_QUERY } from '../src/lib/breakpoints';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  _resetForTests,
} from '../src/store/keymap';

let mqlMatches = false;
function install() {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: q === MOBILE_MEDIA_QUERY ? mqlMatches : false,
    media: q,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    onchange: null,
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

function openOverlay() {
  act(() => {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: '?' }));
  });
}

describe('HelpOverlay — mobile branch', () => {
  beforeEach(() => {
    install();
    _resetForTests();
    installGlobalKeydown();
  });
  afterEach(() => {
    uninstallGlobalKeydown();
    cleanup();
  });

  it('mobile renders the gesture-guide rows', () => {
    mqlMatches = true;
    render(<HelpOverlay />);
    openOverlay();
    expect(screen.getByText(/tap the chevron/i)).toBeTruthy();
    expect(screen.getByText(/long-press a panel/i)).toBeTruthy();
    expect(screen.getByText(/tap ✕|tap the dim backdrop|backdrop/i)).toBeTruthy();
    expect(screen.getByText(/tap a model chip/i)).toBeTruthy();
    expect(screen.getByText(/tap the sync chip/i)).toBeTruthy();
  });

  it('mobile renders the keyboard list inside a <details> disclosure', () => {
    mqlMatches = true;
    render(<HelpOverlay />);
    openOverlay();
    const details = screen.getByText(/keyboard shortcuts/i).closest('details');
    expect(details).not.toBeNull();
    // Default closed.
    expect((details as HTMLDetailsElement).open).toBe(false);
  });

  it('desktop renders the existing keyboard table directly (no gesture guide)', () => {
    mqlMatches = false;
    render(<HelpOverlay />);
    openOverlay();
    expect(screen.getByText('Keybindings')).toBeTruthy();
    expect(screen.queryByText(/tap the chevron/i)).toBeNull();
  });
});
