// CacheReportModal — section ordering, per-column header accents,
// today-row .cur class, settings popover, HTTP 400 error surfacing,
// empty state coverage, and a modal-level integration test that mounts
// <App /> and drives the panel-click → modal-open path (per
// feedback_modal_level_integration_test.md).
//
// Spec 2026-05-21 §7.5 + §7.6.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { CacheReportModal } from './CacheReportModal';
import { App } from '../App';
import {
  _resetForTests,
  dispatch,
  getState,
  updateSnapshot,
} from '../store/store';
import type {
  CacheReportEnvelope,
  Envelope,
} from '../types/envelope';
import { stubMobileMedia } from '../test-utils/mobileMedia';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  vi.restoreAllMocks();
});

function baseEnvelope(): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-05-20T10:00:00Z',
    last_sync_at: null,
    sync_age_s: null,
    last_sync_error: null,
    header: {
      week_label: 'wk May 20', used_pct: 0, five_hour_pct: null,
      dollar_per_pct: null, forecast_pct: null,
      forecast_verdict: 'ok', vs_last_week_delta: null,
    },
    current_week: null,
    forecast: null,
    trend: null,
    weekly: { rows: [] },
    monthly: { rows: [] },
    blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: {
      tz: 'local', resolved_tz: 'Etc/UTC',
      offset_label: 'UTC', offset_seconds: 0,
    },
    alerts: [],
    alerts_settings: {
      enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [],
    },
  };
}

function makeCacheReport(
  overrides: Partial<CacheReportEnvelope> = {},
): CacheReportEnvelope {
  const days = Array.from({ length: 14 }, (_, i) => ({
    date: `2026-05-${String(i + 7).padStart(2, '0')}`,
    cache_hit_percent: 67 + (i % 5),
    input_tokens: 1_200_000,
    output_tokens: 180_000,
    cache_creation_tokens: 200_000,
    cache_read_tokens: 2_000_000,
    saved_usd: 1.20,
    wasted_usd: 0.15,
    net_usd: 1.05,
    anomaly_triggered: false,
    anomaly_reasons: [],
  }));
  return {
    window_days: 14,
    anomaly_threshold_pp: 15,
    anomaly_window_days: 14,
    today: {
      date: '2026-05-20',
      cache_hit_percent: 68,
      baseline_median_percent: 67,
      delta_pp: -1,
      net_usd: 1.20,
      saved_usd: 1.35,
      wasted_usd: 0.15,
      anomaly_triggered: false,
      anomaly_reasons: [],
      baseline_daily_row_count: 13,
    },
    days,
    by_project: [
      { key: 'cctally', cache_hit_percent: 52, net_usd: -0.18 },
      { key: 'dotfiles', cache_hit_percent: 71, net_usd: 0.42 },
    ],
    by_model: [
      { key: 'claude-sonnet-4-6', cache_hit_percent: 67, net_usd: 1.10 },
    ],
    seven_day_net_usd: 5.94,
    seven_day_anomaly_count: 0,
    fourteen_day_counterfactual_usd: 28.40,
    fourteen_day_efficiency_ratio: 0.82,
    is_empty: false,
    ...overrides,
  };
}

function envelopeWith(cr: CacheReportEnvelope): Envelope {
  const e = baseEnvelope();
  e.cache_report = cr;
  return e;
}

