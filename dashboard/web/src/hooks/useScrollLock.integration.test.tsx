// Integration test for the scroll-lock wiring (#214 M1-1). This is the
// non-vacuous "did every overlay root get wired" check: open/mount each
// of the 7 overlay roots through its real open path and assert
// `document.body.style.overflow === 'hidden'`, then close and assert it
// restores. No @media is involved — this exercises the JS wiring only,
// and is the ONLY thing that catches a missed call site (the refcount
// unit test cannot). The CSS reflows are verified in Playwright.
import { act, fireEvent, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  _resetForTests,
  dispatch,
  getState,
  type UpdateState,
} from '../store/store';
import {
  openShareModal,
  closeShareModal,
  openComposer,
  closeComposer,
} from '../store/shareSlice';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  _resetForTests as _resetKeymap,
} from '../store/keymap';
import { _resetForTests as _resetScrollLock } from './useScrollLock';
import { ModalRoot } from '../modals/ModalRoot';
import { ShareModalRoot } from '../share/ShareModalRoot';
import { DoctorModal } from '../components/DoctorModal';
import { UpdateModal } from '../components/UpdateModal';
import { SettingsOverlay } from '../components/SettingsOverlay';
import { HelpOverlay } from '../components/HelpOverlay';

// Minimal UpdateState so the UpdateModal actually renders (it returns null
// when update.state is null even with modalOpen=true — the Codex finding).
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
    configured_channel: 'stable',
  };
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  _resetKeymap();
  _resetScrollLock();
  installGlobalKeydown();
  document.body.innerHTML = '';
  document.documentElement.style.overflow = '';
  document.body.style.overflow = '';
  // The share/doctor roots fetch on mount/open; stub so nothing hangs and
  // no unhandled-rejection noise leaks. The body is irrelevant to the lock.
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ panel: 'weekly', templates: [], categories: [] }),
  }));
});

afterEach(() => {
  uninstallGlobalKeydown();
  _resetKeymap();
  document.documentElement.style.overflow = '';
  document.body.style.overflow = '';
  vi.unstubAllGlobals();
});

describe('scroll-lock wiring (per overlay root)', () => {
  it('panel Modal (via ModalRoot) locks page scroll while open, restores on close', () => {
    const { unmount } = render(<ModalRoot />);
    act(() => dispatch({ type: 'OPEN_MODAL', kind: 'alerts' }));
    expect(document.documentElement.style.overflow).toBe('hidden');
    act(() => dispatch({ type: 'CLOSE_MODAL' }));
    expect(document.documentElement.style.overflow).toBe('');
    unmount();
  });

  it('Share modal locks page scroll while open (via ShareModalRoot slot)', () => {
    const { unmount } = render(<ShareModalRoot />);
    act(() => dispatch(openShareModal('weekly', null)));
    expect(document.documentElement.style.overflow).toBe('hidden');
    act(() => dispatch(closeShareModal()));
    expect(document.documentElement.style.overflow).toBe('');
    unmount();
  });

  it('Composer locks page scroll while open (via ShareModalRoot sibling)', () => {
    const { unmount } = render(<ShareModalRoot />);
    act(() => dispatch(openComposer()));
    expect(document.documentElement.style.overflow).toBe('hidden');
    act(() => dispatch(closeComposer()));
    expect(document.documentElement.style.overflow).toBe('');
    unmount();
  });

  it('Doctor modal locks page scroll while open', () => {
    const { unmount } = render(<DoctorModal />);
    act(() => dispatch({ type: 'OPEN_DOCTOR_MODAL' }));
    expect(document.documentElement.style.overflow).toBe('hidden');
    act(() => dispatch({ type: 'CLOSE_DOCTOR_MODAL' }));
    expect(document.documentElement.style.overflow).toBe('');
    unmount();
  });

  it('Update modal locks ONLY when modalOpen AND state are both present', () => {
    const { unmount } = render(<UpdateModal />);
    // modalOpen with null state -> modal NOT rendered -> NOT locked (Codex finding).
    act(() => dispatch({ type: 'OPEN_UPDATE_MODAL' }));
    expect(getState().update.state).toBeNull();
    expect(document.documentElement.style.overflow).toBe('');
    // Give it a state so it actually renders, then assert locked.
    act(() =>
      dispatch({
        type: 'SET_UPDATE_STATE',
        state: seedUpdateState(),
        suppress: { skipped_versions: [], remind_after: null },
      }),
    );
    expect(document.documentElement.style.overflow).toBe('hidden');
    act(() => dispatch({ type: 'CLOSE_UPDATE_MODAL' }));
    expect(document.documentElement.style.overflow).toBe('');
    unmount();
  });

  it('Settings overlay locks page scroll while open (s key), restores on close', () => {
    const { unmount } = render(<SettingsOverlay />);
    // Production open path: the `s` global key (no store action).
    act(() => {
      fireEvent.keyDown(document, { key: 's' });
    });
    expect(document.documentElement.style.overflow).toBe('hidden');
    // Close via the modal-scope Esc binding the overlay registers.
    act(() => {
      fireEvent.keyDown(document, { key: 'Escape' });
    });
    expect(document.documentElement.style.overflow).toBe('');
    unmount();
  });

  it('Help overlay locks page scroll while open (? key), restores on close', () => {
    const { unmount } = render(<HelpOverlay />);
    // Production open path: the `?` global toggle key (no store action).
    act(() => {
      fireEvent.keyDown(document, { key: '?' });
    });
    expect(document.documentElement.style.overflow).toBe('hidden');
    // `?` toggles, so a second press closes it.
    act(() => {
      fireEvent.keyDown(document, { key: '?' });
    });
    expect(document.documentElement.style.overflow).toBe('');
    unmount();
  });
});
