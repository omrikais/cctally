// Spec §12.1 guards for the `B` binding (composer):
//   shareModal === null && composerModal === null && inputMode === null
//   && update.modalOpen === false && !mobile.
//
// The binding is registered in main.tsx alongside the other always-on
// globals. These tests drive it through the same dispatcher: install
// the global keydown handler, register the binding via the helper, then
// fire `keydown { key: 'B' }` (uppercase — the M3 spec-compliance fix
// dropped the lowercase twin to mirror the `S`-vs-`s` precedent) on
// document and assert the resulting store state.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent } from '@testing-library/react';
import {
  _resetForTests as _resetStore, dispatch, getState,
} from '../store/store';
import {
  _resetForTests as _resetKeymap, installGlobalKeydown, registerKeymap,
} from '../store/keymap';
import { buildBasketKeyBindings } from './keyboardBasket';
import { openShareModal, openComposer } from '../store/shareSlice';
import { MOBILE_MEDIA_QUERY } from '../lib/breakpoints';

function fireB() { fireEvent.keyDown(document, { key: 'B' }); }

beforeEach(() => {
  _resetStore();
  _resetKeymap();
  installGlobalKeydown();
  registerKeymap(buildBasketKeyBindings());
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: false, media: q, onchange: null,
    addEventListener: () => {}, removeEventListener: () => {},
    addListener: () => {}, removeListener: () => {},
    dispatchEvent: () => false,
  }));
});

afterEach(() => {
  _resetKeymap();
  vi.restoreAllMocks();
});

describe('B keybinding (composer)', () => {
  it('opens the composer', () => {
    fireB();
    expect(getState().composerModal).not.toBeNull();
  });

  it('does nothing when share modal open', () => {
    dispatch(openShareModal('weekly', 'weekly-panel'));
    fireB();
    expect(getState().composerModal).toBeNull();
  });

  it('does nothing when composer already open', () => {
    dispatch(openComposer());
    const before = getState().composerModal;
    fireB();
    expect(getState().composerModal).toBe(before);
  });

  it('does nothing when input mode is filter', () => {
    dispatch({ type: 'SET_INPUT_MODE', mode: 'filter' });
    fireB();
    expect(getState().composerModal).toBeNull();
  });

  it('does nothing when input mode is search', () => {
    dispatch({ type: 'SET_INPUT_MODE', mode: 'search' });
    fireB();
    expect(getState().composerModal).toBeNull();
  });

  it('does nothing when update modal is open (global-hotkey-modal-guard)', () => {
    dispatch({ type: 'OPEN_UPDATE_MODAL' });
    fireB();
    expect(getState().composerModal).toBeNull();
  });

  it('does nothing on mobile', () => {
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: q === MOBILE_MEDIA_QUERY, media: q, onchange: null,
      addEventListener: () => {}, removeEventListener: () => {},
      addListener: () => {}, removeListener: () => {},
      dispatchEvent: () => false,
    }));
    fireB();
    expect(getState().composerModal).toBeNull();
  });

  it('lowercase b does NOT trigger (uppercase-only, mirrors the S precedent)', () => {
    fireEvent.keyDown(document, { key: 'b' });
    expect(getState().composerModal).toBeNull();
  });
});