describe('<CacheReportModal /> section ordering + structure', () => {
  it('renders all six section headings in order', () => {
    updateSnapshot(envelopeWith(makeCacheReport()));
    render(<CacheReportModal />);
    const dialog = screen.getByRole('dialog', { name: /cache report/i });
    const text = dialog.textContent ?? '';
    const order: RegExp[] = [
      /today's spotlight/i,
      /cache hit % — 14-day timeline/i,
      /net \$ per day/i,
      /without caching, you'd have paid/i,
      /daily rows · 14 days/i,
      /by project/i,
    ];
    let lastIdx = -1;
    for (const re of order) {
      const m = text.search(re);
      expect(m, `section ${re} not found in modal text`).toBeGreaterThan(lastIdx);
      lastIdx = m;
    }
  });

  it('counterfactual callout shows the dollar figure + efficiency ratio', () => {
    updateSnapshot(envelopeWith(makeCacheReport()));
    render(<CacheReportModal />);
    // 28.40 -> "+$28.40 more"; 0.82 -> "82%"
    expect(screen.getByText(/\+\$28\.40 more/)).toBeInTheDocument();
    // Efficiency tooltip
    expect(screen.getByText(/^82%$/)).toBeInTheDocument();
  });
});

describe('<CacheReportModal /> daily rows table', () => {
  it('has per-column header classes', () => {
    updateSnapshot(envelopeWith(makeCacheReport()));
    render(<CacheReportModal />);
    const headers = document.querySelectorAll('.ch-table thead th');
    const expectedClasses = ['c-date', 'c-hit', 'c-tokens', 'c-tokens',
                             'c-saved', 'c-wasted', 'c-net', 'c-flag'];
    expect(headers.length).toBe(expectedClasses.length);
    headers.forEach((th, i) => {
      expect(th.className).toContain(expectedClasses[i]);
    });
  });

  it('today row gets .cur class', () => {
    updateSnapshot(envelopeWith(makeCacheReport()));
    render(<CacheReportModal />);
    const rows = document.querySelectorAll('.ch-table tbody tr');
    const curRow = Array.from(rows).find((r) => r.classList.contains('cur'));
    expect(curRow).toBeTruthy();
    // today.date = 2026-05-20; CR-5 renders the date cell via fmt.calDate
    // ("May 20") while the raw date survives on the data-date attribute.
    expect(curRow?.textContent).toContain('May 20');
    expect(curRow?.getAttribute('data-date')).toBe('2026-05-20');
  });

  it('renders 14 daily rows', () => {
    updateSnapshot(envelopeWith(makeCacheReport()));
    render(<CacheReportModal />);
    const rows = document.querySelectorAll(
      '.ch-table tbody tr[data-testid="crm-daily-row"]',
    );
    expect(rows.length).toBe(14);
  });

  it('anomalous-day row gets flag-warn + hit-bad on the appropriate cells', () => {
    const cr = makeCacheReport();
    // Mark day index 5 as anomalous with a hit drop > 5 pp below baseline (67-5 = 62).
    cr.days[5] = {
      ...cr.days[5],
      cache_hit_percent: 41,
      anomaly_triggered: true,
      anomaly_reasons: ['cache_drop'],
    };
    updateSnapshot(envelopeWith(cr));
    render(<CacheReportModal />);
    const row = document.querySelector(
      `[data-testid="crm-daily-row"][data-date="${cr.days[5].date}"]`,
    );
    expect(row).toBeTruthy();
    // hit cell (2nd col) should have hit-bad; flag cell (last) should have flag-warn.
    const cells = row?.querySelectorAll('td') ?? [];
    expect(cells[1].className).toContain('hit-bad');
    expect(cells[cells.length - 1].className).toContain('flag-warn');
  });

  it('hit-bad coloring tracks the displayed ±5pp band, not the per-row anomaly flag (round-3 finding)', () => {
    // Round-3 review: the previous binding tied `hit-bad` to
    // `d.anomaly_reasons.includes('cache_drop')`, which uses
    // anomaly_threshold_pp (default 15) instead of the modal's
    // displayed ±CACHE_REPORT_BAND_PP=5 band. A day 6-14pp below
    // baseline visibly sat outside the sparkline band yet rendered
    // green; raising the threshold widened the gap.
    //
    // New contract: cell coloring follows the band, the Flag column
    // follows the per-row anomaly classifier — the two signals stay
    // independent. A row that is within the band but still
    // anomaly_triggered (e.g. net_negative-only) must render hit-good
    // alongside flag-warn; a row that is below the band but NOT
    // anomaly_triggered (e.g. between BAND_PP and anomaly_threshold_pp
    // below baseline, no net loss) must still render hit-bad with
    // flag-ok.
    const cr = makeCacheReport();
    cr.today = {
      ...cr.today,
      baseline_median_percent: 67,
    };
    // Day 1: in-band hit% but anomaly_triggered (net_negative-only).
    // -> hit-good + flag-warn (independent signals).
    cr.days[1] = {
      ...cr.days[1],
      cache_hit_percent: 65, // 67-65=2 <= BAND_PP=5
      net_usd: -0.40,
      anomaly_triggered: true,
      anomaly_reasons: ['net_negative'],
    };
    // Day 2: below-band hit% but NOT anomaly_triggered (only 10pp drop,
    // below the default 15pp anomaly_threshold_pp).
    // -> hit-bad + flag-ok.
    cr.days[2] = {
      ...cr.days[2],
      cache_hit_percent: 57, // 67-57=10 > BAND_PP=5
      anomaly_triggered: false,
      anomaly_reasons: [],
    };
    updateSnapshot(envelopeWith(cr));
    render(<CacheReportModal />);

    const row1 = document.querySelector(
      `[data-testid="crm-daily-row"][data-date="${cr.days[1].date}"]`,
    );
    expect(row1).toBeTruthy();
    const cells1 = row1?.querySelectorAll('td') ?? [];
    expect(cells1[1].className).toContain('hit-good');
    expect(cells1[1].className).not.toContain('hit-bad');
    expect(cells1[cells1.length - 1].className).toContain('flag-warn');

    const row2 = document.querySelector(
      `[data-testid="crm-daily-row"][data-date="${cr.days[2].date}"]`,
    );
    expect(row2).toBeTruthy();
    const cells2 = row2?.querySelectorAll('td') ?? [];
    expect(cells2[1].className).toContain('hit-bad');
    expect(cells2[1].className).not.toContain('hit-good');
    expect(cells2[cells2.length - 1].className).toContain('flag-ok');
  });
});

