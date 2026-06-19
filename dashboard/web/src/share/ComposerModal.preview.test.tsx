// M1-4: the composer preview iframe is wrapped in a labeled document
// frame so the WYSIWYG white card (a light-theme export) reads as paper,
// not a broken panel on the dark dashboard. This test pins the frame DOM
// structure (the visual framing/CSS is Playwright; JSDOM can't evaluate
// @media or paint). The label reflects the live `theme` knob.
import { act, fireEvent, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ComposerModal } from './ComposerModal';
import { _resetForTests, dispatch } from '../store/store';
import { openComposer } from '../store/shareSlice';
import {
  installGlobalKeydown,
  _resetForTests as _resetKeymap,
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

function seedBasket(): BasketItem[] {
  return [{
    id: 'a', panel: 'weekly', template_id: 'weekly-recap',
    options: defaultOpts(), added_at: '2026-06-19T09:00:00Z',
    data_digest_at_add: 'sha256:abc', kernel_version: 1,
    label_hint: 'Weekly recap',
  }];
}

beforeEach(() => {
  _resetForTests();
  _resetKeymap();
  installGlobalKeydown();
  // Desktop layout (the frame structure is layout-agnostic).
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: false, media: q, onchange: null,
    addEventListener: () => {}, removeEventListener: () => {},
    addListener: () => {}, removeListener: () => {},
    dispatchEvent: () => false,
  }));
  // The composer POSTs /api/share/compose on mount; stub so it resolves.
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response(JSON.stringify({
      body: '<html><body><section>A</section></body></html>',
      content_type: 'text/html',
      snapshot: { kernel_version: 1, composed_at: 't', section_results: [] },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
  );
});

afterEach(() => {
  _resetKeymap();
  vi.restoreAllMocks();
});

describe('Composer preview frame (M1-4)', () => {
  it('wraps the preview iframe in a labeled document frame reflecting the theme', () => {
    dispatch({ type: 'BASKET_HYDRATE', items: seedBasket() });
    dispatch(openComposer());
    const { container, unmount } = render(<ComposerModal />);

    const frame = container.querySelector('.composer-preview-frame');
    expect(frame).not.toBeNull();
    const iframe = frame!.querySelector('iframe.composer-preview');
    expect(iframe).not.toBeNull();
    const label = frame!.querySelector('.composer-preview-label');
    // Default export theme is light -> caption reads "Preview · light".
    expect(label?.textContent).toMatch(/^Preview · (light|dark)$/);
    expect(label?.textContent).toBe('Preview · light');
    unmount();
  });

  it('caption flips to "Preview · dark" when the theme knob changes', () => {
    dispatch({ type: 'BASKET_HYDRATE', items: seedBasket() });
    dispatch(openComposer());
    const { container, getByDisplayValue, unmount } = render(<ComposerModal />);

    // The Theme <select> defaults to 'light'; flip it to 'dark'.
    const themeSelect = getByDisplayValue('light') as HTMLSelectElement;
    act(() => {
      fireEvent.change(themeSelect, { target: { value: 'dark' } });
    });
    const label = container.querySelector('.composer-preview-label');
    expect(label?.textContent).toBe('Preview · dark');
    unmount();
  });
});
