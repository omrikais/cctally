// Recipe-only share-report basket — spec §7.1-§7.4.
//
// Items capture {panel, template_id, options, added_at,
// data_digest_at_add, kernel_version, label_hint} — NOT the rendered
// body. The compose endpoint re-renders from these recipes server-side
// so privacy + data-drift detection are server-anchored: the basket
// can't smuggle a body the client invented, and digest comparison at
// compose-time tells the user whether the underlying data shifted
// between add-time and compose-time.
//
// Lifecycle is intentionally separate from `shareSlice`: the basket
// persists across the share modal opening/closing (the whole point of
// a basket is to accumulate sections across multiple share-modal
// sessions). State is hoisted onto the master `UIState` and dispatched
// through the same `dispatch()` chokepoint so we keep one store; the
// master store wires the localStorage persistence side-effect and the
// capacity-reject toast.
//
// `rejectedReason` is the slot the master store reads to fire a toast
// when an ADD bounces off the hard cap. Consumers clear it via
// BASKET_CLEAR_REJECTED so the toast surface doesn't double-fire on
// the next mutation.
import type { SharePanelId, ShareOptions } from '../share/types';

export interface BasketItem {
  id: string;
  panel: SharePanelId;
  template_id: string;
  options: ShareOptions;
  added_at: string;
  data_digest_at_add: string;
  kernel_version: number;
  label_hint: string;
}

export interface BasketSlice {
  items: BasketItem[];
  rejectedReason: 'capacity' | null;
}

export const initialBasketState: BasketSlice = {
  items: [],
  rejectedReason: null,
};

// Spec §7.4 — hard cap 20, soft-warn at 18. The soft-warn threshold is
// exported so the master store / ActionBar can surface a "basket
// almost full" toast before the user hits the hard wall.
export const BASKET_HARD_CAP = 20;
export const BASKET_WARN_THRESHOLD = 18;

// Spec §7.2 — single localStorage key. Versioned with `:v1` only if
// we ship a schema migration later; right now the unversioned key
// matches the spec text verbatim.
export const BASKET_STORAGE_KEY = 'cctally:share:basket';

export type BasketAction =
  | { type: 'BASKET_ADD'; item: BasketItem }
  | { type: 'BASKET_REMOVE'; id: string }
  | { type: 'BASKET_REORDER'; fromIdx: number; toIdx: number }
  | { type: 'BASKET_CLEAR' }
  | { type: 'BASKET_CLEAR_REJECTED' }
  | { type: 'BASKET_HYDRATE'; items: BasketItem[] };

export function basketReducer(state: BasketSlice, action: BasketAction): BasketSlice {
  switch (action.type) {
    case 'BASKET_ADD': {
      if (state.items.length >= BASKET_HARD_CAP) {
        return { ...state, rejectedReason: 'capacity' };
      }
      return { items: [...state.items, action.item], rejectedReason: null };
    }
    case 'BASKET_REMOVE':
      return { ...state, items: state.items.filter((it) => it.id !== action.id) };
    case 'BASKET_REORDER': {
      if (action.fromIdx === action.toIdx) return state;
      if (action.fromIdx < 0 || action.toIdx < 0) return state;
      if (action.fromIdx >= state.items.length || action.toIdx >= state.items.length) return state;
      const next = state.items.slice();
      const [moved] = next.splice(action.fromIdx, 1);
      next.splice(action.toIdx, 0, moved);
      return { ...state, items: next };
    }
    case 'BASKET_CLEAR':
      return { items: [], rejectedReason: null };
    case 'BASKET_CLEAR_REJECTED':
      if (state.rejectedReason == null) return state;
      return { ...state, rejectedReason: null };
    case 'BASKET_HYDRATE':
      return { items: action.items, rejectedReason: null };
    default:
      return state;
  }
}

// Builds a BasketItem from a recipe. `id` is auto-generated when
// omitted — the basket is not time-sorted so we don't need monotonic
// ULIDs; a random nonce is plenty. Keeping the generator centralized
// here means the master store (which calls makeBasketItem from
// ActionBar's "+ Basket" handler) and any future tests share the same
// shape contract.
export function makeBasketItem(args: Omit<BasketItem, 'id'> & { id?: string }): BasketItem {
  return {
    id: args.id ?? cryptoRandomItemId(),
    panel: args.panel,
    template_id: args.template_id,
    options: args.options,
    added_at: args.added_at,
    data_digest_at_add: args.data_digest_at_add,
    kernel_version: args.kernel_version,
    label_hint: args.label_hint,
  };
}

// localStorage helpers — exported so `store.ts` can wire them. Both
// guard against the (rare) localStorage exceptions: a private-mode
// Safari window can throw on getItem, and a quota-exceeded setItem is
// silently swallowed (a toast surface is layered on top via the
// master store if we ever care to expose it).
export function loadBasketFromStorage(): BasketItem[] {
  try {
    const raw = localStorage.getItem(BASKET_STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(isBasketItemShape);
  } catch {
    return [];
  }
}

export function saveBasketToStorage(items: BasketItem[]): void {
  try {
    localStorage.setItem(BASKET_STORAGE_KEY, JSON.stringify(items));
  } catch {
    /* quota exceeded or storage disabled — silent */
  }
}

function isBasketItemShape(it: unknown): it is BasketItem {
  if (typeof it !== 'object' || it === null) return false;
  const r = it as Record<string, unknown>;
  return (
    typeof r.id === 'string' &&
    typeof r.panel === 'string' &&
    typeof r.template_id === 'string' &&
    typeof r.added_at === 'string' &&
    typeof r.data_digest_at_add === 'string' &&
    typeof r.kernel_version === 'number' &&
    typeof r.label_hint === 'string' &&
    typeof r.options === 'object' &&
    r.options !== null
  );
}

// Random base32-ish id, 26 chars (ULID width). We don't need ULID
// time ordering — items carry their own `added_at` — so a random
// nonce is fine and avoids a ulid dep.
function cryptoRandomItemId(): string {
  const arr = new Uint8Array(16);
  crypto.getRandomValues(arr);
  return Array.from(arr, (b) => b.toString(16).padStart(2, '0')).join('').slice(0, 26);
}
