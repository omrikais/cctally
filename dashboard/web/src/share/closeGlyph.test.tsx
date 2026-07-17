// #207 D6 — the share/composer/preset modal shells must all use the single
// `×` (U+00D7) close glyph that the shared modals/Modal.tsx and
// Settings/Help/Doctor/Update already use — not the bespoke `⤬` (U+292C).
// This test pins each close control's rendered text to `×` so a regression
// back to `⤬` fails RED.
import { render, screen, act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ShareModal } from './ShareModal';
import { ManagePresetsModal } from './ManagePresetsModal';
import { ComposerModal } from './ComposerModal';
import { _resetForTests, dispatch, getState } from '../store/store';
import { openComposer } from '../store/shareSlice';
import {
  installGlobalKeydown, _resetForTests as _resetKeymap,
} from '../store/keymap';
import type { BasketItem } from '../store/basketSlice';
import type { ShareOptions } from './types';

const CLOSE_GLYPH = '×'; // ×

function defaultOpts(): ShareOptions {
  return {
    format: 'html', theme: 'light', reveal_projects: false,
    no_branding: false, top_n: 5, period: { kind: 'current' },
    project_allowlist: null, show_chart: true, show_table: true,
  };
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  _resetKeymap();
  installGlobalKeydown();
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
  vi.unstubAllGlobals();
});

describe('D6: share modals use the × close glyph', () => {
  it('ShareModal close button uses ×', () => {
    // The modal fetches templates on mount; a pending fetch leaves the
    // rest of the shell (incl. the close button) mounted. Stub it so no
    // real network is hit.
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => {})));
    render(<ShareModal panel="weekly" onClose={() => {}} />);
    const btn = screen.getByRole('button', { name: /close share modal/i });
    expect(btn.textContent).toBe(CLOSE_GLYPH);
  });

  it('ManagePresetsModal close button uses ×', () => {
    render(<ManagePresetsModal open={true} onClose={() => {}} />);
    const btn = screen.getByRole('button', { name: 'Close' });
    expect(btn.textContent).toBe(CLOSE_GLYPH);
  });

  it('ComposerModal (empty-basket state) close button uses ×', () => {
    act(() => { dispatch(openComposer()); });
    render(<ComposerModal />);
    expect(getState().composerModal).not.toBeNull();
    const btn = screen.getByRole('button', { name: 'Close' });
    expect(btn.textContent).toBe(CLOSE_GLYPH);
  });

  it('ComposerModal (non-empty-basket state) close button uses ×', () => {
    const items: BasketItem[] = [{
      id: 'a', panel: 'weekly', template_id: 'weekly-recap',
      options: defaultOpts(), added_at: 't', data_digest_at_add: 'sha256:abc',
      kernel_version: 1, label_hint: 'Weekly recap', source: 'claude' as const,
    }];
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => {})));
    act(() => {
      dispatch({ type: 'BASKET_HYDRATE', items });
      dispatch(openComposer());
    });
    render(<ComposerModal />);
    const btn = screen.getByRole('button', { name: 'Close' });
    expect(btn.textContent).toBe(CLOSE_GLYPH);
  });
});
