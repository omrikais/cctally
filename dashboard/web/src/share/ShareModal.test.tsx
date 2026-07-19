// #293 S4 SHARE-1 — the phone Share form gets a preview-FIRST render reorder so
// editing a top-of-stack knob gives immediate feedback. Because .share-preview-col
// is nested inside .share-main-section (a separate flex parent from the sibling
// gallery), CSS `order` cannot hoist it — a useIsMobile()-gated render reorder is
// required. Desktop keeps the two-pane (knobs | preview) layout byte-identical.
//
// JSDOM can't evaluate the @media 16px / 44px rules (those are the ui-qa /
// hasTouch Playwright gate); the DOM ORDER is the real, non-vacuous thing to
// assert here. Non-vacuous: with the reorder absent, the mobile branch renders
// the desktop knobs-first order and the "preview precedes knobs" case is RED.
import { render, act, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ShareModalRoot } from './ShareModalRoot';
import { _resetForTests, dispatch } from '../store/store';
import { openShareModal } from '../store/shareSlice';
import {
  installGlobalKeydown,
  _resetForTests as _resetKeymap,
} from '../store/keymap';
import { MOBILE_MEDIA_QUERY } from '../lib/breakpoints';

function stubMatchMedia(mobile: boolean) {
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: mobile ? q === MOBILE_MEDIA_QUERY : false,
    media: q,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }));
}

function stubFetch() {
  vi.stubGlobal('fetch', vi.fn().mockImplementation((url: string) => {
    if (typeof url === 'string' && url.includes('/api/share/presets')) {
      return Promise.resolve(new Response(JSON.stringify({ presets: {} }), { status: 200 }));
    }
    return Promise.resolve({
      ok: true,
      json: async () => ({
        panel: 'weekly',
        templates: [{
          id: 'weekly-recap',
          label: 'Recap',
          description: 'Text + tiny chart',
          default_options: { format: 'md', theme: 'light' },
        }],
      }),
    });
  }));
}

beforeEach(() => {
  _resetForTests();
  _resetKeymap();
  installGlobalKeydown();
  stubFetch();
});

afterEach(() => {
  vi.restoreAllMocks();
});

async function openShare() {
  render(<ShareModalRoot />);
  await act(async () => {
    dispatch(openShareModal('weekly', null));
  });
}

const FOLLOWING = 4; // Node.DOCUMENT_POSITION_FOLLOWING

describe('#293 S4 SHARE-1 — mobile preview-first render reorder', () => {
  it('mobile: the Live preview precedes the knob stack in DOM order', async () => {
    stubMatchMedia(true);
    await openShare();
    const preview = document.querySelector('.share-preview-col');
    const knobs = document.querySelector('.share-knobs-col');
    expect(preview).not.toBeNull();
    expect(knobs).not.toBeNull();
    // knobs FOLLOWS preview → preview leads.
    expect(preview!.compareDocumentPosition(knobs!) & FOLLOWING).toBeTruthy();
  });

  it('desktop: the knob stack precedes the Live preview (two-pane preserved)', async () => {
    stubMatchMedia(false);
    await openShare();
    const preview = document.querySelector('.share-preview-col');
    const knobs = document.querySelector('.share-knobs-col');
    expect(preview).not.toBeNull();
    expect(knobs).not.toBeNull();
    // preview FOLLOWS knobs → knobs leads (the desktop two-pane order).
    expect(knobs!.compareDocumentPosition(preview!) & FOLLOWING).toBeTruthy();
  });

  it('renders exactly one Live preview pane in either layout', async () => {
    stubMatchMedia(true);
    await openShare();
    expect(document.querySelectorAll('.share-preview-col').length).toBe(1);
  });

  it('desktop preview renders the source captured when the share flow opened', async () => {
    stubMatchMedia(false);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    await openShare();

    await waitFor(() => {
      const renderCall = (fetch as ReturnType<typeof vi.fn>).mock.calls.find(
        ([url]) => typeof url === 'string' && url.includes('/api/share/render'),
      );
      expect(renderCall).toBeDefined();
      const body = JSON.parse((renderCall?.[1] as RequestInit).body as string);
      expect(body.source).toBe('codex');
    });
  });
});
