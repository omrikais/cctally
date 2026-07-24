import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useSnapshot } from '../hooks/useSnapshot';
import {
  ALL_ACCOUNTS,
  resolveAccountFocus,
  sourceAccounts,
} from '../store/accountFocus';
import type { AccountCard, SourceEntry, SourceName } from '../types/envelope';

// #341 Task 4 — the per-account chip row (Q6 Option A, spec §5).
//
// Rendered under the source switcher for the DASHBOARD workspace ONLY when the
// ACTIVE physical source has >1 real account (`sourceAccounts != null`);
// otherwise absent, so single-account layouts are pixel-identical and source
// `all` never shows it. WAI-ARIA radiogroup exactly like SourceSwitcher: one
// roving tab stop, Left/Right (+ Up/Down) move focus AND selection with wrap,
// Home/End jump to the ends. Chips: "All accounts" (default) · one per account
// (label + live weekly-% hint) · the unattributed bucket rides the accounts
// array (dimmed styling from its `unattributed` flag). Keyboard `a` cycles the
// same order (globalBindings.cycleActiveAccount).

interface Chip {
  key: string; // ALL_ACCOUNTS or an accountKey
  label: string;
  hint: string | null;
  dimmed: boolean;
}

function buildChips(accounts: AccountCard[]): Chip[] {
  const chips: Chip[] = [{ key: ALL_ACCOUNTS, label: 'All accounts', hint: null, dimmed: false }];
  for (const a of accounts) {
    chips.push({
      key: a.accountKey,
      label: a.label,
      hint: a.weeklyPercent != null ? `${Math.round(a.weeklyPercent)}%` : null,
      dimmed: a.unattributed === true,
    });
  }
  return chips;
}

export function AccountChipRow() {
  const env = useSnapshot();
  const activeSource = useSyncExternalStore(subscribeStore, () => getState().activeSource);
  const view = useSyncExternalStore(subscribeStore, () => getState().view);
  const focusSlot = useSyncExternalStore(
    subscribeStore,
    () => (activeSource === 'all' ? ALL_ACCOUNTS : getState().accountFocus[activeSource as SourceName]),
  );
  const segRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const [announce, setAnnounce] = useState('');
  const prevKeyRef = useRef<string | null>(null);

  // Effective per-account focus, computed BEFORE the conditional-render early
  // returns so the announce effect (a hook) always runs in the same order.
  const shown = view === 'dashboard' && activeSource !== 'all';
  const source = shown ? (activeSource as SourceName) : null;
  const entry = source != null
    ? ((env?.sources?.[source] ?? null) as SourceEntry<unknown> | null)
    : null;
  const accounts = source != null ? sourceAccounts(entry) : null;
  const focused = source != null && accounts != null
    ? resolveAccountFocus(env, source, focusSlot ?? ALL_ACCOUNTS)
    : null;
  // The active chip key while the row is visible; null when the row is absent
  // (undecorated / source `all` / non-dashboard view).
  const activeKey = source != null && accounts != null ? (focused ?? ALL_ACCOUNTS) : null;
  const activeLabel = activeKey == null
    ? null
    : activeKey === ALL_ACCOUNTS
      ? 'All accounts'
      : (accounts?.find((a) => a.accountKey === activeKey)?.label ?? 'All accounts');

  // State-derived live-region announce (#341 ui-qa P3). Deriving from the store's
  // effective focus means EVERY focus change announces: the arrow/click path AND
  // the global `a` shortcut (`cycleActiveAccount`, which dispatches
  // SET_ACCOUNT_FOCUS directly) both re-render this row and land here, so the
  // announce cannot be bypassed. The mount/(re)appearance render only seeds the
  // baseline — no spurious announce — and hiding the row resets it.
  useEffect(() => {
    if (activeKey == null) {
      prevKeyRef.current = null; // row hidden → reset baseline
      return;
    }
    if (prevKeyRef.current == null) {
      prevKeyRef.current = activeKey; // seed on (re)appearance, stay silent
      return;
    }
    if (prevKeyRef.current !== activeKey) {
      prevKeyRef.current = activeKey;
      setAnnounce(`${activeLabel} account selected`);
    }
  }, [activeKey, activeLabel]);

  // Hooks first, then the conditional-render early returns.
  if (source == null || accounts == null) return null; // undecorated / all → no chip row.

  const chips = buildChips(accounts);

  const select = (i: number): void => {
    const chip = chips[i];
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source, account: chip.key });
    segRefs.current[i]?.focus();
    // Announce is state-derived from the store's focus (see the effect above),
    // so the arrow/click path and the global `a` shortcut announce identically.
  };

  const onKeyDown = (e: React.KeyboardEvent, i: number): void => {
    let next: number;
    switch (e.key) {
      case 'ArrowRight':
      case 'ArrowDown':
        next = (i + 1) % chips.length;
        break;
      case 'ArrowLeft':
      case 'ArrowUp':
        next = (i - 1 + chips.length) % chips.length;
        break;
      case 'Home':
        next = 0;
        break;
      case 'End':
        next = chips.length - 1;
        break;
      default:
        return;
    }
    e.preventDefault();
    select(next);
  };

  return (
    <div
      className="account-chip-row"
      role="radiogroup"
      aria-label="Account focus"
      data-testid="account-chip-row"
    >
      {chips.map((chip, i) => {
        const checked = chip.key === activeKey;
        return (
          <button
            key={chip.key}
            ref={(el) => {
              segRefs.current[i] = el;
            }}
            type="button"
            role="radio"
            aria-checked={checked}
            aria-label={chip.hint != null ? `${chip.label}, ${chip.hint} weekly` : chip.label}
            tabIndex={checked ? 0 : -1}
            className={`account-chip${checked ? ' is-active' : ''}${chip.dimmed ? ' is-dimmed' : ''}`}
            data-account={chip.key}
            onClick={() => select(i)}
            onKeyDown={(e) => onKeyDown(e, i)}
          >
            <span className="account-chip-label">{chip.label}</span>
            {chip.hint != null && <span className="account-chip-hint">{chip.hint}</span>}
          </button>
        );
      })}
      <div className="sr-only" role="status" aria-live="polite" data-testid="account-chip-live">
        {announce}
      </div>
    </div>
  );
}
