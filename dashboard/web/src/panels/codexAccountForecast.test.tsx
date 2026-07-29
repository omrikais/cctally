// #416 QA P0 — the Codex Forecast panel and modal under "All accounts".
//
// The fourth member of the same defect class: a Codex surface deriving a
// user-visible value from ONE representative window and presenting it as the
// merged truth. `presentationForecast` takes
// `histories.find(w => w.window_minutes === 10_080) ?? histories[0]`, and under
// decoration `quota.histories` carries one weekly row PER ACCOUNT — so the
// panel published whichever account sorted first, unlabelled, as "the"
// forecast. The reported symptom: `PROJECTED @ RESET ≥100%` / `⛔ OVER` above a
// hero that reads `Forecast @ reset — per account`, ~40px apart, while the
// sibling account is at 31% / OK.
//
// The modal was worse: it BLENDED. `nativeHistory` came from
// `histories[0]` while `nativeWeek` came from the MERGED weekly period rows, so
// `$ / 1%` and the two derived daily budgets belonged to no account that
// exists.
//
// D6: percentage, reset, `$/1%` and forecast never blend. The panel blanks with
// the established `per account` pointer; the modal — where a disclosure beats a
// blank — renders each account's OWN server-emitted projection, exactly as the
// cycle modal's per-account table and the All-providers strip already do.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { BlocksPanel } from './BlocksPanel';
import { ForecastPanel } from './ForecastPanel';
import { ForecastModal } from '../modals/ForecastModal';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import {
  ACCOUNT_A,
  ACCOUNT_B,
  ACCOUNT_EMPTY,
  makeAllSourceEntry,
  makeClaudeSourceEntry,
  makeCodexSourceEntry,
  makeCodexSourceData,
  makeDecoratedCodexSourceData,
  makeSourceEnvelope,
  withAccountScopedQuotaHistories,
  withSharedRootWeeklyWindows,
} from '../test-utils/sourceEnvelope';
import { presentationCodexAccountForecasts } from '../lib/dashboardPresentation';
import type { CodexSourceData, Envelope, SourcesMap } from '../types/envelope';

const NOW = '2026-04-24T13:07:00Z';

function envWith(codexData: CodexSourceData): Envelope {
  const claude = makeClaudeSourceEntry();
  const codex = makeCodexSourceEntry({ data: codexData });
  const sources = {
    claude,
    codex,
    all: makeAllSourceEntry(claude, codex),
  } as unknown as SourcesMap;
  return makeSourceEnvelope({ sources }) as unknown as Envelope;
}

function renderPanel(codexData: CodexSourceData, focus?: string) {
  updateSnapshot(envWith(codexData));
  dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
  if (focus != null) {
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', account: focus });
  }
  return render(<ForecastPanel />);
}

function renderModal(codexData: CodexSourceData, focus?: string) {
  updateSnapshot(envWith(codexData));
  dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
  if (focus != null) {
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', account: focus });
  }
  return render(<ForecastModal />);
}

const decorated = () =>
  withAccountScopedQuotaHistories(makeDecoratedCodexSourceData());

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  cleanup();
  vi.spyOn(Date, 'now').mockReturnValue(Date.parse(NOW));
});

describe('Forecast panel — "All accounts" never publishes one account', () => {
  it('does not print the representative account projection or verdict', () => {
    const { container } = renderPanel(decorated());
    const panel = container.querySelector('#panel-forecast') as HTMLElement;
    // A projects OVER the cap (104%), B is at 31% / OK. Either number alone is
    // a claim about the provider that only one account supports.
    expect(panel.textContent).not.toContain('≥100%');
    expect(panel.textContent).not.toContain('OVER');
    expect(panel.textContent).not.toContain('31%');
    expect(panel.querySelector('.fc-accent-edge')).toBeNull();
    expect(panel.className).not.toContain('fc-esc-over');
  });

  it('points at the per-account disclosure in the shared vocabulary', () => {
    renderPanel(decorated());
    const pointers = screen.getAllByTestId('forecast-per-account');
    // Both blanked slots — the projection and the current-quota foot line —
    // carry the pointer, so neither reads as a failure or as missing data.
    expect(pointers.length).toBe(2);
    for (const el of pointers) {
      expect(el.textContent).toContain('per account');
      expect(el.className).toContain('hero-per-account-value');
    }
  });

  it('restores that account\'s own forecast under focus', () => {
    const { container } = renderPanel(decorated(), ACCOUNT_B);
    const panel = container.querySelector('#panel-forecast') as HTMLElement;
    expect(panel.textContent).toContain('31%');
    expect(panel.textContent).not.toContain('per account');
  });
});

