// #416 QA sweep — the remaining "All accounts" Codex slots that either read as
// broken or still elect one account.
//
// P3-C: the hero's `WEEK USAGE` em-dash renders at KPI size in full-brightness
// text colour, which reads as a loading skeleton rather than a deliberate
// blank. Every OTHER blanked slot on this hero already carries the dimmed
// `per account` caption; this one carries the glyph alone, so it must at least
// be dimmed.
//
// Alerts: `hero.quota.summary.latest_percent` is `max(...)` across every
// account's active window. The empty-state gauge prints it as a big
// unlabelled "%", so the gauge asserts one account's percentage as the
// provider's — the same class as the forecast, arrived at through an aggregate
// rather than an index.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/react';
import { HeroStrip } from './HeroStrip';
import { RecentAlertsPanel } from './RecentAlertsPanel';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import {
  ACCOUNT_B,
  ACCOUNT_EMPTY,
  makeAllSourceEntry,
  makeClaudeSourceEntry,
  makeCodexSourceData,
  makeCodexSourceEntry,
  makeDecoratedCodexSourceData,
  makeSourceEnvelope,
  withSharedRootWeeklyWindows,
} from '../test-utils/sourceEnvelope';
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

function renderCodex(
  Component: () => JSX.Element,
  codexData: CodexSourceData,
  focus?: string,
) {
  updateSnapshot(envWith(codexData));
  dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
  if (focus != null) {
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'provider', account: focus });
  }
  return render(<Component />);
}

// The alerts empty state only renders when there are no alert rows.
function withoutAlerts(data: CodexSourceData): CodexSourceData {
  const scopes = Object.fromEntries(
    Object.entries(data.account_scopes ?? {}).map(([key, scope]) => [
      key, { ...scope, alerts: { ...scope.alerts, rows: [] } },
    ]),
  );
  return {
    ...data,
    alerts: { ...data.alerts, rows: [] },
    ...(data.account_scopes == null ? {} : { account_scopes: scopes }),
  };
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  cleanup();
  vi.spyOn(Date, 'now').mockReturnValue(Date.parse(NOW));
});

describe('The All-accounts hero blank is styled as a blank (P3-C)', () => {
  it('dims the WEEK USAGE em-dash instead of printing it at full brightness', () => {
    const { container } = renderCodex(HeroStrip, makeDecoratedCodexSourceData());
    const num = container.querySelector('.hu-num') as HTMLElement;
    expect(num.textContent).toBe('—');
    expect(num.className).toContain('is-blank');
  });

  it('leaves a real percentage undimmed (R8 + non-vacuity)', () => {
    const { container } = renderCodex(HeroStrip, makeCodexSourceData());
    const num = container.querySelector('.hu-num') as HTMLElement;
    expect(num.textContent).toBe('61.0%');
    expect(num.className).not.toContain('is-blank');
  });
});

describe('The All-accounts alerts gauge never publishes one account percent', () => {
  it('abstains rather than printing the max active window as the provider', () => {
    const { container } = renderCodex(
      RecentAlertsPanel, withoutAlerts(makeDecoratedCodexSourceData()));
    // Non-vacuity: the empty-state gauge really did render.
    expect(container.querySelector('.ra-gauge')).not.toBeNull();
    // 61% is the representative account's weekly window; `latest_percent` is
    // the MAX across accounts, which is still one account's number.
    expect((container.querySelector('.ra-gauge-hero') as HTMLElement).textContent)
      .toBe('—');
    expect((container.querySelector('.ra-gauge-fill') as HTMLElement).style.width)
      .toBe('0%');
  });

  it('restores that account own percent under focus', () => {
    const { container } = renderCodex(
      RecentAlertsPanel, withoutAlerts(makeDecoratedCodexSourceData()), ACCOUNT_B);
    // 12% is ACCOUNT_B's OWN weekly window — the same number its `accounts[]`
    // card and its per-account forecast row carry. The fixture used to answer
    // `30` here from a hardcoded child constant that matched nothing else in
    // the envelope; `latest_percent` is a MAX over the child's active rows.
    expect((container.querySelector('.ra-gauge-hero') as HTMLElement).textContent)
      .toBe('12%');
  });

  it('keeps the single-account gauge unchanged (R8)', () => {
    const { container } = renderCodex(
      RecentAlertsPanel, withoutAlerts(makeCodexSourceData()));
    expect((container.querySelector('.ra-gauge-hero') as HTMLElement).textContent)
      .toBe('61%');
  });
});

// #564 — the decorated headline sums the cards beneath it, and a card with no
// live cycle now covers one native cycle width rather than the whole accounting
// range. The hero says so in both of its states, because the aggregate prints
// the merged figure with no date range at all and the focused view prints one
// account's figure under a bare "SPENT THIS WEEK".
function withoutFallbackCards(data: CodexSourceData): CodexSourceData {
  return {
    ...data,
    accounts: (data.accounts ?? []).map(({ spendWindow: _drop, ...card }) => card),
  };
}

