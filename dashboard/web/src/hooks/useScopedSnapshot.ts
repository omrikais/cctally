import { useSyncExternalStore } from 'react';
import { getScopedSnapshot, getState, subscribeStore } from '../store/store';
import { scopeToAccount, type AccountScopeResult } from '../store/accountScope';
import { ALL_ACCOUNTS } from '../store/accountFocus';
import type { DashboardSelection, Envelope } from '../types/envelope';

// #416 Task 15 — the panel-facing account-scope chokepoint.
//
// `useScopedSnapshot()` is `useSnapshot()` re-scoped to the selected account:
// the merged parent under "All accounts", that account's own child under focus.
// A panel switches to it with a ONE-line change and gets scoping for free — and
// because the scope is identity-preserving when nothing is focused, an
// undecorated or unfocused dashboard renders exactly as before.
//
// Panels that must see the WHOLE population (the account chip row, the hero
// cards, the source status chip) deliberately keep plain `useSnapshot()`.
//
// A MODAL passes its own bound source (`openModalSource ?? activeSource`).
// Every modal that EXPANDS a scoped panel must use this hook: the panels were
// converted and the modals were not, so one click on an `ExpandButton` took the
// operator from the focused account's rows straight back to the merged
// all-account rows, under the focused account's chip and with no disclosure.
// `BlockModal` / `SessionModal` are the deliberate exceptions — they read only
// `snapshot.generated_at` for SSE revalidation and fetch their detail
// server-side.
export function useScopedSnapshot(source?: DashboardSelection): Envelope | null {
  return useSyncExternalStore(
    subscribeStore, () => getScopedSnapshot(undefined, source));
}

// The resolved scope itself, for the handful of surfaces that need to know
// WHICH account is focused (the hero empty state, the panel account labels, the
// `?account=` route qualifier) rather than just its data.
// `forSource` pins the scope to a specific provider instead of whatever is
// active. A modal's data comes from `useScopedSnapshot(openModalSource ??
// activeSource)`, so resolving its scope against the LIVE `activeSource`
// makes the two disagree the moment the source changes while the modal is
// open — and a caller that guards on the mismatch has to fall back to the
// harsher "could not be built" rather than the empty state it meant.
// Passing the same source it bound its data to keeps them in step.
export function useAccountScope(forSource?: DashboardSelection): AccountScopeResult {
  const env = useSyncExternalStore(subscribeStore, () => getState().snapshot);
  const active = useSyncExternalStore(subscribeStore, () => getState().activeSource);
  const source = forSource ?? active;
  const stored = useSyncExternalStore(
    subscribeStore,
    () => (source === 'all' ? ALL_ACCOUNTS : getState().accountFocus[source] ?? ALL_ACCOUNTS),
  );
  return scopeToAccount(env, source, stored);
}