describe('Forecast modal — "All accounts" discloses instead of blending', () => {
  it('never derives a $ / 1% that belongs to no account', () => {
    const { container } = renderModal(decorated());
    // The blend was `histories[0]`'s percent paired with the MERGED weekly
    // row's `dollar_per_pct`. Neither the merged rate nor the merged daily
    // budgets may appear at all.
    expect(container.querySelector('#mfc-dpp')).toBeNull();
    expect(container.querySelector('#mfc-bud100')).toBeNull();
    expect(container.querySelector('#mfc-bud90')).toBeNull();
  });

  it('renders one row per account with that account\'s own projection', () => {
    render(<></>);
    const { container } = renderModal(decorated());
    const rows = [...container.querySelectorAll('[data-testid="forecast-account-row"]')];
    expect(rows.map((el) => el.getAttribute('data-account-key')))
      .toEqual([ACCOUNT_A, ACCOUNT_B, ACCOUNT_EMPTY]);
    expect(rows[0].textContent).toContain('work@example.com');
    expect(rows[0].textContent).toContain('104.0%');
    expect(rows[1].textContent).toContain('personal@example.com');
    expect(rows[1].textContent).toContain('31.0%');
    // An account with no weekly evidence abstains rather than borrowing one.
    expect(rows[2].textContent).not.toContain('104.0%');
    expect(rows[2].textContent).not.toContain('31.0%');
  });

  it('restores the canonical single-account modal under focus', () => {
    const { container } = renderModal(decorated(), ACCOUNT_B);
    expect(container.querySelector('[data-testid="forecast-account-row"]')).toBeNull();
    expect(container.querySelector('#mfc-dpp')).not.toBeNull();
    expect((container.querySelector('#mfc-wa-pct') as HTMLElement).textContent)
      .toBe('31.0%');
  });
});

// #416 QA P1 — a quota window whose reset has ALREADY PASSED is not a current
// quota, on any surface.
//
// Observed on the QA world: `omri@client-example.com`'s only weekly window was
// captured 2026-07-13 with `resets_at` 2026-07-19 — the reset had passed at
// capture time — and by the snapshot's clock it was nine days dead. The server
// says so: `_quota_read_model` appends an active row only when
// `baseline.resets_at > now`, so the row is absent from `quota.summary.active`
// (parent AND that account's child), and `accounts[]` carries
// `weeklyPercent: null`. Every consumer of that decision abstained — the account
// card read `Weekly —`, the hero `WEEK USAGE —`, the alerts gauge `—`, and the
// sibling per-account Current Cycle table `—`.
//
// The two forecast surfaces did not: they read `quota.histories[].forecast`
// directly, where the dead window's `current_percent: 41` and
// `confidence: 'medium'` are retained as evidence. The modal's per-account table
// printed `41.0% / medium` under CURRENT QUOTA and the focused panel foot
// printed `7-day limit: 41%` / `Confidence: medium`, so two per-account tables
// in one dashboard disagreed about whether the account had a current quota.
//
// The predicate is WINDOW LIVENESS, never forecast quality: `status === 'ok'` is
// derived from freshness and sample count alone (`forecast_quota`), so it says
// nothing about whether the window is still running, and blanking on it would
// hide a live window whose forecast is merely stale or low-confidence.
function withExpiredWeekly(
  data: CodexSourceData,
  accountKey: string,
): CodexSourceData {
  const weeklyKey = (data.quota.histories ?? []).find(
    (row) => row.account_key === accountKey && row.window_minutes === 10080,
  )?.key;
  const summary = {
    ...data.quota.summary,
    active_window_count: data.quota.summary.active_window_count - 1,
    active: data.quota.summary.active.filter((row) => row.key !== weeklyKey),
  };
  return {
    ...data,
    quota: {
      ...data.quota,
      summary,
      histories: (data.quota.histories ?? []).map((row) => (
        row.key !== weeklyKey ? row : {
          ...row,
          captured_at: '2026-04-10T09:00:00Z',
          freshness: 'stale' as const,
          forecast: {
            ...row.forecast,
            status: 'stale' as const,
            projected_percent: null,
            resets_at: '2026-04-17T00:00:00Z',
            remaining_seconds: 0,
            confidence: 'medium' as const,
          },
        }
      )),
    },
    hero: {
      ...data.hero,
      quota: summary,
      // No live cycle for that account, exactly as `_codex_accounts_wire` /
      // `hero_cycles_wire` emit it.
      cycles: (data.hero.cycles ?? []).filter((c) => c.accountKey !== accountKey),
    },
    accounts: (data.accounts ?? []).map((card) => (
      card.accountKey !== accountKey
        ? card
        : { ...card, weeklyPercent: null, resetsAt: null }
    )),
  } as CodexSourceData;
}

