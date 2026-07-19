// #294 S5 Task 9 — share source identity end-to-end (§7).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act } from '@testing-library/react';
import {
  _resetForTests,
  dispatch,
  getState,
} from '../store/store';
import { openShareModal } from '../store/shareSlice';
import { shareReducer, initialShareState } from '../store/shareSlice';
import {
  loadBasketFromStorage,
  makeBasketItem,
  BASKET_STORAGE_KEY,
} from '../store/basketSlice';
import { buildComposeRequest } from './composerApi';
import { renderShare } from './api';
import {
  SHARE_PANEL_MATRIX,
  isSharePanelAllowed,
  SELECTION_LABEL,
} from './types';
import { buildShareKeyBinding } from './keyboardShare';
import type { ShareOptions } from './types';

function opts(): ShareOptions {
  return {
    format: 'md', theme: 'light', reveal_projects: false, no_branding: false,
    top_n: 5, period: { kind: 'current' }, project_allowlist: null,
    show_chart: true, show_table: true,
  };
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('OPEN_SHARE source capture (§7)', () => {
  it('stamps the active source onto shareModal.source at OPEN_SHARE', () => {
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' }));
    act(() => dispatch(openShareModal('weekly', 'weekly-panel')));
    expect(getState().shareModal?.source).toBe('codex');
  });

  it('a mid-flow SET_ACTIVE_SOURCE does NOT restamp the open flow', () => {
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' }));
    act(() => dispatch(openShareModal('weekly', 'weekly-panel')));
    expect(getState().shareModal?.source).toBe('claude');
    // Switch the global selector while the share flow is open.
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' }));
    // The captured flow source is frozen.
    expect(getState().shareModal?.source).toBe('claude');
  });

  it('a panel modal and share launched from it keep the source captured at open', () => {
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' }));
    act(() => dispatch({ type: 'OPEN_MODAL', kind: 'weekly' }));
    expect(getState().openModalSource).toBe('codex');
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' }));
    expect(getState().openModal).toBe('weekly');
    expect(getState().openModalSource).toBe('codex');
    act(() => dispatch(openShareModal('weekly', 'weekly-modal')));
    expect(getState().shareModal?.source).toBe('codex');
  });

  it('a qualified detail remains bound to its physical source after a board switch', () => {
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' }));
    act(() => dispatch({ type: 'OPEN_SOURCE_DETAIL', source: 'codex', resource: 'session', key: 'v1.native' }));
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' }));
    expect(getState().openSourceDetail).toEqual({ source: 'codex', resource: 'session', key: 'v1.native' });
  });

  it('shareReducer defaults source to claude for a bare dispatch', () => {
    const next = shareReducer(initialShareState, { type: 'OPEN_SHARE', panel: 'daily', triggerId: null });
    expect(next.shareModal?.source).toBe('claude');
  });
});

describe('renderShare / compose bodies stamp source (§7)', () => {
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it('renderShare POSTs an explicit source (including claude)', async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) =>
      new Response(JSON.stringify({ body: 'x', content_type: 'text/markdown', snapshot: {} }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    await renderShare({ panel: 'weekly', template_id: 'weekly-recap', options: opts(), source: 'claude' });
    const body = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string);
    expect(body.source).toBe('claude');
  });

  it('buildComposeRequest carries each item source per section (mixed basket)', () => {
    const claudeItem = makeBasketItem({
      panel: 'weekly', template_id: 'weekly-recap', options: opts(),
      added_at: 'a', data_digest_at_add: 'd', kernel_version: 1, label_hint: 'Weekly', source: 'claude',
    });
    const codexItem = makeBasketItem({
      panel: 'daily', template_id: 'daily-recap', options: opts(),
      added_at: 'a', data_digest_at_add: 'd', kernel_version: 1, label_hint: 'Daily', source: 'codex',
    });
    const req = buildComposeRequest([claudeItem, codexItem], {
      title: 't', theme: 'light', format: 'md', no_branding: false, reveal_projects: false,
    });
    expect(req.sections.map((s) => s.snapshot.source)).toEqual(['claude', 'codex']);
  });
});

describe('basket legacy-item load (§7)', () => {
  it('loads a legacy item (no source) as claude WITHOUT rewriting storage', () => {
    const legacy = [{
      id: 'x', panel: 'weekly', template_id: 'weekly-recap', options: opts(),
      added_at: 'a', data_digest_at_add: 'd', kernel_version: 1, label_hint: 'Weekly',
    }];
    const raw = JSON.stringify(legacy);
    localStorage.setItem(BASKET_STORAGE_KEY, raw);
    const loaded = loadBasketFromStorage();
    expect(loaded[0].source).toBe('claude');
    // Pure load — the stored bytes are byte-for-byte unchanged.
    expect(localStorage.getItem(BASKET_STORAGE_KEY)).toBe(raw);
  });
});

describe('SHARE_PANEL_MATRIX (§7)', () => {
  it('all three selections expose the same nine share-capable cards', () => {
    expect(SHARE_PANEL_MATRIX.claude.size).toBe(9);
    expect(SHARE_PANEL_MATRIX.claude.has('forecast')).toBe(true);
    expect(SHARE_PANEL_MATRIX.claude.has('trend')).toBe(true);
    expect(SHARE_PANEL_MATRIX.codex.size).toBe(9);
    expect(SHARE_PANEL_MATRIX.codex.has('forecast')).toBe(true);
    expect(SHARE_PANEL_MATRIX.codex.has('trend')).toBe(true);
    expect([...SHARE_PANEL_MATRIX.all].sort()).toEqual([...SHARE_PANEL_MATRIX.claude].sort());
  });

  it('isSharePanelAllowed keeps forecast/trend source-parity', () => {
    expect(isSharePanelAllowed('claude', 'forecast')).toBe(true);
    expect(isSharePanelAllowed('codex', 'forecast')).toBe(true);
    expect(isSharePanelAllowed('all', 'trend')).toBe(true);
  });

  it('SELECTION_LABEL covers all three selections', () => {
    expect(SELECTION_LABEL.claude).toBe('Claude');
    expect(SELECTION_LABEL.codex).toBe('Codex');
    expect(SELECTION_LABEL.all).toBe('All');
  });
});

describe('keyboardShare respects the matrix (§7)', () => {
  afterEach(() => { document.body.innerHTML = ''; });

  it('S on a forecast panel under Codex opens a source-bound share modal', () => {
    // Focus a forecast panel section.
    document.body.innerHTML = '<section data-panel-kind="forecast"><button id="f">x</button></section>';
    (document.getElementById('f') as HTMLButtonElement).focus();
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' }));
    const binding = buildShareKeyBinding();
    act(() => binding.action());
    expect(getState().shareModal?.panel).toBe('forecast');
    expect(getState().shareModal?.source).toBe('codex');
  });

  it('S on a weekly panel under Codex DOES open the share modal (in the matrix)', () => {
    document.body.innerHTML = '<section data-panel-kind="weekly"><button id="w">x</button></section>';
    (document.getElementById('w') as HTMLButtonElement).focus();
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' }));
    const binding = buildShareKeyBinding();
    act(() => binding.action());
    expect(getState().shareModal?.panel).toBe('weekly');
    expect(getState().shareModal?.source).toBe('codex');
  });
});
