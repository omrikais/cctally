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
      enabled: true, weekly_thresholds: [], five_hour_thresholds: [],
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
    // today.date = 2026-05-20; today row should include that text
    expect(curRow?.textContent).toContain('2026-05-20');
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
  // accepts an empty / negative value) should still fire the inline-error
  // path so the user sees the message without a server round-trip.
  // NOTE: fractional values like '15.5' are NOT in this matrix — the client
  // uses parseInt(value, 10) which silently truncates to 15 (a valid
  // in-range integer) and POSTs successfully. The server is the only place
  // that rejects fractional input; if you want client-side fractional
  // rejection you have to swap parseInt for a strict /^-?\d+$/ guard.
  it.each([
    { name: 'upper bound (>100)', value: '101' },
    { name: 'empty string', value: '' },
    { name: 'negative integer', value: '-1' },
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
    // exists at the tail of DEFAULT_PANEL_ORDER (position 11).
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