// CR-2 / CR-3 — mobile (≤640w) daily-rows card layout + short header subtitle.
// JSDOM does not evaluate @media; these cover the useIsMobile() React branch
// (labeled cards vs the desktop .ch-table, and the shortened subtitle). The
// CSS reflow itself is verified by the real-browser QA gate.
describe('<CacheReportModal /> mobile daily cards (CR-2/CR-3)', () => {
  it('renders labeled mobile cards for the daily rows', () => {
    stubMobileMedia(true);
    updateSnapshot(envelopeWith(makeCacheReport()));
    render(<CacheReportModal />);
    const cards = document.querySelectorAll('[data-testid="crm-daily-card"]');
    expect(cards.length).toBe(14);
    const card = cards[0]!;
    // Every column label rides along in the card (the run-on reflow this fixes).
    expect(card.textContent).toContain('Cache %');
    expect(card.textContent).toContain('Net');
    expect(card.textContent).toContain('Saved');
    expect(card.textContent).toContain('Wasted');
    expect(card.textContent).toContain('Tok In');
    expect(card.textContent).toContain('Tok Out');
    // Desktop table is absent on mobile.
    expect(document.querySelector('.ch-table')).toBeNull();
  });

  it('carries the net-neg / hit-bad coloring into the cards', () => {
    const cr = makeCacheReport();
    // Day 3: below-band hit% (67-57=10 > BAND_PP=5) + net-negative.
    cr.days[3] = {
      ...cr.days[3],
      cache_hit_percent: 57,
      net_usd: -0.40,
    };
    stubMobileMedia(true);
    updateSnapshot(envelopeWith(cr));
    render(<CacheReportModal />);
    const card = document.querySelector(
      `[data-testid="crm-daily-card"][data-date="${cr.days[3].date}"]`,
    )!;
    expect(card).toBeTruthy();
    expect(card.querySelector('.hit-bad')).toBeTruthy();
    expect(card.querySelector('.net-neg')).toBeTruthy();
  });

  it('renders the desktop table on wide viewports', () => {
    stubMobileMedia(false);
    updateSnapshot(envelopeWith(makeCacheReport()));
    render(<CacheReportModal />);
    expect(document.querySelector('.ch-table')).toBeTruthy();
    expect(document.querySelector('[data-testid="crm-daily-card"]')).toBeNull();
  });

  it('shortens the header subtitle on mobile so the title keeps priority (CR-3)', () => {
    stubMobileMedia(true);
    updateSnapshot(envelopeWith(makeCacheReport()));
    render(<CacheReportModal />);
    const dialog = screen.getByRole('dialog', { name: /cache report/i });
    // The long desktop subtitle is dropped; a short one takes its place.
    expect(dialog.textContent).not.toMatch(/baseline · Claude only/);
    expect(dialog.textContent).toMatch(/14d · Claude/);
  });

  it('keeps the long subtitle on desktop', () => {
    stubMobileMedia(false);
    updateSnapshot(envelopeWith(makeCacheReport()));
    render(<CacheReportModal />);
    const dialog = screen.getByRole('dialog', { name: /cache report/i });
    expect(dialog.textContent).toMatch(/14d baseline · Claude only/);
  });
});