// A window that is genuinely LIVE but whose forecast is stale / low-confidence.
// This is the control the liveness gate must not touch: the percentage is real
// and the account is still spending against it.
function withStaleButLiveWeekly(
  data: CodexSourceData,
  accountKey: string,
): CodexSourceData {
  const weeklyKey = (data.quota.histories ?? []).find(
    (row) => row.account_key === accountKey && row.window_minutes === 10080,
  )?.key;
  return {
    ...data,
    quota: {
      ...data.quota,
      histories: (data.quota.histories ?? []).map((row) => (
        row.key !== weeklyKey ? row : {
          ...row,
          freshness: 'stale' as const,
          forecast: {
            ...row.forecast,
            status: 'stale' as const,
            projected_percent: null,
            confidence: 'low' as const,
          },
        }
      )),
    },
  } as CodexSourceData;
}

describe('An expired Codex quota window is not a current quota', () => {
  it('blanks the dead account\'s current quota and confidence in the modal', () => {
    const { container } = renderModal(
      withAccountScopedQuotaHistories(
        withExpiredWeekly(makeDecoratedCodexSourceData(), ACCOUNT_B),
      ),
    );
    const rows = [...container.querySelectorAll('[data-testid="forecast-account-row"]')];
    const dead = rows.find((el) => el.getAttribute('data-account-key') === ACCOUNT_B)!;
    // 12% was the dead window's retained `current_percent`, `medium` its
    // retained confidence. Neither describes a quota the account still holds.
    // Observed RED: `personal@example.com—12.0%medium—`.
    expect(dead.textContent).not.toContain('12.0%');
    expect(dead.textContent).not.toContain('medium');
    expect(dead.querySelectorAll('.m-unavailable').length).toBeGreaterThanOrEqual(3);
    // The live sibling is untouched — the gate is per window, not per table.
    const live = rows.find((el) => el.getAttribute('data-account-key') === ACCOUNT_A)!;
    expect(live.textContent).toContain('61.0%');
    expect(live.textContent).toContain('104.0%');
  });

  it('blanks the dead account\'s foot lines under focus', () => {
    const { container } = renderPanel(
      withAccountScopedQuotaHistories(
        withExpiredWeekly(makeDecoratedCodexSourceData(), ACCOUNT_B),
      ),
      ACCOUNT_B,
    );
    const foot = container.querySelector('.fc-budget-foot') as HTMLElement;
    // Observed RED: `7-day limit12%ConfidencemediumBudget pace—`.
    expect(foot.textContent).not.toContain('12%');
    expect(foot.textContent).not.toContain('medium');
    expect(foot.textContent).toContain('—');
    expect(foot.textContent).toContain('unavailable');
  });

  it('keeps a LIVE window whose forecast is merely stale / low-confidence', () => {
    const data = withAccountScopedQuotaHistories(
      withStaleButLiveWeekly(makeDecoratedCodexSourceData(), ACCOUNT_B),
    );
    const modal = renderModal(data);
    const row = [...modal.container.querySelectorAll('[data-testid="forecast-account-row"]')]
      .find((el) => el.getAttribute('data-account-key') === ACCOUNT_B)!;
    // The projection is legitimately absent (`status !== 'ok'`); the CURRENT
    // percentage and its confidence are real and must survive.
    expect(row.textContent).toContain('12.0%');
    expect(row.textContent).toContain('low');
    cleanup();
    const panel = renderPanel(data, ACCOUNT_B);
    const foot = panel.container.querySelector('.fc-budget-foot') as HTMLElement;
    expect(foot.textContent).toContain('12%');
    expect(foot.textContent).toContain('low');
  });

  it('gates on the window, not on the account card (unattributed)', () => {
    // `_codex_accounts_wire` forces `weeklyPercent: null` for the unattributed
    // bucket (dimmed, totals only) while its weekly window may still be LIVE and
    // listed in `quota.summary.active`. The gate is the window's liveness, so
    // that percentage stays — a card-derived gate would wrongly blank it.
    //
    // The bucket is a first-class ACCOUNT here, not a residual: it is a card in
    // `accounts_wire`, and `_codex_account_scopes_wire` is called with EVERY
    // card key, so it has its own `account_scopes` child holding its own live
    // window. The fixture used to give it a card and no child, which no wire
    // emits.
    const base = makeDecoratedCodexSourceData();
    const unattributedWeekly = {
      ...base.quota.histories.find((row) => row.window_minutes === 10080)!,
      key: 'quota:codex-weekly-unattributed',
      account_key: 'unattributed',
      current_percent: 19.5,
      forecast: {
        ...base.quota.histories.find((row) => row.window_minutes === 10080)!.forecast,
        status: 'stale' as const,
        current_percent: 19.5,
        projected_percent: null,
        confidence: 'medium' as const,
      },
    };
    const summary = {
      ...base.quota.summary,
      active: [
        ...base.quota.summary.active,
        {
          key: unattributedWeekly.key,
          current_percent: 19.5,
          captured_at: '2026-04-24T13:00:00Z',
          resets_at: '2026-04-30T00:00:00Z',
          freshness: 'fresh' as const,
          stale_after_seconds: 3600,
        },
      ],
    };
    const data = withAccountScopedQuotaHistories({
      ...base,
      account_scopes: {
        ...(base.account_scopes ?? {}),
        unattributed: {
          ...base.account_scopes![ACCOUNT_EMPTY],
          is_empty: false,
        },
      },
      quota: {
        ...base.quota,
        summary,
        histories: [...base.quota.histories, unattributedWeekly],
      },
      hero: { ...base.hero, quota: summary },
      accounts: [
        ...(base.accounts ?? []),
        {
          accountKey: 'unattributed',
          label: 'Unattributed',
          plan: null,
          active: false,
          weeklyPercent: null,
          fiveHourPercent: null,
          resetsAt: null,
          spendUsd: 0.23,
          inputTokens: 0,
          cachedInputTokens: 0,
          outputTokens: 0,
          reasoningOutputTokens: 0,
          totalTokens: 82000,
          unattributed: true as const,
        },
      ],
    } as CodexSourceData);
    const { container } = renderModal(data);
    const row = [...container.querySelectorAll('[data-testid="forecast-account-row"]')]
      .find((el) => el.getAttribute('data-account-key') === 'unattributed')!;
    expect(row.textContent).toContain('19.5%');
    expect(row.textContent).toContain('medium');
  });
});

