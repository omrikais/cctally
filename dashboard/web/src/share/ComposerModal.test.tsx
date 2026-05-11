// Task M3.6 — composer modal foundation (spec §8.2-§8.6).
//
// The modal subscribes to (composerModal slot, basket items) and posts
// to /api/share/compose with a 200ms debounce. Tests drive the reducer
// directly (BASKET_HYDRATE / openComposer) and fake the network with
// vi.spyOn(globalThis, 'fetch'). The recompose useEffect fires through
// setTimeout, so each "did we POST?" assertion uses waitFor to wait
// past the debounce.
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ComposerModal } from './ComposerModal';
import { _resetForTests, dispatch, getState } from '../store/store';
import { openComposer } from '../store/shareSlice';
import {
  installGlobalKeydown, _resetForTests as _resetKeymap,
} from '../store/keymap';
import type { BasketItem } from '../store/basketSlice';
import type { ShareOptions } from './types';

function defaultOpts(): ShareOptions {
  return {
    format: 'html', theme: 'light', reveal_projects: false,
    no_branding: false, top_n: 5, period: { kind: 'current' },
    project_allowlist: null, show_chart: true, show_table: true,
  };
}

function seedBasket(items: BasketItem[]) {
  dispatch({ type: 'BASKET_HYDRATE', items });
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  _resetForTests();
  _resetKeymap();
  installGlobalKeydown();
  // Default: NOT mobile (the modal renders desktop unless overridden).
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

describe('<ComposerModal>', () => {
  it('renders nothing when composer slot is empty', () => {
    const { container } = render(<ComposerModal />);
    expect(container.firstChild).toBeNull();
  });

  it('shows empty state when basket is empty', () => {
    dispatch(openComposer());
    render(<ComposerModal />);
    expect(screen.getByText(/basket is empty/i)).toBeInTheDocument();
  });

  it('fetches /api/share/compose on mount with non-empty basket', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({
        body: '<html><body><section>A</section></body></html>',
        content_type: 'text/html',
        snapshot: {
          kernel_version: 1,
          composed_at: '2026-05-11T09:00:00Z',
          section_results: [{
            snapshot_id: '00',
            drift_detected: false,
            data_digest_at_add: 'sha256:abc',
            data_digest_now: 'sha256:abc',
          }],
        },
      }),
    );
    seedBasket([{
      id: 'a', panel: 'weekly', template_id: 'weekly-recap',
      options: defaultOpts(), added_at: '2026-05-11T09:00:00Z',
      data_digest_at_add: 'sha256:abc', kernel_version: 1,
      label_hint: 'Weekly recap',
    }]);
    dispatch(openComposer());
    render(<ComposerModal />);
    await waitFor(() => expect(fetchSpy).toHaveBeenCalledWith(
      '/api/share/compose',
      expect.objectContaining({ method: 'POST' }),
    ));
  });

  it('per-section Remove dispatches BASKET_REMOVE', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({
        body: '<html />', content_type: 'text/html',
        snapshot: {
          kernel_version: 1, composed_at: 't',
          section_results: [],
        },
      }),
    );
    seedBasket([{
      id: 'a', panel: 'weekly', template_id: 'weekly-recap',
      options: defaultOpts(), added_at: '2026-05-11T09:00:00Z',
      data_digest_at_add: 'sha256:abc', kernel_version: 1,
      label_hint: 'Weekly recap',
    }]);
    dispatch(openComposer());
    render(<ComposerModal />);
    // Open the kebab first; the "Remove" entry lives inside the menu.
    fireEvent.click(screen.getByRole('button', { name: /actions for weekly recap/i }));
    fireEvent.click(screen.getByRole('button', { name: /remove weekly recap/i }));
    expect(getState().basket.items).toHaveLength(0);
  });

  it('Outdated badge shows when section drift_detected', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({
        body: '<html />', content_type: 'text/html',
        snapshot: {
          kernel_version: 1, composed_at: 't',
          section_results: [{
            snapshot_id: '00',
            drift_detected: true,
            data_digest_at_add: 'sha256:old',
            data_digest_now: 'sha256:new',
          }],
        },
      }),
    );
    seedBasket([{
      id: 'a', panel: 'weekly', template_id: 'weekly-recap',
      options: defaultOpts(), added_at: 't', data_digest_at_add: 'sha256:old',
      kernel_version: 1, label_hint: 'Weekly recap',
    }]);
    dispatch(openComposer());
    render(<ComposerModal />);
    await waitFor(() => expect(screen.getByText(/outdated/i)).toBeInTheDocument());
  });

  it('real-name banner appears when a reveal-at-add section is present and anon-on-export unchecked', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({
        body: '<html />', content_type: 'text/html',
        snapshot: { kernel_version: 1, composed_at: 't', section_results: [] },
      }),
    );
    seedBasket([{
      id: 'a', panel: 'weekly', template_id: 'weekly-recap',
      options: { ...defaultOpts(), reveal_projects: true },
      added_at: 't', data_digest_at_add: 'sha256:abc',
      kernel_version: 1, label_hint: 'Weekly recap',
    }]);
    dispatch(openComposer());
    render(<ComposerModal />);
    // Default anon-on-export is TRUE → composite reveal is FALSE → banner hidden.
    expect(screen.queryByText(/real project names/i)).toBeNull();
    // Uncheck "Anon on export" → composite reveal flips to TRUE → banner appears.
    const anonCheckbox = screen.getByLabelText(/anon on export/i) as HTMLInputElement;
    fireEvent.click(anonCheckbox);
    await waitFor(() => expect(screen.getByText(/real project names/i)).toBeInTheDocument());
    // Click "Anonymize all" → flips anon-on-export back ON → banner hides.
    fireEvent.click(screen.getByRole('button', { name: /anonymize all/i }));
    expect(screen.queryByText(/real project names/i)).toBeNull();
  });

  it('applies composer-modal-mobile class below 640px (spec §8.10)', () => {
    // Re-stub matchMedia to return true for the mobile breakpoint
    // query. useIsMobile reads the same query on first render via
    // useSyncExternalStore-style state init.
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: q.includes('640'), media: q, onchange: null,
      addEventListener: () => {}, removeEventListener: () => {},
      addListener: () => {}, removeListener: () => {},
      dispatchEvent: () => false,
    }));
    seedBasket([{
      id: 'a', panel: 'weekly', template_id: 'weekly-recap',
      options: defaultOpts(), added_at: 't',
      data_digest_at_add: 'sha256:abc', kernel_version: 1,
      label_hint: 'W',
    }]);
    dispatch(openComposer());
    const { container } = render(<ComposerModal />);
    expect(container.querySelector('.composer-modal-mobile')).not.toBeNull();
  });

  it('omits composer-modal-mobile class on desktop', () => {
    seedBasket([{
      id: 'a', panel: 'weekly', template_id: 'weekly-recap',
      options: defaultOpts(), added_at: 't',
      data_digest_at_add: 'sha256:abc', kernel_version: 1,
      label_hint: 'W',
    }]);
    dispatch(openComposer());
    const { container } = render(<ComposerModal />);
    expect(container.querySelector('.composer-modal-mobile')).toBeNull();
    // But still has the base class.
    expect(container.querySelector('.composer-modal')).not.toBeNull();
  });

  it('Close button dispatches CLOSE_COMPOSER', () => {
    seedBasket([{
      id: 'a', panel: 'weekly', template_id: 'weekly-recap',
      options: defaultOpts(), added_at: 't', data_digest_at_add: 'sha256:abc',
      kernel_version: 1, label_hint: 'Weekly recap',
    }]);
    dispatch(openComposer());
    render(<ComposerModal />);
    fireEvent.click(screen.getByRole('button', { name: /^close$/i }));
    expect(getState().composerModal).toBeNull();
  });

  it('Esc closes the composer (spec §12.1 MUST FIX regression)', () => {
    // Spec §12.1 mandates Esc closes any share/composer overlay. The
    // composer registers Esc at overlay scope with a `when:` gate
    // requiring composerModal !== null, so it fires only while the
    // composer is mounted-and-open.
    seedBasket([{
      id: 'a', panel: 'weekly', template_id: 'weekly-recap',
      options: defaultOpts(), added_at: 't', data_digest_at_add: 'sha256:abc',
      kernel_version: 1, label_hint: 'Weekly recap',
    }]);
    dispatch(openComposer());
    render(<ComposerModal />);
    expect(getState().composerModal).not.toBeNull();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(getState().composerModal).toBeNull();
  });

  it('Esc closes empty-state composer too', () => {
    // Empty basket renders the empty-state branch; the Esc binding is
    // registered before the early-return on closed, so it must still
    // fire when the user dismisses an empty composer. Explicitly seed
    // an empty basket to guard against localStorage carryover from
    // earlier tests in the file (BASKET_HYDRATE persists; the master
    // store re-reads on _resetForTests).
    seedBasket([]);
    dispatch(openComposer());
    render(<ComposerModal />);
    expect(screen.getByText(/basket is empty/i)).toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(getState().composerModal).toBeNull();
  });
});