// CR-5 — every cache calendar date routes through fmt.calDate ("May 20"),
// never the raw YYYY-MM-DD. data-date / key attributes keep the raw value.
describe('<CacheReportModal /> localizes cache dates (CR-5)', () => {
  it('renders desktop daily-row dates as "Mon DD", never raw YYYY-MM-DD', () => {
    updateSnapshot(envelopeWith(makeCacheReport()));
    render(<CacheReportModal />);
    const row = document.querySelector(
      '[data-testid="crm-daily-row"][data-date="2026-05-07"]',
    ) as HTMLElement;
    expect(row).toBeTruthy();
    expect(row.textContent).toContain('May 07');
    expect(row.textContent).not.toContain('2026-');
  });

  it('renders the spotlight today date via fmt.calDate', () => {
    updateSnapshot(envelopeWith(makeCacheReport()));
    render(<CacheReportModal />);
    const meta = document.querySelector('.crm-sh-spotlight .meta') as HTMLElement;
    expect(meta.textContent).toContain('May 20');
    expect(meta.textContent).not.toContain('2026-');
  });

  it('renders mobile daily-card dates via fmt.calDate', () => {
    stubMobileMedia(true);
    updateSnapshot(envelopeWith(makeCacheReport()));
    render(<CacheReportModal />);
    const card = document.querySelector(
      '[data-testid="crm-daily-card"][data-date="2026-05-07"]',
    ) as HTMLElement;
    expect(card.querySelector('.cd-date')?.textContent).toBe('May 07');
  });
});