describe('#564 — the decorated hero discloses the fallback window', () => {
  const NOTE = 'Includes accounts with no live cycle, counted over the last 7 days.';

  it('notes the fallback period beside the merged figure', () => {
    const { container } = renderCodex(HeroStrip, makeDecoratedCodexSourceData());
    const zone = container.querySelector('.hero-spent') as HTMLElement;
    expect(zone.textContent).toContain(NOTE);
    expect(zone.getAttribute('title')).toContain(NOTE);
    // The merged figure stays published — bounding the addends is what made it
    // honest, so withholding it would undo the fix.
    expect((zone.querySelector('.hs-big') as HTMLElement).textContent).toBe('$12.30');
  });

  // ui-qa P2: the full sentence wrapped to five lines at 375px and nine at
  // 320px because the responsive swap has nothing to swap to unless a compact
  // sibling exists. The swap itself is a `@media` rule and only the browser can
  // judge it; what this pins is that both forms are emitted, each with the class
  // the swap keys on, and that the short form still names the period.
  it('emits a mobile shorthand beside the full disclosure sentence', () => {
    const { container } = renderCodex(HeroStrip, makeDecoratedCodexSourceData());
    const note = container.querySelector(
      '[data-testid="hero-spent-note"]') as HTMLElement;
    const full = note.querySelector('.hero-ingest-backlog-label-full');
    const compact = note.querySelector('.hero-ingest-backlog-label-compact');
    expect(full?.textContent).toBe(NOTE);
    expect(compact?.textContent).toBe('Incl. no-cycle accounts · last 7 days');
  });

  // The dim treatment rides on its own class, so it cannot be lost by a note
  // that has no responsive form. Without it such a span carries no class at all
  // and falls through to the bright `.hs-sub span` metric rule.
  it('keeps the disclosure in the dim caption class, not the metric one', () => {
    const { container } = renderCodex(HeroStrip, makeDecoratedCodexSourceData());
    const note = container.querySelector(
      '[data-testid="hero-spent-note"]') as HTMLElement;
    for (const span of [...note.querySelectorAll('span')]) {
      expect(span.getAttribute('class')).not.toBeNull();
    }
    expect(note.querySelector('.hero-spent-note-text')).not.toBeNull();
  });

  it('renders no note when every card is cycle-bounded', () => {
    const { container } = renderCodex(
      HeroStrip, withoutFallbackCards(makeDecoratedCodexSourceData()));
    const zone = container.querySelector('.hero-spent') as HTMLElement;
    expect(zone.textContent).not.toContain('no live cycle');
    expect((zone.querySelector('.hs-label') as HTMLElement).textContent)
      .toBe('SPENT THIS WEEK');
  });

  it('names the window in the focused label instead of "this week"', () => {
    const { container } = renderCodex(
      HeroStrip, makeDecoratedCodexSourceData(), ACCOUNT_EMPTY);
    const label = container.querySelector('.hero-spent .hs-label') as HTMLElement;
    expect(label.textContent).toBe('SPENT · LAST 7 DAYS');
  });

  it('leaves a focused cycle-bounded account reading "this week"', () => {
    const { container } = renderCodex(
      HeroStrip, makeDecoratedCodexSourceData(), ACCOUNT_B);
    const label = container.querySelector('.hero-spent .hs-label') as HTMLElement;
    expect(label.textContent).toBe('SPENT THIS WEEK');
  });

  // Review P3: every other focused-fallback assertion runs on `ACCOUNT_EMPTY`,
  // whose card is legitimately $0.00, so none of them could tell the window
  // label apart from a label sitting over an empty state. `ACCOUNT_B` under
  // `withSharedRootWeeklyWindows` is a real account whose cycle has expired and
  // whose bounded window still carries spend.
  it('names the window over a fallback account that actually spent', () => {
    const { container } = renderCodex(
      HeroStrip,
      withSharedRootWeeklyWindows(makeDecoratedCodexSourceData()),
      ACCOUNT_B,
    );
    const zone = container.querySelector('.hero-spent') as HTMLElement;
    expect((zone.querySelector('.hs-label') as HTMLElement).textContent)
      .toBe('SPENT · LAST 7 DAYS');
    expect((zone.querySelector('.hs-big') as HTMLElement).textContent)
      .toBe('$2.15');
  });

  // Review P2: the spoken label is supplied precisely because the visible one is
  // upper case, and this is the state it exists for — a focused fallback account
  // carries no note, so the note-gated `aria-label` used to be absent entirely
  // and assistive tech read the raw `SPENT · LAST 7 DAYS`.
  it('announces the focused window label to assistive tech', () => {
    const { container } = renderCodex(
      HeroStrip,
      withSharedRootWeeklyWindows(makeDecoratedCodexSourceData()),
      ACCOUNT_B,
    );
    const zone = container.querySelector('.hero-spent') as HTMLElement;
    expect(zone.getAttribute('aria-label')).toBe('Spent over the last 7 days');
  });

  it('leaves the default week label unannounced, so nothing is said twice', () => {
    const { container } = renderCodex(
      HeroStrip, withoutFallbackCards(makeDecoratedCodexSourceData()));
    const zone = container.querySelector('.hero-spent') as HTMLElement;
    expect(zone.getAttribute('aria-label')).toBeNull();
  });
});
