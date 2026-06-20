// #216 (follow-up to #210 / #207 D6) — the chrome overlays
// (Settings / Help / Doctor / Update) and the onboarding toast must render
// their close/dismiss affordance through the shared <ModalCloseButton>
// primitive, so the single `×` (U+00D7) glyph — never the bespoke `⤬`
// (U+292C) — reaches every modal. This pins each close control's rendered
// text to `×`; a regression that hand-rolls a bespoke glyph back into any of
// these shells fails RED. Sibling to src/share/closeGlyph.test.tsx, which
// guards the share-family shells the same way.
import { render, screen, fireEvent, act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SettingsOverlay } from './SettingsOverlay';
import { HelpOverlay } from './HelpOverlay';
import { DoctorModal } from './DoctorModal';
import { UpdateModal } from './UpdateModal';
import { OnboardingToast } from './OnboardingToast';
import { _resetForTests, dispatch, type UpdateState } from '../store/store';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  _resetForTests as _resetKeymap,
} from '../store/keymap';

const CLOSE_GLYPH = '×'; // U+00D7
const BESPOKE_GLYPH = '⤬'; // U+292C — the pre-D6 drift glyph

// Minimal UpdateState so UpdateModal actually renders (it returns null when
// update.state is null even with modalOpen=true). Mirrors the seed in
// useScrollLock.integration.test.tsx.
function seedUpdateState(): UpdateState {
  return {
    current_version: '1.50.0',
    latest_version: '1.51.0',
    available: true,
    method: 'npm',
    update_command: 'npm i -g cctally',
    release_notes_url: null,
    check_status: 'ok',
    checked_at_utc: '2026-06-19T00:00:00Z',
    prerelease_note: null,
  };
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  _resetKeymap();
  installGlobalKeydown();
  // useIsMobile (Help / Onboarding) reads matchMedia; default to desktop.
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: false, media: q, onchange: null,
    addEventListener: () => {}, removeEventListener: () => {},
    addListener: () => {}, removeListener: () => {},
    dispatchEvent: () => false,
  }));
  // Doctor fires a report fetch on open; stub so nothing hangs.
  vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => {})));
});

afterEach(() => {
  uninstallGlobalKeydown();
  _resetKeymap();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('#216: chrome overlays use the shared × close glyph', () => {
  it('SettingsOverlay close button uses ×', () => {
    render(<SettingsOverlay />);
    // Opens on the global `s` key (the production open path).
    fireEvent.keyDown(document, { key: 's' });
    const btn = screen.getByRole('button', { name: 'Close' });
    expect(btn.textContent).toBe(CLOSE_GLYPH);
    expect(btn.textContent).not.toBe(BESPOKE_GLYPH);
  });

  it('HelpOverlay close button uses ×', () => {
    render(<HelpOverlay />);
    // Toggles open on the global `?` key.
    fireEvent.keyDown(document, { key: '?' });
    const btn = screen.getByRole('button', { name: 'Close' });
    expect(btn.textContent).toBe(CLOSE_GLYPH);
    expect(btn.textContent).not.toBe(BESPOKE_GLYPH);
  });

  it('DoctorModal close button uses ×', () => {
    render(<DoctorModal />);
    act(() => { dispatch({ type: 'OPEN_DOCTOR_MODAL' }); });
    const btn = screen.getByRole('button', { name: 'Close' });
    expect(btn.textContent).toBe(CLOSE_GLYPH);
    expect(btn.textContent).not.toBe(BESPOKE_GLYPH);
  });

  it('UpdateModal close button uses ×', () => {
    render(<UpdateModal />);
    act(() => {
      dispatch({
        type: 'SET_UPDATE_STATE',
        state: seedUpdateState(),
        suppress: { skipped_versions: [], remind_after: null },
      });
      dispatch({ type: 'OPEN_UPDATE_MODAL' });
    });
    const btn = screen.getByRole('button', { name: 'Close' });
    expect(btn.textContent).toBe(CLOSE_GLYPH);
    expect(btn.textContent).not.toBe(BESPOKE_GLYPH);
  });

  it('OnboardingToast dismiss button uses × (custom "Dismiss" label/class)', () => {
    // Unseen on a fresh store → the toast renders with its dismiss affordance.
    render(<OnboardingToast />);
    const btn = screen.getByRole('button', { name: 'Dismiss' });
    expect(btn).toHaveClass('onboarding-toast-dismiss');
    expect(btn.textContent).toBe(CLOSE_GLYPH);
    expect(btn.textContent).not.toBe(BESPOKE_GLYPH);
  });
});