describe('<CacheReportModal /> anomaly spotlight', () => {
  it('shows ⚠ Anomaly pill + reasons for an anomalous today', () => {
    updateSnapshot(envelopeWith(makeCacheReport({
      today: {
        date: '2026-05-20',
        cache_hit_percent: 49,
        baseline_median_percent: 67,
        delta_pp: -18,
        net_usd: -0.42,
        saved_usd: 0.36,
        wasted_usd: 0.78,
        anomaly_triggered: true,
        anomaly_reasons: ['cache_drop', 'net_negative'],
        baseline_daily_row_count: 13,
      },
    })));
    render(<CacheReportModal />);
    expect(screen.getByText(/⚠ Anomaly/i)).toBeInTheDocument();
    // Reasons codes
    expect(screen.getByText(/cache_drop/)).toBeInTheDocument();
    expect(screen.getByText(/net_negative/)).toBeInTheDocument();
  });

  // Regression for round-3 Codex finding: during baseline-building
  // (baseline_daily_row_count < CACHE_REPORT_MIN_BASELINE_DAYS=5),
  // `cr.today.anomaly_triggered` can already be true (the server-side
  // classifier still fires `net_negative` without a baseline). The
  // panel and modal-card chrome both stay teal/"Building baseline" on
  // such days; the spotlight pill MUST follow suit so the user does
  // not see contradictory states between the panel and the spotlight.
  // The previous precedence checked anomaly first, flipping the pill
  // to ⚠ Anomaly on the same day the panel said Building baseline.
  it('keeps the Building baseline pill (not ⚠ Anomaly) when baseline is thin even if anomaly_triggered=true', () => {
    updateSnapshot(envelopeWith(makeCacheReport({
      today: {
        date: '2026-05-20',
        cache_hit_percent: 60,
        baseline_median_percent: null, // baseline not established
        delta_pp: null,
        net_usd: -0.30,
        saved_usd: 0.10,
        wasted_usd: 0.40,
        // Server already flagged net_negative even though only 3 days
        // of history exist (cache_drop is the one the server skips
        // when samples are thin; net_negative is not gated).
        anomaly_triggered: true,
        anomaly_reasons: ['net_negative'],
        baseline_daily_row_count: 3,
      },
    })));
    render(<CacheReportModal />);
    expect(screen.getByText(/Building baseline · 3\/5 days/i)).toBeInTheDocument();
    expect(screen.queryByText(/⚠ Anomaly/i)).toBeNull();
  });
});

describe('<CacheReportModal /> modal-card severity mirror (issue #77 P3-1)', () => {
  it('uses accent-teal on a healthy day', () => {
    updateSnapshot(envelopeWith(makeCacheReport()));
    render(<CacheReportModal />);
    const card = document.querySelector('.modal-card');
    expect(card).toBeTruthy();
    expect(card!.classList.contains('accent-teal')).toBe(true);
    expect(card!.classList.contains('accent-amber')).toBe(false);
  });

  it('flips to accent-amber on an anomalous day so it matches the panel border', () => {
    updateSnapshot(envelopeWith(makeCacheReport({
      today: {
        date: '2026-05-20',
        cache_hit_percent: 49,
        baseline_median_percent: 67,
        delta_pp: -18,
        net_usd: -0.42,
        saved_usd: 0.36,
        wasted_usd: 0.78,
        anomaly_triggered: true,
        anomaly_reasons: ['cache_drop', 'net_negative'],
        baseline_daily_row_count: 13,
      },
    })));
    render(<CacheReportModal />);
    const card = document.querySelector('.modal-card');
    expect(card).toBeTruthy();
    expect(card!.classList.contains('accent-amber')).toBe(true);
    expect(card!.classList.contains('accent-teal')).toBe(false);
  });
});

