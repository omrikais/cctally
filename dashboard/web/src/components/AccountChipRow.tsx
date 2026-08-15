import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useSnapshot } from '../hooks/useSnapshot';
import {
  ALL_ACCOUNTS,
  decoratedProvidersFor,
  focusSlotFor,
  resolveViewAccountFocus,
  sourceAccounts,
} from '../store/accountFocus';
import type {
  AccountCard,
  DashboardSelection,
  SourceEntry,
  SourceName,
} from '../types/envelope';

// #341 Task 4 — the per-account chip row (Q6 Option A, spec §5).
//
// Rendered under the source switcher for the DASHBOARD workspace ONLY when a
// provider has >1 real account (`sourceAccounts != null`); otherwise absent, so
// single-account layouts are pixel-identical. WAI-ARIA radiogroup exactly like
// SourceSwitcher: one roving tab stop, Left/Right (+ Up/Down) move focus AND
// selection with wrap, Home/End jump to the ends. Chips: "All accounts"
// (default) · one per account (label + live weekly-% hint) · the unattributed
// bucket rides the accounts array (dimmed styling from its `unattributed`
// flag). Keyboard `a` cycles the same order (globalBindings.cycleActiveAccount).
//
// #556 S5 §5.5 — under All there is ONE ROW PER DECORATED PROVIDER, each doing
// exactly what it does on that provider's own tab, and each carrying a VISIBLE
// effect qualifier. The qualifier is on screen and not only in `title` or
// `aria-label`, because finding B5 in this epic was a state word reachable only
// by tooltip. The two effects genuinely differ: only Codex publishes
// `account_scopes`, so only a Codex focus can narrow a panel.
const ROW_QUALIFIER: Record<SourceName, string> = {
  claude: 'hero and alerts only',
  codex: 'filters every panel',
};

const ROW_PROVIDER_LABEL: Record<SourceName, string> = {
  claude: 'Claude accounts',
  codex: 'Codex accounts',
};

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

function AccountChipProviderRow({
  provider,
  selection,
  labelled,
}: {
  provider: SourceName;
  selection: DashboardSelection;
  // True only under All, where two rows can be on screen at once and each
  // therefore needs its own accessible name and its own visible effect
  // qualifier. A single-provider tab keeps the pre-S5 markup byte-identical.
  labelled: boolean;
}) {
  const env = useSnapshot();
  const focusState = useSyncExternalStore(subscribeStore, () => getState().accountFocus);
  const segRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const [announce, setAnnounce] = useState('');
  const prevKeyRef = useRef<string | null>(null);

  // Effective per-account focus, computed BEFORE the conditional-render early
  // return so the announce effect (a hook) always runs in the same order.
  const entry = (env?.sources?.[provider] ?? null) as SourceEntry<unknown> | null;
  const accounts = sourceAccounts(entry);
  const focused = accounts != null
    ? resolveViewAccountFocus(env, selection, provider, focusState)
    : null;
  // The active chip key while the row is visible; null when the row is absent.
  const activeKey = accounts != null ? (focused ?? ALL_ACCOUNTS) : null;
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
      setAnnounce(
        activeKey === ALL_ACCOUNTS
          ? 'All accounts selected'
          : `${activeLabel} account selected`,
      );
    }
  }, [activeKey, activeLabel]);

  // Hooks first, then the conditional-render early return.
  if (accounts == null) return null; // undecorated → no chip row.

  const chips = buildChips(accounts);
  const labelId = `account-focus-${provider}-label`;

  const select = (i: number): void => {
    const chip = chips[i];
    dispatch({
      type: 'SET_ACCOUNT_FOCUS',
      source: provider,
      // #556 S5 §5.1 — the slot follows the VIEW, and is passed explicitly so
      // the reducer never has to infer it from the mutable `activeSource`.
      slot: focusSlotFor(selection),
      account: chip.key,
    });
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
      {...(labelled
        ? { 'aria-labelledby': labelId }
        : { 'aria-label': 'Account focus' })}
      data-testid="account-chip-row"
      data-provider={provider}
    >
      {labelled && (
        <span className="account-chip-rowlabel" id={labelId}>
          <span className="account-chip-rowprovider">{ROW_PROVIDER_LABEL[provider]}</span>
          {' '}
          <span className="account-chip-rowqualifier" data-testid={`account-chip-qualifier-${provider}`}>
            {ROW_QUALIFIER[provider]}
          </span>
        </span>
      )}
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

export function AccountChipRow() {
  const env = useSnapshot();
  const activeSource = useSyncExternalStore(subscribeStore, () => getState().activeSource);
  const view = useSyncExternalStore(subscribeStore, () => getState().view);
  if (view !== 'dashboard') return null;
  const providers = decoratedProvidersFor(env, activeSource);
  if (providers.length === 0) return null;
  if (activeSource !== 'all') {
    // Single-provider tab: exactly the pre-S5 markup, no wrapper and no label.
    return (
      <AccountChipProviderRow
        provider={providers[0]}
        selection={activeSource}
        labelled={false}
      />
    );
  }
  return (
    <div className="account-chip-rows" data-testid="account-chip-rows">
      {providers.map((provider) => (
        <AccountChipProviderRow
          key={provider}
          provider={provider}
          selection="all"
          labelled
        />
      ))}
    </div>
  );
}