// #416 QA P2-1 — the liveness set must be the ACCOUNT's, not the provider's.
//
// The window key excludes the account (`dashboard_resource_key("quota",
// "codex", source_root_key, logical_limit_key, observed_slot, window_minutes)`,
// and `logical_limit_key` carries no account either), so two accounts under one
// `$CODEX_HOME` root produce two history rows with ONE key. `quota.summary.active`
// on the PARENT then lists that key once, contributed by whichever account is
// live — and a key-only `has(weekly.key)` over the parent revives the dead
// sibling with its retained percentage. The per-account children exist for
// exactly this: each comes from `_quota_read_model` over that account's own
// observations, so its `summary.active` genuinely names that account's live
// windows.
describe('Two Codex accounts under ONE root collide on the quota key', () => {
  const sharedRoot = () => withSharedRootWeeklyWindows(makeDecoratedCodexSourceData());

  it('does not revive the dead account from its sibling\'s active row', () => {
    const { container } = renderModal(sharedRoot());
    const rows = [...container.querySelectorAll('[data-testid="forecast-account-row"]')];
    const dead = rows.find((el) => el.getAttribute('data-account-key') === ACCOUNT_B)!;
    // 41.0% / medium is the nine-day-dead window's retained evidence. The only
    // reason it survived the gate is that ACCOUNT_A contributed the same key.
    expect(dead.textContent).not.toContain('41.0%');
    expect(dead.textContent).not.toContain('medium');
    expect(dead.querySelectorAll('.m-unavailable').length).toBeGreaterThanOrEqual(3);
    // The account that actually owns the live window keeps everything.
    const live = rows.find((el) => el.getAttribute('data-account-key') === ACCOUNT_A)!;
    expect(live.textContent).toContain('78.2%');
    expect(live.textContent).toContain('91.0%');
    expect(live.textContent).toContain('high');
  });

  it('leaves the focused panel foot correct — it already read the child', () => {
    // The focused leg takes BOTH its histories and its liveness set from the
    // same per-account child (`composeScopedData` replaces `quota` wholesale),
    // so the collision cannot reach it. Pinned rather than assumed.
    const { container } = renderPanel(sharedRoot(), ACCOUNT_B);
    const foot = container.querySelector('.fc-budget-foot') as HTMLElement;
    expect(foot.textContent).not.toContain('41%');
    expect(foot.textContent).toContain('unavailable');
    cleanup();
    const livePanel = renderPanel(sharedRoot(), ACCOUNT_A);
    const liveFoot = livePanel.container.querySelector('.fc-budget-foot') as HTMLElement;
    expect(liveFoot.textContent).toContain('78%');
    expect(liveFoot.textContent).toContain('high');
  });

  it('abstains rather than borrowing the parent when a child is missing', () => {
    // A card with no `account_scopes` child is DRIFT — `accounts[]` and
    // `account_scopes` ship under one `_codex_decorated` gate and every card key
    // is passed to `_codex_account_scopes_wire`. When it happens anyway, the
    // account's liveness is simply unknowable, and `accountScope.ts`'s law
    // applies: degrade to an explicit empty scope, NEVER to the parent — whose
    // set under a shared root is exactly what revives the dead sibling.
    const data = sharedRoot();
    const scopes = { ...(data.account_scopes ?? {}) };
    delete scopes[ACCOUNT_A];
    const { container } = renderModal({ ...data, account_scopes: scopes });
    const orphan = [...container.querySelectorAll('[data-testid="forecast-account-row"]')]
      .find((el) => el.getAttribute('data-account-key') === ACCOUNT_A)!;
    expect(orphan.textContent).not.toContain('78.2%');
    expect(orphan.querySelectorAll('.m-unavailable').length).toBeGreaterThanOrEqual(3);
  });
});

