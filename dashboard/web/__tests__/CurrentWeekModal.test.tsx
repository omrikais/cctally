import { describe, it, expect, beforeEach } from 'vitest';
import { render } from '@testing-library/react';
import { CurrentWeekModal } from '../src/modals/CurrentWeekModal';
import { updateSnapshot, _resetForTests } from '../src/store/store';
import fixture from './fixtures/envelope.json';
import type { Envelope } from '../src/types/envelope';

describe('<CurrentWeekModal />', () => {
  beforeEach(() => {
    _resetForTests();
    updateSnapshot(fixture as unknown as Envelope);
  });

  it('renders the week-pill chip strip with formatted week window', () => {
    render(<CurrentWeekModal />);
    const pill = document.getElementById('mcw-week-pill');
    expect(pill).not.toBeNull();
    expect(pill?.classList.contains('m-pill')).toBe(true);
    expect(pill?.classList.contains('accent-green')).toBe(true);
    // Fixture: week_label "Apr 21–28", reset "2026-04-28T00:00:00Z" → "Apr 28"
    // Post-F1: literal " UTC" is gone — the offset suffix is rendered by
    // datetime formatters (e.g. fmt.datetimeShortZ on the reset cell);
    // the week-window pill is a pure date range with no clock time.
    expect(pill?.textContent).toMatch(/Apr 21–28 → Apr 28/);
  });

  it('splits the big numeral into .int + .unit', () => {
    render(<CurrentWeekModal />);
    const wrap = document.getElementById('mcw-bignum');
    expect(wrap).not.toBeNull();
    const intEl = wrap?.querySelector('.int');
    const unit = wrap?.querySelector('.unit');
    // Fixture used_pct=17.4 → int "17", unit ".4%"
    expect(intEl?.textContent).toBe('17');
    expect(unit?.textContent).toBe('.4%');
  });

  it('renders the progress-bar fill + marker + scale ticks', () => {
    render(<CurrentWeekModal />);
    expect(document.querySelector('.mcw-pbar')).not.toBeNull();
    const fill = document.getElementById('mcw-fill');
    expect(fill?.style.width).toBe('17.4%');
    const marker = document.getElementById('mcw-marker');
    expect(marker?.style.left).toBe('17.4%');
    const scale = document.querySelector('.mcw-pscale');
    expect(scale?.children.length).toBe(5);
  });

  it('renders the three mcw-mini stats (spent, $ / 1%, reset)', () => {
    render(<CurrentWeekModal />);
    const mini = document.getElementById('mcw-mini');
    expect(mini?.querySelectorAll('.s').length).toBe(3);
    expect(document.getElementById('mcw-spent')?.classList.contains('v-magenta')).toBe(true);
    expect(document.getElementById('mcw-dpp')?.classList.contains('v-cyan')).toBe(true);
    // Values
    expect(document.getElementById('mcw-spent')?.textContent).toBe('$20.95');
    expect(document.getElementById('mcw-dpp')?.textContent).toBe('$1.230');
  });

  it('renders the Milestones section header with hash icon + count pill', () => {
    render(<CurrentWeekModal />);
    const sec = document.querySelector('.m-sec.sec-ms');
    expect(sec).not.toBeNull();
    const useEl = sec?.querySelector('svg use');
    expect(useEl?.getAttribute('href')).toBe('/static/icons.svg#hash');
    const cnt = document.getElementById('mcw-ms-count');
    expect(cnt?.classList.contains('m-pill')).toBe(true);
    expect(cnt?.classList.contains('accent-purple')).toBe(true);
    const snap = fixture as unknown as Envelope;
    const expected = (snap.current_week?.milestones?.length ?? 0) + ' crossed';
    expect(cnt?.textContent).toBe(expected);
  });

  it('renders one m-histable row per milestone with pct-cell pill', () => {
    render(<CurrentWeekModal />);
    const snap = fixture as unknown as Envelope;
    const expected = snap.current_week?.milestones?.length ?? 0;
    const rows = document.querySelectorAll('#mcw-rows tr');
    expect(rows.length).toBe(expected);
    const pctCells = document.querySelectorAll('#mcw-rows .pct-cell');
    expect(pctCells.length).toBe(expected);
    pctCells.forEach((cell) => {
      expect(cell.classList.contains('m-pill')).toBe(true);
      expect(cell.classList.contains('accent-purple')).toBe(true);
    });
  });

  it('renders the empty state when no milestones', () => {
    _resetForTests();
    updateSnapshot({
      ...(fixture as unknown as Envelope),
      current_week: {
        ...(fixture as unknown as Envelope).current_week!,
        milestones: [],
      },
    });
    render(<CurrentWeekModal />);
    const empty = document.getElementById('mcw-empty');
    expect(empty).not.toBeNull();
    expect(empty?.textContent).toMatch(/No milestones yet/);
    expect(document.getElementById('mcw-table')).toBeNull();
  });

  it('renders a sub text with avg marginal + latest at when ≥2 milestones', () => {
    render(<CurrentWeekModal />);
    const sub = document.getElementById('mcw-ms-sub');
    expect(sub).not.toBeNull();
    expect(sub?.textContent).toMatch(/avg marginal/);
    expect(sub?.textContent).toMatch(/latest at/);
  });

  // ---- 5h milestone section (Round-3 Item 4a / spec §5.3) ----------

  // Build an envelope with stacked pre-credit + post-credit 5h
  // milestones plus one credit event in the same block. Used by the
  // tests below; leaves everything else identical to the canonical
  // fixture.
  function withFiveHourCreditScenario(): Envelope {
    const base = JSON.parse(JSON.stringify(fixture)) as Envelope;
    const cw = base.current_week;
    if (cw) {
      // Pre-credit milestones (reset_event_id=0) at threshold 10 and 28.
      // Post-credit milestone (reset_event_id=42) at threshold 10 again
      // — the row key MUST disambiguate via reset_event_id or React
      // will warn "encountered two children with the same key".
      cw.five_hour_milestones = [
        {
          percent_threshold: 10,
          captured_at_utc: '2026-04-24T11:00:00+00:00',
          block_cost_usd: 1.5,
          marginal_cost_usd: null,
          seven_day_pct_at_crossing: 7.0,
          reset_event_id: 0,
        },
        {
          percent_threshold: 28,
          captured_at_utc: '2026-04-24T12:30:00+00:00',
          block_cost_usd: 5.5,
          marginal_cost_usd: 4.0,
          seven_day_pct_at_crossing: 9.0,
          reset_event_id: 0,
        },
        {
          percent_threshold: 10,
          captured_at_utc: '2026-04-24T14:10:00+00:00',
          block_cost_usd: 1.8,
          marginal_cost_usd: null,
          seven_day_pct_at_crossing: 10.5,
          reset_event_id: 42,
        },
      ];
      // Canonical fixture lacks five_hour_block; build a minimal one
      // with one credit event so the modal's 5h section renders.
      cw.five_hour_block = {
        block_start_at: '2026-04-24T10:00:00+00:00',
        five_hour_window_key: 1745493600,
        seven_day_pct_at_block_start: 14.0,
        seven_day_pct_delta_pp: 3.4,
        crossed_seven_day_reset: false,
        credits: [
          {
            effective_reset_at_utc: '2026-04-24T13:00:00+00:00',
            prior_percent: 28.0,
            post_percent: 8.0,
            delta_pp: -20,
          },
        ],
      };
    }
    return base;
  }

  describe('5h milestone section (Round-3)', () => {
    it('does not render the 5h section when both streams are empty', () => {
      // Canonical fixture has no five_hour_milestones / credits → no
      // 5h section (spec §5.3: suppressed for pre-v1.7.x users).
      render(<CurrentWeekModal />);
      expect(document.getElementById('mcw-5h-table')).toBeNull();
    });

    it('renders the 5h section header + count pill when milestones present', () => {
      _resetForTests();
      updateSnapshot(withFiveHourCreditScenario());
      render(<CurrentWeekModal />);
      const sec = document.querySelector('.m-sec.sec-5h');
      expect(sec).not.toBeNull();
      expect(sec?.textContent).toMatch(/5h milestones/);
      const cnt = document.getElementById('mcw-5h-count');
      expect(cnt).not.toBeNull();
      // 3 5h milestones in the fixture.
      expect(cnt?.textContent).toBe('3 crossed');
    });

    it('renders a credit divider row between pre and post-credit milestones', () => {
      _resetForTests();
      updateSnapshot(withFiveHourCreditScenario());
      render(<CurrentWeekModal />);
      const dividers = document.querySelectorAll('.mcw-5h-credit-row');
      expect(dividers.length).toBe(1);
      const cell = document.querySelector('.mcw-5h-credit-cell');
      expect(cell).not.toBeNull();
      expect(cell?.textContent).toMatch(/⚡/);
      expect(cell?.textContent).toMatch(/CREDIT/);
      expect(cell?.textContent).toMatch(/-20pp/);
      // colSpan covers all 5 columns (% / When / Block $ / Marginal $ / 7d %).
      expect(cell?.getAttribute('colspan')).toBe('5');
    });

    it('keys milestone rows by reset_event_id so post-credit repeats render as distinct rows', () => {
      _resetForTests();
      updateSnapshot(withFiveHourCreditScenario());
      render(<CurrentWeekModal />);
      // 3 milestone rows + 1 credit divider = 4 total <tr> in the 5h table.
      const allRows = document.querySelectorAll('#mcw-5h-table tbody tr');
      expect(allRows.length).toBe(4);
      // Both threshold-10 rows render (would collide on React key
      // without reset_event_id in the key).
      const thresholdCells = document.querySelectorAll('#mcw-5h-table tbody .pct-cell');
      const thresholds = Array.from(thresholdCells).map((c) => c.textContent);
      const tenCount = thresholds.filter((t) => t === '10').length;
      expect(tenCount).toBe(2);
    });
  });
});
