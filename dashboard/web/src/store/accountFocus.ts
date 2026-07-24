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
// Source `all` has NO account selector (account keys are provider-scoped), so
// only 'claude' / 'codex' are persisted.

import type {
  AccountCard,
  DashboardSelection,
  Envelope,
  SourceEntry,
  SourceName,
} from '../types/envelope';

export const ACCOUNT_STORAGE_PREFIX = 'cctally:dashboard:account:';

// The "All accounts" sentinel. A real accountKey is a 32-char hex string and the
// reserved sentinels are 'unattributed' / '*', so 'all' can never collide.
export const ALL_ACCOUNTS = 'all';

function storageKey(source: SourceName): string {
  return `${ACCOUNT_STORAGE_PREFIX}${source}`;
}

// Read the persisted focus for one source, or ALL_ACCOUNTS when absent /
// storage-unavailable. The value is validated against the envelope at read time
// by `resolveAccountFocus`, so a stale key survives in storage but resolves to
// All until (if ever) that account reappears.
export function loadAccountFocus(source: SourceName): string {
  try {
    const raw = localStorage.getItem(storageKey(source));
    if (raw != null && raw !== '') return raw;
  } catch {
    // localStorage unavailable → default to All.
  }
  return ALL_ACCOUNTS;
}

export function saveAccountFocus(source: SourceName, value: string): void {
  try {
    localStorage.setItem(storageKey(source), value);
  } catch {
    // localStorage unavailable → the selection just won't survive a reload.
  }
}

export function seedAccountFocus(): Record<SourceName, string> {
  return {
    claude: loadAccountFocus('claude'),
    codex: loadAccountFocus('codex'),
  };
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
// account). Source `all` is never decorated (no selector). Drives whether the
// chip row renders and whether `a` cycles.
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
