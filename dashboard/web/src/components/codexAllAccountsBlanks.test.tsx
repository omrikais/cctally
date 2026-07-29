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
  makeAllSourceEntry,
  makeClaudeSourceEntry,
  makeCodexSourceData,
  makeCodexSourceEntry,
  makeDecoratedCodexSourceData,
  makeSourceEnvelope,
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
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', account: focus });
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