describe('<CacheReportModal /> settings popover', () => {
  it('opens on gear click', () => {
    updateSnapshot(envelopeWith(makeCacheReport()));
    render(<CacheReportModal />);
    fireEvent.click(
      screen.getByRole('button', { name: /cache report settings/i }),
    );
    expect(
      screen.getByRole('dialog', { name: /cache report settings/i }),
    ).toBeInTheDocument();
  });

  // Regression for H2 (/check-review round 4): a second click on the
  // gear toggled showSettings from true→false in the onClick handler,
  // but the popover's outside-mousedown listener fired FIRST on the
  // same gesture (mousedown precedes click) and set showSettings to
  // false. The functional setState then read latest state (false) and
  // toggled BACK to true — net result, the popover re-opened and the
  // gear stopped working as a close affordance. Fix: the listener
  // exempts targets inside ``[data-cr-settings-toggle]``, so the
  // mousedown is ignored and the click cleanly toggles closed.
  it('second gear click toggles the popover CLOSED (H2 regression)', () => {
    updateSnapshot(envelopeWith(makeCacheReport()));
    render(<CacheReportModal />);
    const gear = screen.getByRole('button', { name: /cache report settings/i });
    // First click — popover opens.
    fireEvent.mouseDown(gear);
    fireEvent.click(gear);
    expect(
      screen.getByRole('dialog', { name: /cache report settings/i }),
    ).toBeInTheDocument();
    // Second click — popover MUST close (not re-open).
    fireEvent.mouseDown(gear);
    fireEvent.click(gear);
    expect(
      screen.queryByRole('dialog', { name: /cache report settings/i }),
    ).toBeNull();
  });

  it('Save dispatches POST /api/settings with the correct body', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response('{}', { status: 200 }));
    updateSnapshot(envelopeWith(makeCacheReport()));
    render(<CacheReportModal />);
    fireEvent.click(
      screen.getByRole('button', { name: /cache report settings/i }),
    );
    const input = screen.getByLabelText(/anomaly threshold/i);
    fireEvent.change(input, { target: { value: '20' } });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(fetchSpy).toHaveBeenCalled());
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe('/api/settings');
    expect((init as RequestInit).method).toBe('POST');
    expect((init as RequestInit).body).toBe(
      JSON.stringify({ cache_report: { anomaly_threshold_pp: 20 } }),
    );
    fetchSpy.mockRestore();
  });

  it('HTTP 400 error surfaces inline under the input', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          error: 'anomaly_threshold_pp must be in [1, 100]',
          field: 'anomaly_threshold_pp',
        }),
        { status: 400 },
      ),
    );
    updateSnapshot(envelopeWith(makeCacheReport()));
    render(<CacheReportModal />);
    fireEvent.click(
      screen.getByRole('button', { name: /cache report settings/i }),
    );
    const input = screen.getByLabelText(/anomaly threshold/i);
    // Pass client-side guard (use a valid in-range integer); the
    // server will reject with the mocked 400 anyway.
    fireEvent.change(input, { target: { value: '50' } });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));
    expect(
      await screen.findByText(/must be in \[1, 100\]/i),
    ).toBeInTheDocument();
  });

  it('client-side guard rejects non-integer / out-of-range without dispatch', () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('{}', { status: 200 }),
    );
    updateSnapshot(envelopeWith(makeCacheReport()));
    render(<CacheReportModal />);
    fireEvent.click(
      screen.getByRole('button', { name: /cache report settings/i }),
    );
    const input = screen.getByLabelText(/anomaly threshold/i);
    fireEvent.change(input, { target: { value: '0' } });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));
    expect(
      screen.getByText(/must be an integer between 1 and 100/i),
    ).toBeInTheDocument();
    // No POST went out.
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });

  // Bound coverage: the server backstops via _validate_cache_report_settings,
  // but a regression that drops the lower or upper bound on the client (or
  // accepts an empty / negative / fractional value) should still fire the
  // inline-error path so the user sees the message without a server
  // round-trip. The fractional cases were added per round-3 Codex review:
  // `parseInt('1.5', 10)` returns 1, so values like '1.5' / '15.9' / '1.0'
  // were silently truncated and POSTed as a different threshold than the
  // user typed. The current guard uses a strict /^-?\d+$/ regex against
  // the trimmed input before parseInt, rejecting any non-integer literal.
  it.each([
    { name: 'upper bound (>100)', value: '101' },
    { name: 'empty string', value: '' },
    { name: 'negative integer', value: '-1' },
    { name: 'fractional (1.5)', value: '1.5' },
    { name: 'fractional (15.9)', value: '15.9' },
    { name: 'fractional (1.0)', value: '1.0' },
    { name: 'scientific notation (15e2)', value: '15e2' },
  ])('client-side guard rejects $name', ({ value }) => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('{}', { status: 200 }),
    );
    updateSnapshot(envelopeWith(makeCacheReport()));
    render(<CacheReportModal />);
    fireEvent.click(
      screen.getByRole('button', { name: /cache report settings/i }),
    );
    const input = screen.getByLabelText(/anomaly threshold/i);
    fireEvent.change(input, { target: { value } });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));
    expect(
      screen.getByText(/must be an integer between 1 and 100/i),
    ).toBeInTheDocument();
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });
});

