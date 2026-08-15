// #341 Task 4 — per-source account focus (Q6 Option A, spec §4/§5).
//
// The account chip is CLIENT-SIDE filter state persisted PER PHYSICAL SOURCE at
// `cctally:dashboard:account:<source>` (spec §4). The stored value is a bare
// literal: an `accountKey`, or the `ALL_ACCOUNTS` sentinel for "All accounts"
// (the default). Reconciliation is stored-valid-else-All: a selection naming an
// account that is absent from the current envelope resolves to All (the store
// never mutates the stored value on reconcile — the selector decides at read
// time, so a vanished account transparently falls back and a re-appearing one
// re-engages).
//
// #556 S5 §5.1 — source `all` DOES have an account selector now, one row per
// decorated provider. This reverses the #341 decision quoted in the paragraph
// above, and the reversal is narrow: account focus stays entirely client-side
// (the SSE hub broadcasts one identical envelope to every client and never
// receives a selection), and the new state is scoped per provider WITHIN All.
// So there are TWO SLOTS per provider — the provider's own tab and All — held
// in separate localStorage keys and never written to each other:
//
//   provider slot  `cctally:dashboard:account:<provider>`      (pre-existing)
//   All slot       `cctally:dashboard:account:all:<provider>`  (S5)
//
// Slot isolation is the invariant the epic told S5 to keep, in both directions:
// a provider-tab focus never narrows All, and an All focus never narrows the
// provider tab. The price is stated rather than discovered — a user who focuses
// under All and then switches to that provider's tab sees the tab's own
// independent focus, commonly "All accounts".
//
// No migration is required and none is written: localStorage holds one bare
// value per key, never a serialised composite, so preserving the two old keys
// and defaulting the two new ones is safe.

import type {
  AccountCard,
  DashboardSelection,
  Envelope,
  SourceEntry,
  SourceName,
} from '../types/envelope';

export const ACCOUNT_STORAGE_PREFIX = 'cctally:dashboard:account:';

// Which of a provider's two focus slots a selection reads and writes. NEVER
// inferred from the mutable `activeSource` at dispatch time — the action
// carries it explicitly, because `source: 'codex'` alone is ambiguous once a
// provider has two slots.
export type AccountFocusSlot = 'provider' | 'all';

export type AccountFocusState = {
  provider: Record<SourceName, string>;
  all: Record<SourceName, string>;
};

export function focusSlotFor(selection: DashboardSelection): AccountFocusSlot {
  return selection === 'all' ? 'all' : 'provider';
}

// The "All accounts" sentinel. A real accountKey is a 32-char hex string and the
// reserved sentinels are 'unattributed' / '*', so 'all' can never collide.
export const ALL_ACCOUNTS = 'all';

// Long account labels have to remain recognisable in narrow panel headers.
// Preserve both the distinguishing prefix and suffix instead of exposing only
// the first few characters through CSS ellipsis. The full label remains in the
// element's accessible name and title.
export function shortAccountLabel(label: string): string {
  if (label.length <= 24) return label;
  return `${label.slice(0, 12)}…${label.slice(-8)}`;
}

function storageKey(source: SourceName, slot: AccountFocusSlot): string {
  return slot === 'all'
    ? `${ACCOUNT_STORAGE_PREFIX}all:${source}`
    : `${ACCOUNT_STORAGE_PREFIX}${source}`;
}

// Read the persisted focus for one source, or ALL_ACCOUNTS when absent /
// storage-unavailable. The value is validated against the envelope at read time
// by `resolveAccountFocus`, so a stale key survives in storage but resolves to
// All until (if ever) that account reappears.
export function loadAccountFocus(
  source: SourceName,
  slot: AccountFocusSlot = 'provider',
): string {
  try {
    const raw = localStorage.getItem(storageKey(source, slot));
    if (raw != null && raw !== '') return raw;
  } catch {
    // localStorage unavailable → default to All.
  }
  return ALL_ACCOUNTS;
}

export function saveAccountFocus(
  source: SourceName,
  value: string,
  slot: AccountFocusSlot = 'provider',
): void {
  try {
    localStorage.setItem(storageKey(source, slot), value);
  } catch {
    // localStorage unavailable → the selection just won't survive a reload.
  }
}

export function seedAccountFocus(): AccountFocusState {
  return {
    provider: {
      claude: loadAccountFocus('claude', 'provider'),
      codex: loadAccountFocus('codex', 'provider'),
    },
    all: {
      claude: loadAccountFocus('claude', 'all'),
      codex: loadAccountFocus('codex', 'all'),
    },
  };
}

