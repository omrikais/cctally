// #264 S4 (A2) — Blocks UNCAP. This file previously codified the #248 Task 4
// 3-row summary cap (`slice(0,3)`); the S4 desktop-bento repair removes that cap
// so the bento Blocks card renders EVERY block and scrolls in-card (the Recent
// Sessions inner-scroll model), which is how "see all blocks" is reached. This
// test now feeds 8 rows and asserts all 8 render, in envelope (head) order, with
// the store snapshot carrying all 8 — the inverse of the old cap.
//
// (S8 #254 removed the Weekly/Monthly grid tiles — the consolidated History
// modal supersedes them — so their former compaction cases left with the
// components. BlocksPanel is the remaining tiled summary panel.)
import { render } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { BlocksPanel } from './BlocksPanel';
import { _resetForTests, getState, updateSnapshot } from '../store/store';
import type { BlocksPanelRow, Envelope } from '../types/envelope';

function blocksRow(start: string, label: string, cost: number): BlocksPanelRow {
  return {
    start_at: start,
    end_at: start,
    anchor: 'recorded',
    is_active: false,
    cost_usd: cost,
    label,
    models: [{ model: 'm', display: 'opus', chip: 'opus', cost_usd: cost, cost_pct: 100 }],
  };
}

const BLOCKS: BlocksPanelRow[] = Array.from({ length: 8 }, (_, i) =>
  blocksRow(`2026-06-${10 + i}T00:00:00Z`, `B${i}`, (i + 1) * 5),
);

function env(): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-06-30T10:00:00Z',
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: {
      week_label: 'wk Jun 30', used_pct: 11, five_hour_pct: 8,
      dollar_per_pct: 23.4, forecast_pct: 31, forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null, forecast: null, trend: null,
    weekly: { rows: [] },
    monthly: { rows: [] },
    blocks: { rows: BLOCKS, total_cost_usd: 180 },
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

describe('#264 S4 (A2) — Blocks renders every row (no cap)', () => {
  it('BlocksPanel renders all 8 .blocks-row rows from the 8-row envelope', () => {
    const { container } = render(<BlocksPanel />);
    expect(container.querySelectorAll('.blocks-row').length).toBe(8);
    expect(getState().snapshot?.blocks?.rows?.length).toBe(8);
  });

  it('renders every row in envelope (head) order — all reachable via in-card scroll', () => {
    const { container } = render(<BlocksPanel />);
    const labels = Array.from(container.querySelectorAll('.blocks-row .label')).map((n) =>
      (n.textContent ?? '').trim(),
    );
    expect(labels).toEqual(['B0', 'B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7']);
  });
});
