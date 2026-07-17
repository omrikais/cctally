import { render } from '@testing-library/react';
import { beforeEach, describe, it, expect, vi } from 'vitest';
import { CSS } from '@dnd-kit/utilities';

// Force a mismatched-span drag transform (non-identity scale) so we can assert
// PanelHost serializes translate-only — the #293 S2 tall-row distortion fix.
const MOCK_TRANSFORM = { x: 10, y: 0, scaleX: 0.5, scaleY: 2 };
vi.mock('@dnd-kit/sortable', () => ({
  useSortable: () => ({
    listeners: {},
    setNodeRef: () => {},
    transform: MOCK_TRANSFORM,
    transition: undefined,
    isDragging: false,
  }),
}));

import { PanelHost } from './PanelHost';
import { _resetForTests, updateSnapshot } from '../store/store';
import type { Envelope } from '../types/envelope';

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
  _resetForTests();
  updateSnapshot(env());
});

describe('PanelHost strips drag scale (translate-only) — #293 S2', () => {
  it('serializes translate3d with no scaleX/scaleY on mismatched spans', () => {
    const { container } = render(<PanelHost id="sessions" index={0} mode="bento" />);
    const host = container.querySelector('[data-panel-host="sessions"]') as HTMLElement;
    expect(host.style.transform).toBe(CSS.Translate.toString(MOCK_TRANSFORM));
    expect(host.style.transform).not.toMatch(/scale/i);
  });
});