// The stored (unreconciled) value one view reads for one provider. Returns
// ALL_ACCOUNTS for a provider the selection does not show, so a Claude-tab read
// of the Codex slot can never leak a focus onto a surface that is not showing
// Codex.
export function storedFocusFor(
  focus: AccountFocusState,
  selection: DashboardSelection,
  provider: SourceName,
): string {
  if (selection !== 'all' && selection !== provider) return ALL_ACCOUNTS;
  return focus[focusSlotFor(selection)][provider] ?? ALL_ACCOUNTS;
}

// The per-account cards emitted on a decorated source entry (spec §4). Returns
// `null` for a <=1-real-account source (no `accounts` array), source `all` (no
// selector), or any envelope shape without the array.
export function sourceAccounts(
  entry: SourceEntry<unknown> | null,
): AccountCard[] | null {
  const data = entry?.data as { accounts?: unknown } | null | undefined;
  const accounts = data?.accounts;
  if (!Array.isArray(accounts) || accounts.length === 0) return null;
  return accounts as AccountCard[];
}

// True when the ACTIVE physical source has the per-account decoration (>1 real
// account). Source `all` is never decorated (no selector).
//
// #556 S5 Unit 2 review F8 — RETAINED ONLY as the view-shaped predicate, and no
// longer read by any component. The chip row and the `a` cycle it used to drive
// now go through `decoratedProvidersFor` / `providerIsDecorated` below, because
// under All the answer is per provider rather than per view. Kept because the
// view-shaped question ("does the source the user is looking at have a
// selector") is still a coherent one and `providerIsDecorated` cannot answer it
// for `all`; its only caller today is `accountFocus.test.ts`.
export function sourceIsDecorated(
  env: Envelope | null,
  source: DashboardSelection,
): boolean {
  if (source === 'all') return false;
  const entry = (env?.sources?.[source] ?? null) as SourceEntry<unknown> | null;
  return sourceAccounts(entry) != null;
}

// Resolve the effective focused account for a source, reconciled against the
// current envelope (stored-valid-else-All). Returns an `accountKey`, or `null`
// for All (undecorated sources always resolve to null).
export function resolveAccountFocus(
  env: Envelope | null,
  source: DashboardSelection,
  stored: string,
): string | null {
  if (source === 'all' || stored === ALL_ACCOUNTS || stored === '') return null;
  const entry = (env?.sources?.[source] ?? null) as SourceEntry<unknown> | null;
  const accounts = sourceAccounts(entry);
  if (accounts == null) return null;
  return accounts.some((a) => a.accountKey === stored) ? stored : null;
}

// #556 S5 §5.5 — decoration for ONE provider, independent of which view is
// showing it. `sourceIsDecorated` answers the older question ("is the ACTIVE
// physical source decorated") and returns false for `all`; under All the chip
// rows need the per-provider answer instead, one row per decorated provider.
export function providerIsDecorated(
  env: Envelope | null,
  provider: SourceName,
): boolean {
  return sourceAccounts(
    (env?.sources?.[provider] ?? null) as SourceEntry<unknown> | null,
  ) != null;
}

// The providers that get a chip row for a selection, in render order. On a
// provider tab that is at most that provider; under All it is every decorated
// provider.
export function decoratedProvidersFor(
  env: Envelope | null,
  selection: DashboardSelection,
): SourceName[] {
  const candidates: SourceName[] = selection === 'all'
    ? ['claude', 'codex']
    : [selection];
  return candidates.filter((provider) => providerIsDecorated(env, provider));
}

// #556 S5 §5.4 — the ONE shared resolver every focus consumer goes through.
//
// Finding B4 in this epic was three surfaces deciding the same thing
// independently and disagreeing, so the slot choice, the not-showing-this-
// provider case and the stale-key reconciliation are decided here and nowhere
// else. Stale-key validation is delegated to `resolveAccountFocus`, which is
// called with the PROVIDER (never the selection), so an All view still
// reconciles against that provider's own `accounts[]`.
export function resolveViewAccountFocus(
  env: Envelope | null,
  selection: DashboardSelection,
  provider: SourceName,
  focus: AccountFocusState,
): string | null {
  return resolveAccountFocus(
    env, provider, storedFocusFor(focus, selection, provider),
  );
}

// The `a`-cycle order over a decorated source: All → account₁ → … → accountₙ →
// All. Given the current effective focus (null = All), return the next value to
// store (an accountKey or ALL_ACCOUNTS). A no-op ALL_ACCOUNTS when undecorated.
export function nextAccountFocus(
  accounts: AccountCard[] | null,
  current: string | null,
): string {
  if (accounts == null || accounts.length === 0) return ALL_ACCOUNTS;
  const order: string[] = [ALL_ACCOUNTS, ...accounts.map((a) => a.accountKey)];
  const idx = current == null ? 0 : Math.max(0, order.indexOf(current));
  return order[(idx + 1) % order.length];
}