// #416 QA P3-1 — the two sibling reads of the same dead window.
describe('A dead Codex window publishes no rate and no forecast status', () => {
  const dead = () => withAccountScopedQuotaHistories(
    withExpiredWeekly(makeDecoratedCodexSourceData(), ACCOUNT_B),
  );

  it('blanks the quota rate in the focused canonical modal', () => {
    const { container } = renderModal(dead(), ACCOUNT_B);
    // `explain.rates.week_average_pct_per_hour` comes straight off the dead
    // window's `forecast.rate_percent_per_hour`, and it is RENDERED twice —
    // under the projection (`#mfc-wa-sub`) and as the "Quota rate" row. A real
    // %/h for a window that no longer exists, beside a `—` current quota.
    expect((container.querySelector('#mfc-wa-sub') as HTMLElement).textContent)
      .toBe('—');
    const rateRow = [...container.querySelectorAll('.mfc-krow')]
      .find((el) => el.textContent?.startsWith('Quota rate'))!;
    expect(rateRow.textContent).not.toContain('1.5');
    expect(rateRow.querySelector('.m-unavailable')).not.toBeNull();
  });

  it('keeps the quota rate for a LIVE window (control)', () => {
    const { container } = renderModal(dead(), ACCOUNT_A);
    expect((container.querySelector('#mfc-wa-sub') as HTMLElement).textContent)
      .toContain('%/h');
    const rateRow = [...container.querySelectorAll('.mfc-krow')]
      .find((el) => el.textContent?.startsWith('Quota rate'))!;
    expect(rateRow.querySelector('.m-unavailable')).toBeNull();
  });

  it('reports no forecast status for a dead window on the row contract', () => {
    // Not rendered today, but `status` is on the exported
    // `CodexAccountForecastRow` — a consumer reading it would be told the dead
    // window's forecast is merely `stale`.
    const rows = presentationCodexAccountForecasts(envWith(dead()))!;
    const row = rows.find((r) => r.accountKey === ACCOUNT_B)!;
    expect(row.status).toBeNull();
    expect(rows.find((r) => r.accountKey === ACCOUNT_A)!.status).toBe('ok');
  });
});