describe('<CacheReportModal /> empty + loading states', () => {
  it('renders empty state when is_empty', () => {
    updateSnapshot(envelopeWith(makeCacheReport({
      is_empty: true,
      days: [],
    })));
    render(<CacheReportModal />);
    expect(
      screen.getByText(/no claude activity in the last 14 days/i),
    ).toBeInTheDocument();
  });

  it('renders loading placeholder when snapshot is null', () => {
    // No updateSnapshot — store.snapshot stays null.
    render(<CacheReportModal />);
    expect(screen.getByText(/^loading…$/i)).toBeInTheDocument();
  });
});

// ---- Modal-level integration test (per feedback_modal_level_integration_test.md) ----

describe('<CacheReportModal /> integration (panel click -> modal open)', () => {
  it('panel click opens the cache-report modal with the spotlight section', async () => {
    updateSnapshot(envelopeWith(makeCacheReport()));
    render(<App />);
    // Locate the Cache Report panel via its role/name. The panel
    // exists at position 5 of DEFAULT_PANEL_ORDER (the medium row).
    const panel = screen.getByRole('region', { name: /cache report/i });
    fireEvent.click(panel);
    // Modal should now be in the DOM.
    await waitFor(() => {
      expect(
        screen.getByRole('dialog', { name: /cache report/i }),
      ).toBeInTheDocument();
    });
    // Spotlight heading is the first sub-section.
    expect(
      screen.getByText(/today's spotlight/i),
    ).toBeInTheDocument();
  });

  it('SSE re-tick re-renders the modal in place without flicker', () => {
    updateSnapshot(envelopeWith(makeCacheReport()));
    render(<CacheReportModal />);
    // Capture the modal-card DOM node identity pre-tick.
    const modalCardBefore = document.querySelector('.modal-card');
    expect(modalCardBefore).toBeTruthy();
    // New snapshot with a higher cache_hit_percent for today.
    act(() => {
      const cr = makeCacheReport({
        today: {
          date: '2026-05-20',
          cache_hit_percent: 75,
          baseline_median_percent: 67,
          delta_pp: 8,
          net_usd: 2.10,
          saved_usd: 2.20,
          wasted_usd: 0.10,
          anomaly_triggered: false,
          anomaly_reasons: [],
          baseline_daily_row_count: 13,
        },
      });
      const env = envelopeWith(cr);
      env.generated_at = '2026-05-20T11:00:00Z'; // later than initial
      updateSnapshot(env);
    });
    // The modal card node should still be the same DOM element (React
    // re-renders in place; no unmount).
    const modalCardAfter = document.querySelector('.modal-card');
    expect(modalCardAfter).toBe(modalCardBefore);
    // And the new spotlight reflects today=75%.
    expect(screen.getByText(/75%/)).toBeInTheDocument();
  });

  it('CLOSE_MODAL closes the modal without resetting the snapshot', () => {
    updateSnapshot(envelopeWith(makeCacheReport()));
    render(<App />);
    act(() => {
      dispatch({ type: 'OPEN_MODAL', kind: 'cache-report' });
    });
    expect(
      screen.getByRole('dialog', { name: /cache report/i }),
    ).toBeInTheDocument();
    act(() => {
      dispatch({ type: 'CLOSE_MODAL' });
    });
    expect(
      screen.queryByRole('dialog', { name: /cache report/i }),
    ).toBeNull();
    // Snapshot still present (panel still mounted).
    expect(getState().snapshot?.cache_report).toBeTruthy();
  });
});
