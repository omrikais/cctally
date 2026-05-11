// Master-store integration tests for the basket slice (spec §7).
//
// These exercise the localStorage round-trip + capacity-rejection
// toast surface — the side-effects that live in the master dispatch
// wrapper rather than the pure reducer (which is covered by
// basketSlice.test.ts).
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  _resetForTests,
  dispatch,
  getState,
  loadInitialForTests,
} from './store';
import {
  BASKET_HARD_CAP,
  BASKET_STORAGE_KEY,
  makeBasketItem,
} from './basketSlice';
import type { ShareOptions } from '../share/types';

function defaults(): ShareOptions {
  return {
    format: 'html',
    theme: 'light',
    reveal_projects: true,
    no_branding: false,
    top_n: 5,
    period: { kind: 'current' },
    project_allowlist: null,
    show_chart: true,
    show_table: true,
  };
}

function fixture(id: string) {
  return makeBasketItem({
    id,
    panel: 'weekly',
    template_id: 'weekly-recap',
    options: defaults(),
    added_at: '2026-05-11T09:00:00Z',
    data_digest_at_add: 'sha256:abc',
    kernel_version: 1,
    label_hint: 'Weekly recap',
  });
}

beforeEach(() => {
  localStorage.removeItem(BASKET_STORAGE_KEY);
  _resetForTests();
});

afterEach(() => {
  localStorage.removeItem(BASKET_STORAGE_KEY);
});

describe('master store — basket persistence', () => {
  it('ADD writes the items array to localStorage', () => {
    dispatch({ type: 'BASKET_ADD', item: fixture('a') });
    const raw = localStorage.getItem(BASKET_STORAGE_KEY);
    expect(raw).toBeTruthy();
    const parsed = JSON.parse(raw!) as Array<{ id: string }>;
    expect(parsed.map((it) => it.id)).toEqual(['a']);
  });

  it('REMOVE rewrites localStorage with the remaining items', () => {
    dispatch({ type: 'BASKET_ADD', item: fixture('a') });
    dispatch({ type: 'BASKET_ADD', item: fixture('b') });
    dispatch({ type: 'BASKET_REMOVE', id: 'a' });
    const parsed = JSON.parse(localStorage.getItem(BASKET_STORAGE_KEY)!) as Array<{ id: string }>;
    expect(parsed.map((it) => it.id)).toEqual(['b']);
  });

  it('CLEAR empties localStorage', () => {
    dispatch({ type: 'BASKET_ADD', item: fixture('a') });
    dispatch({ type: 'BASKET_CLEAR' });
    const parsed = JSON.parse(localStorage.getItem(BASKET_STORAGE_KEY)!) as unknown[];
    expect(parsed).toEqual([]);
  });

  it('hydrates from localStorage on init', () => {
    // Seed storage BEFORE the store loads — loadInitialForTests reads
    // through the documented load helper so this mirrors the real
    // page-load path.
    localStorage.setItem(
      BASKET_STORAGE_KEY,
      JSON.stringify([fixture('persisted')]),
    );
    const initial = loadInitialForTests();
    expect(initial.basket.items.map((it) => it.id)).toEqual(['persisted']);
  });

  it('ADD beyond the hard cap surfaces a status toast', () => {
    for (let i = 0; i < BASKET_HARD_CAP; i += 1) {
      dispatch({ type: 'BASKET_ADD', item: fixture(`a${i}`) });
    }
    dispatch({ type: 'BASKET_ADD', item: fixture('overflow') });
    const s = getState();
    expect(s.basket.items).toHaveLength(BASKET_HARD_CAP);
    expect(s.basket.rejectedReason).toBe('capacity');
    expect(s.toast).toEqual({
      kind: 'status',
      text: 'Basket is full (20 sections). Remove one to add another.',
    });
  });

  it('REORDER persists the new order to localStorage', () => {
    for (const id of ['a', 'b', 'c']) {
      dispatch({ type: 'BASKET_ADD', item: fixture(id) });
    }
    dispatch({ type: 'BASKET_REORDER', fromIdx: 0, toIdx: 2 });
    const parsed = JSON.parse(localStorage.getItem(BASKET_STORAGE_KEY)!) as Array<{ id: string }>;
    expect(parsed.map((it) => it.id)).toEqual(['b', 'c', 'a']);
  });

  it('CLEAR_REJECTED does not rewrite localStorage (items unchanged)', () => {
    dispatch({ type: 'BASKET_ADD', item: fixture('a') });
    // Tamper externally so we can detect a redundant write.
    localStorage.setItem(BASKET_STORAGE_KEY, '__sentinel__');
    dispatch({ type: 'BASKET_CLEAR_REJECTED' });
    expect(localStorage.getItem(BASKET_STORAGE_KEY)).toBe('__sentinel__');
  });
});
