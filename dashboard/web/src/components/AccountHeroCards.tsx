import { useSyncExternalStore } from 'react';
import { getState, subscribeStore } from '../store/store';
import { useSnapshot } from '../hooks/useSnapshot';
import { humanizeDuration } from '../lib/syncFreshness';
import {
  ALL_ACCOUNTS,
  resolveAccountFocus,
  sourceAccounts,
} from '../store/accountFocus';
import { useAccountScope } from '../hooks/useScopedSnapshot';
import type { AccountCard, SourceEntry, SourceName } from '../types/envelope';

// #341 Task 4 — the unified per-account hero cards (spec §5). Rendered under the
// hero for a DECORATED physical source: "All accounts" shows one card per
// account; a focused chip narrows to that one card. Each real card carries the
// label, plan chip, weekly/5h bars in the account color, a reset countdown, and
// weekly spend; the unattributed card renders DIMMED with totals only (no live
// bars). Absent for a <=1-real-account source, so single-account layouts are
// unchanged (R8).
//
// #416 QA — the strip ALSO renders on the ALL-PROVIDERS tab, for Codex. It used
// to self-hide there ("account cards are provider-scoped"), which was right while
// the strip was only a companion to the per-provider account CHIP — the chip is
// provider-scoped and the combined tab has none. It is wrong now that the strip
// is the DISCLOSURE that makes a blanked headline honest: the combined hero
// blanks its Codex percent, countdown and quota row precisely because each
// account owns an independent cycle (D6), and spec §6 names the per-account strip
// as the thing those slots point at. Without it the combined tab would point at
// nothing. It renders unfocused and provider-labelled there — Codex only, because
// Codex is the only provider whose numbers this tab blanks.

// Deterministic per-account color palette, assigned by registry order (spec §5).
const ACCOUNT_COLORS = [
  '#5b8def', '#e0913b', '#3fa66a', '#b569d6', '#d15b7f', '#3fb6b0',
];

function resetCountdown(resetsAt: string | null, nowMs: number): string | null {
  if (resetsAt == null) return null;
  const ms = Date.parse(resetsAt);
  if (Number.isNaN(ms)) return null;
  const secs = Math.max(0, Math.round((ms - nowMs) / 1000));
  // At/after the reset boundary `secs` clamps to 0 (#341 ui-qa P3).
  if (secs === 0) return 'resets now';
  // #416 QA P2-A: this is a FUTURE interval, so it takes the direction-free
  // duration. The boundary case above was special-cased in #341 while the
  // ordinary future path kept calling `humanizeAge`, whose " ago" suffix is
  // unconditional — so every card with a future reset read "resets in 2d 2h
  // ago". The hero's own countdown (`fmt.ddhh`) reads correctly, so the two
  // contradicted each other side by side.
  return `resets in ${humanizeDuration(secs)}`;
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
  const scope = useAccountScope();
  const activeSource = useSyncExternalStore(subscribeStore, () => getState().activeSource);
  const focusSlot = useSyncExternalStore(
    subscribeStore,
    () => (activeSource === 'all' ? ALL_ACCOUNTS : getState().accountFocus[activeSource as SourceName]),
  );
  // The combined tab has no account chip and no provider context, so it shows
  // every decorated physical provider unfocused and labels each group.
  const combined = activeSource === 'all';
  const sources: SourceName[] = combined
    ? ['claude', 'codex']
    : [activeSource as SourceName];
  const groups = sources.flatMap((source) => {
    const entry = (env?.sources?.[source] ?? null) as SourceEntry<unknown> | null;
    const accounts = sourceAccounts(entry);
    return accounts == null ? [] : [{ source, accounts }];
  });
  if (groups.length === 0) return null; // <=1 real account everywhere → no cards.

  // A focus stored for the Codex TAB must never narrow the combined tab: there
  // is no chip there to show or undo it, so a narrowed strip would silently
  // reintroduce exactly the one-account-as-the-whole-picture defect this fixes.
  const providerGroup = groups[0];
  const source = providerGroup.source;
  const focused = combined
    ? null
    : resolveAccountFocus(env, source, focusSlot ?? ALL_ACCOUNTS);
  // #416 §6 — the explicit empty state. An account with neither accounting rows
  // nor quota evidence renders a NAMED "no activity" note with its bars and
  // reset blank, rather than silently inheriting the previous account's numbers
  // (the literal reported symptom) or painting a blank panel with no
  // explanation. Only the provider that actually emits per-account children can
  // know this, so it comes from the scope chokepoint, never from the card.
  // Wording is deliberate: the server's `is_empty` means "owns neither
  // accounting rows nor quota evidence in the loaded window", NOT "nothing in
  // the current cycle" — an account can carry spend outside its cycle and still
  // have no live cycle, and that case shows its spend with blank bars instead.
  const emptyNote = !combined && scope.isEmpty && scope.card != null
    ? `No ${source === 'codex' ? 'Codex' : 'Claude'} activity recorded for ${scope.card.label}.`
    : null;

  return (
    <div className="account-hero-cards" data-testid="account-hero-cards">
      {groups.map((group) => {
        const groupFocused = combined ? null : focused;
        const visible = groupFocused == null
          ? group.accounts
          : group.accounts.filter((card) => card.accountKey === groupFocused);
        return (
          <div className="account-hero-provider-group" data-source={group.source} key={group.source}>
            {combined && (
              <p className="account-hero-caption" data-testid="account-hero-caption">
                {group.source === 'claude' ? 'Claude' : 'Codex'} accounts — each has its own quota cycle.
              </p>
            )}
            {visible.map((card) => {
              const idx = group.accounts.findIndex((item) => item.accountKey === card.accountKey);
              return (
                <AccountHeroCard
                  key={card.accountKey}
                  card={card}
                  color={ACCOUNT_COLORS[(idx < 0 ? 0 : idx) % ACCOUNT_COLORS.length]}
                  focused={groupFocused === card.accountKey}
                />
              );
            })}
          </div>
        );
      })}
      {emptyNote != null && (
        <p
          className="account-hero-empty"
          data-testid="account-hero-empty"
          role="status"
        >
          {emptyNote}
        </p>
      )}
    </div>
  );
}
