// basketSlice — plan §M3.3 reducer contract. Pure-reducer tests
// (no store boot) cover the four primary actions plus capacity guards
// and hydrate. localStorage round-trip is covered by master-store
// integration tests (store.basket.test.ts).
import { describe, expect, it } from 'vitest';
import {
  basketReducer, initialBasketState, makeBasketItem,
  type BasketSlice,
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

function fixture(id: string, panel: 'weekly' | 'daily' = 'weekly') {
  return makeBasketItem({
    id,
    panel,
    template_id: `${panel}-recap`,
    options: defaults(),
    added_at: '2026-05-11T09:00:00Z',
    data_digest_at_add: 'sha256:abc',
    kernel_version: 1,
    label_hint: `${panel} recap`,
  });
}

describe('basketReducer', () => {
  it('ADD appends to items', () => {
    const out = basketReducer(initialBasketState, { type: 'BASKET_ADD', item: fixture('a') });
    expect(out.items).toHaveLength(1);
    expect(out.items[0].id).toBe('a');
  });

  it('ADD enforces hard cap of 20 (drops the new one, surfaces rejected reason)', () => {
    let state: BasketSlice = initialBasketState;
    for (let i = 0; i < 20; i += 1) {
      state = basketReducer(state, { type: 'BASKET_ADD', item: fixture(`a${i}`) });
    }
    expect(state.items).toHaveLength(20);
    const after = basketReducer(state, { type: 'BASKET_ADD', item: fixture('overflow') });
    expect(after.items).toHaveLength(20);
    expect(after.items.find((it) => it.id === 'overflow')).toBeUndefined();
    expect(after.rejectedReason).toBe('capacity');
  });

  it('REMOVE filters by id', () => {
    const seeded = basketReducer(initialBasketState, { type: 'BASKET_ADD', item: fixture('a') });
    const seeded2 = basketReducer(seeded, { type: 'BASKET_ADD', item: fixture('b') });
    const out = basketReducer(seeded2, { type: 'BASKET_REMOVE', id: 'a' });
    expect(out.items.map((it) => it.id)).toEqual(['b']);
  });

  it('REORDER swaps two indices', () => {
    let state = initialBasketState;
    for (const id of ['a', 'b', 'c']) {
      state = basketReducer(state, { type: 'BASKET_ADD', item: fixture(id) });
    }
    const out = basketReducer(state, { type: 'BASKET_REORDER', fromIdx: 0, toIdx: 2 });
    expect(out.items.map((it) => it.id)).toEqual(['b', 'c', 'a']);
  });

  it('REORDER no-op when indices match (referential equality preserved)', () => {
    const seeded = basketReducer(initialBasketState, { type: 'BASKET_ADD', item: fixture('a') });
    const out = basketReducer(seeded, { type: 'BASKET_REORDER', fromIdx: 0, toIdx: 0 });
    expect(out).toBe(seeded);
  });

  it('REORDER no-op when an index is out of bounds (referential equality preserved)', () => {
    const seeded = basketReducer(initialBasketState, { type: 'BASKET_ADD', item: fixture('a') });
    const out = basketReducer(seeded, { type: 'BASKET_REORDER', fromIdx: 0, toIdx: 5 });
    expect(out).toBe(seeded);
  });

  it('CLEAR empties the list', () => {
    const seeded = basketReducer(initialBasketState, { type: 'BASKET_ADD', item: fixture('a') });
    const out = basketReducer(seeded, { type: 'BASKET_CLEAR' });
    expect(out.items).toEqual([]);
  });

  it('CLEAR_REJECTED clears the capacity sentinel without touching items', () => {
    let state: BasketSlice = initialBasketState;
    for (let i = 0; i < 20; i += 1) {
      state = basketReducer(state, { type: 'BASKET_ADD', item: fixture(`a${i}`) });
    }
    state = basketReducer(state, { type: 'BASKET_ADD', item: fixture('overflow') });
    expect(state.rejectedReason).toBe('capacity');
    const out = basketReducer(state, { type: 'BASKET_CLEAR_REJECTED' });
    expect(out.rejectedReason).toBeNull();
    expect(out.items).toHaveLength(20);
  });

  it('HYDRATE wholesale-replaces items and clears rejection', () => {
    const out = basketReducer(
      { items: [], rejectedReason: 'capacity' },
      { type: 'BASKET_HYDRATE', items: [fixture('x'), fixture('y')] },
    );
    expect(out.items.map((it) => it.id)).toEqual(['x', 'y']);
    expect(out.rejectedReason).toBeNull();
  });
});

describe('makeBasketItem', () => {
  it('uses provided id when set', () => {
    const it = makeBasketItem({
      id: 'fixed-id',
      panel: 'weekly',
      template_id: 'weekly-recap',
      options: defaults(),
      added_at: '2026-05-11T09:00:00Z',
      data_digest_at_add: 'sha256:abc',
      kernel_version: 1,
      label_hint: 'Weekly recap',
    });
    expect(it.id).toBe('fixed-id');
  });

  it('generates a non-empty id when omitted (and unique across calls)', () => {
    const a = makeBasketItem({
      panel: 'weekly',
      template_id: 'weekly-recap',
      options: defaults(),
      added_at: '2026-05-11T09:00:00Z',
      data_digest_at_add: 'sha256:abc',
      kernel_version: 1,
      label_hint: 'Weekly recap',
    });
    const b = makeBasketItem({
      panel: 'weekly',
      template_id: 'weekly-recap',
      options: defaults(),
      added_at: '2026-05-11T09:00:00Z',
      data_digest_at_add: 'sha256:abc',
      kernel_version: 1,
      label_hint: 'Weekly recap',
    });
    expect(a.id).toBeTruthy();
    expect(b.id).toBeTruthy();
    expect(a.id).not.toBe(b.id);
  });
});
