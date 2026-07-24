import { useSyncExternalStore } from 'react';
import { getState, subscribeStore } from '../store/store';
import { useSnapshot } from '../hooks/useSnapshot';
import { humanizeAge } from '../lib/syncFreshness';
import {
  ALL_ACCOUNTS,
  resolveAccountFocus,
  sourceAccounts,
} from '../store/accountFocus';
import type { AccountCard, SourceEntry, SourceName } from '../types/envelope';

// #341 Task 4 — the unified per-account hero cards (spec §5). Rendered under the
// hero for a DECORATED physical source: "All accounts" shows one card per
// account; a focused chip narrows to that one card. Each real card carries the
// label, plan chip, weekly/5h bars in the account color, a reset countdown, and
// weekly spend; the unattributed card renders DIMMED with totals only (no live
// bars). Absent for a <=1-real-account source, so single-account layouts are
// unchanged (R8).

// Deterministic per-account color palette, assigned by registry order (spec §5).
const ACCOUNT_COLORS = [
  '#5b8def', '#e0913b', '#3fa66a', '#b569d6', '#d15b7f', '#3fb6b0',
];

function resetCountdown(resetsAt: string | null, nowMs: number): string | null {
  if (resetsAt == null) return null;
  const ms = Date.parse(resetsAt);
  if (Number.isNaN(ms)) return null;
  const secs = Math.max(0, Math.round((ms - nowMs) / 1000));
  // At/after the reset boundary `secs` clamps to 0; `humanizeAge(0)` is "0s ago",
  // so `resets in ${…}` would read the contradictory "resets in 0s ago" (#341
  // ui-qa P3). Emit a non-contradictory string; the future path is untouched.
  if (secs === 0) return 'resets now';
  return `resets in ${humanizeAge(secs)}`;
}

function pctText(v: number | null): string {
  return v == null ? '—' : `${Math.round(v)}%`;
}

function AccountHeroCard({ card, color, focused }: {
  card: AccountCard;
  color: string;
  focused: boolean;
}) {
  const dimmed = card.unattributed === true;
  const nowMs = Date.now();
  const countdown = dimmed ? null : resetCountdown(card.resetsAt, nowMs);
  return (
    <div
      className={`account-hero-card${dimmed ? ' is-dimmed' : ''}${focused ? ' is-focused' : ''}`}
      data-testid="account-hero-card"
      data-account={card.accountKey}
      style={{ '--account-color': color } as React.CSSProperties}
    >
      <div className="account-hero-card-head">
        <span className="account-hero-card-label">{card.label}</span>
        {card.plan != null && <span className="account-hero-card-plan">{card.plan}</span>}
        {card.active && <span className="account-hero-card-active" title="Active account">●</span>}
      </div>
      {!dimmed && (
        <div className="account-hero-card-bars">
          <div className="account-hero-bar" data-metric="weekly">
            <span className="account-hero-bar-label">Weekly</span>
            <div className="account-hero-bar-track">
              <div
                className="account-hero-bar-fill"
                style={{ width: `${Math.min(100, card.weeklyPercent ?? 0)}%` }}
              />
            </div>
            <span className="account-hero-bar-value">{pctText(card.weeklyPercent)}</span>
          </div>
          <div className="account-hero-bar" data-metric="five-hour">
            <span className="account-hero-bar-label">5h</span>
            <div className="account-hero-bar-track">
              <div
                className="account-hero-bar-fill"
                style={{ width: `${Math.min(100, card.fiveHourPercent ?? 0)}%` }}
              />
            </div>
            <span className="account-hero-bar-value">{pctText(card.fiveHourPercent)}</span>
          </div>
        </div>
      )}
      <div className="account-hero-card-foot">
        <span className="account-hero-card-spend">${card.spendUsd.toFixed(2)}</span>
        {countdown != null && <span className="account-hero-card-reset">{countdown}</span>}
        {dimmed && <span className="account-hero-card-note">totals only</span>}
      </div>
    </div>
  );
}

export function AccountHeroCards() {
  const env = useSnapshot();
  const activeSource = useSyncExternalStore(subscribeStore, () => getState().activeSource);
  const focusSlot = useSyncExternalStore(
    subscribeStore,
    () => (activeSource === 'all' ? ALL_ACCOUNTS : getState().accountFocus[activeSource as SourceName]),
  );
  if (activeSource === 'all') return null; // account cards are provider-scoped.
  const source = activeSource as SourceName;
  const entry = (env?.sources?.[source] ?? null) as SourceEntry<unknown> | null;
  const accounts = sourceAccounts(entry);
  if (accounts == null) return null; // <=1 real account → no cards.

  const focused = resolveAccountFocus(env, source, focusSlot ?? ALL_ACCOUNTS);
  const visible = focused == null ? accounts : accounts.filter((a) => a.accountKey === focused);
  // Color is assigned by registry (array) order, stable across focus changes.
  const colorOf = (key: string): string => {
    const idx = accounts.findIndex((a) => a.accountKey === key);
    return ACCOUNT_COLORS[(idx < 0 ? 0 : idx) % ACCOUNT_COLORS.length];
  };

  return (
    <div className="account-hero-cards" data-testid="account-hero-cards">
      {visible.map((card) => (
        <AccountHeroCard
          key={card.accountKey}
          card={card}
          color={colorOf(card.accountKey)}
          focused={focused === card.accountKey}
        />
      ))}
    </div>
  );
}
