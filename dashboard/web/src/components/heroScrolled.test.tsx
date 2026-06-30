// #248 Task 7 — mobile sticky-collapse: the `heroScrolled` store flag, the
// HeroStrip mobile-only IntersectionObserver that drives it, and the Header's
// condensed Used%/reset readout gated on `view==='dashboard' && heroScrolled`.
import { act, render } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { Header } from './Header';
import { HeroStrip } from './HeroStrip';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import { IntersectionObserverStub, installIntersectionObserverStub } from '../test-utils/intersectionObserver';
import { stubMobileMedia } from '../test-utils/mobileMedia';
import type { Envelope } from '../types/envelope';

function env(usedPct = 11, resetSec = 200000): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-06-30T10:00:00Z',
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: {
      week_label: 'wk Jun 30', used_pct: usedPct, five_hour_pct: 8,
      dollar_per_pct: 23.4, forecast_pct: 31, forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: {
      used_pct: usedPct, five_hour_pct: 8, five_hour_resets_in_sec: null,
      spent_usd: 14.2, dollar_per_pct: 23.4, reset_at_utc: '2026-07-03T00:00:00Z',
      reset_in_sec: resetSec, last_snapshot_age_sec: 30, milestones: [],
      freshness: null, five_hour_block: null,
    },
    forecast: null, trend: null,
    weekly: { rows: [] }, monthly: { rows: [] }, blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [] },
  };
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  updateSnapshot(env());
});

describe('#248 Task 7 — SET_HERO_SCROLLED store flag', () => {
  it('starts false and toggles via SET_HERO_SCROLLED', () => {
    expect(getState().heroScrolled).toBe(false);
    act(() => { dispatch({ type: 'SET_HERO_SCROLLED', scrolled: true }); });
    expect(getState().heroScrolled).toBe(true);
    act(() => { dispatch({ type: 'SET_HERO_SCROLLED', scrolled: false }); });
    expect(getState().heroScrolled).toBe(false);
  });

  it('is NOT persisted to localStorage', () => {
    act(() => { dispatch({ type: 'SET_HERO_SCROLLED', scrolled: true }); });
    // The prefs blob never carries heroScrolled (it lives in transient UIState).
    const raw = localStorage.getItem('ccusage.dashboard.prefs') ?? '';
    expect(raw).not.toContain('heroScrolled');
  });
});

describe('#248 Task 7 — Header condensed readout gating', () => {
  it('hidden by default (heroScrolled=false), and no is-scrolled class', () => {
    const { queryByTestId, container } = render(<Header />);
    expect(queryByTestId('topbar-condensed')).toBeNull();
    // No collapse hook at the top → the mobile @media rule keeps the bar wrapped.
    expect(container.querySelector('header.topbar.is-scrolled')).toBeNull();
  });

  it('shows the condensed Used%/reset + adds is-scrolled only when view===dashboard && heroScrolled', () => {
    const { queryByTestId, container } = render(<Header />);
    act(() => { dispatch({ type: 'SET_HERO_SCROLLED', scrolled: true }); });
    const node = queryByTestId('topbar-condensed');
    expect(node).not.toBeNull();
    expect(node?.textContent).toContain('11.0%');
    expect(node?.textContent).toContain('resets');
    expect(node?.textContent).toContain('2d 7h'); // ddhh(200000)
    // The header gains `is-scrolled` so the mobile @media collapse (hide
    // ViewSwitcher/Doctor/basket → fit the condensed readout valve) can fire.
    expect(container.querySelector('header.topbar.is-scrolled')).not.toBeNull();
  });

  it('hidden in the conversations view even when heroScrolled', () => {
    const { queryByTestId } = render(<Header />);
    act(() => {
      dispatch({ type: 'SET_HERO_SCROLLED', scrolled: true });
      dispatch({ type: 'SET_VIEW', view: 'conversations' });
    });
    expect(queryByTestId('topbar-condensed')).toBeNull();
  });
});

describe('#248 Task 7 — HeroStrip mobile IntersectionObserver', () => {
  beforeEach(() => {
    installIntersectionObserverStub();
  });

  it('on mobile, the observer flips heroScrolled as the hero enters/leaves view', () => {
    stubMobileMedia(true);
    render(<HeroStrip />);
    const io = IntersectionObserverStub.instances.at(-1);
    expect(io, 'expected a HeroStrip IntersectionObserver on mobile').toBeDefined();
    act(() => { io!.emit(false); });            // hero left the viewport
    expect(getState().heroScrolled).toBe(true);
    act(() => { io!.emit(true); });             // hero back in view
    expect(getState().heroScrolled).toBe(false);
  });

  it('does NOT observe on desktop (mobile-gated)', () => {
    stubMobileMedia(false);
    render(<HeroStrip />);
    expect(IntersectionObserverStub.instances.length).toBe(0);
  });

  it('resets heroScrolled to false on unmount (leaving the dashboard view)', () => {
    stubMobileMedia(true);
    const { unmount } = render(<HeroStrip />);
    const io = IntersectionObserverStub.instances.at(-1)!;
    act(() => { io.emit(false); });
    expect(getState().heroScrolled).toBe(true);
    act(() => { unmount(); });
    expect(getState().heroScrolled).toBe(false);
  });

  it('is guarded when IntersectionObserver is undefined (JSDOM/SSR)', () => {
    stubMobileMedia(true);
    const saved = (globalThis as { IntersectionObserver?: unknown }).IntersectionObserver;
    delete (globalThis as { IntersectionObserver?: unknown }).IntersectionObserver;
    try {
      expect(() => render(<HeroStrip />)).not.toThrow();
      expect(getState().heroScrolled).toBe(false);
    } finally {
      (globalThis as { IntersectionObserver?: unknown }).IntersectionObserver = saved;
    }
  });
});