describe('An undecorated Codex provider is untouched (R8)', () => {
  it('keeps the single-account projection, verdict and rates', () => {
    const { container } = renderPanel(makeCodexSourceData());
    const panel = container.querySelector('#panel-forecast') as HTMLElement;
    expect(panel.textContent).toContain('80%');
    expect(panel.textContent).not.toContain('per account');
    expect(container.querySelector('[data-testid="forecast-per-account"]')).toBeNull();
  });

  it('keeps the canonical modal rates and budgets', () => {
    const { container } = renderModal(makeCodexSourceData());
    expect(container.querySelector('[data-testid="forecast-account-row"]')).toBeNull();
    expect(container.querySelector('#mfc-dpp')).not.toBeNull();
    expect((container.querySelector('#mfc-wa-pct') as HTMLElement).textContent)
      .toBe('80.0%');
  });

  // The liveness gate DOES reach the undecorated path — `presentationForecast`
  // is one function for both — so the byte-stability that matters is this: with
  // a live window (the only shape a healthy single-account install has), the
  // current quota and its confidence are unchanged.
  it('keeps the single-account current quota and confidence (live window)', () => {
    const { container } = renderPanel(makeCodexSourceData());
    const foot = container.querySelector('.fc-budget-foot') as HTMLElement;
    expect(foot.textContent).toContain('Weekly limit');
    expect(foot.textContent).toContain('61%');
    expect(foot.textContent).toContain('medium');
  });
});

// #416 QA P1-A (client half) — the merged Blocks list now carries EVERY
// account's 5-hour windows, so two visually identical rows can belong to
// different accounts. Under "All accounts" each Codex row names its owner; a
// focused or undecorated view has exactly one owner and stays unlabelled.
describe('Blocks panel — merged Codex rows name their account', () => {
  function withTwoAccountBlocks(data: CodexSourceData): CodexSourceData {
    return {
      ...data,
      quota: {
        ...data.quota,
        blocks: [
          { ...data.quota.blocks[0], account_key: ACCOUNT_A },
          {
            ...data.quota.blocks[0],
            key: 'block:codex-5h-b',
            label: '18:55 Apr 24 UTC',
            cost_usd: 0.12,
            account_key: ACCOUNT_B,
          },
        ],
      },
    } as CodexSourceData;
  }

  it('labels each merged row with its owning account', () => {
    updateSnapshot(envWith(withTwoAccountBlocks(makeDecoratedCodexSourceData())));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const { container } = render(<BlocksPanel />);
    const chips = [...container.querySelectorAll('[data-testid="block-account-chip"]')];
    expect(chips.map((el) => el.textContent))
      .toEqual(['work@example.com', 'personal@example.com']);
    // Both accounts' blocks are counted, which is the whole point of the union.
    expect(container.querySelector('.panel-foot')!.textContent)
      .toContain('2 blocks');
  });

  it('adds no label under focus or without decoration (R8)', () => {
    updateSnapshot(envWith(makeCodexSourceData()));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const { container } = render(<BlocksPanel />);
    expect(container.querySelector('[data-testid="block-account-chip"]')).toBeNull();
    expect(container.querySelector('.panel-foot')!.textContent).toContain('1 blocks');
  });
});
