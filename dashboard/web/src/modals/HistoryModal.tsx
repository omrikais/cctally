import { useRef, useState } from 'react';
import { Modal } from './Modal';
import { PeriodMiniBars, type PeriodNavRow } from './PeriodMiniBars';
import { PeriodDetailCard } from './PeriodDetailCard';
import { PeriodTable } from './PeriodTable';
import { KeyHintFooter } from '../components/KeyHintFooter';
import { ShareIcon } from '../components/ShareIcon';
import { useSnapshot } from '../hooks/useSnapshot';
import { useKeymap } from '../hooks/useKeymap';
import { keyOf, stepPeriod, type PeriodVariant } from './periodNav';
import { dailyToPeriodRow } from './historyData';
import { dispatch, getState, type HistoryPeriod } from '../store/store';
import { openShareModal } from '../store/shareSlice';
import type { Envelope, PeriodRow } from '../types/envelope';
import type { SharePanelId } from '../share/types';

/**
 * History modal (S8, issue #254) — collapses the former Daily / Weekly /
 * Monthly detail modals into one modal with a Day·Week·Month toggle, a
 * shared mini-bar navigator (PeriodMiniBars), the Projects-drill visual
 * language (ModelCostBars via PeriodDetailCard), and a sortable
 * Weekly/Monthly table (PeriodTable). Client-and-CSS only; every dataset
 * already ships in the SSE snapshot.
 *
 * Selection is a stable key (keyOf): day → date, week → week_start_at,
 * month → label. `period` is LOCAL state seeded once on open — a heatmap-
 * cell deep-link (openDailyDate) forces Day; otherwise the persisted
 * prefs.historyPeriod. Changing the toggle also persists via
 * SET_HISTORY_PERIOD. A single indigo accent is used across all toggles
 * (spec: one accent, not three).
 */

const PERIOD_ORDER: HistoryPeriod[] = ['day', 'week', 'month'];

const DETAIL_VARIANT: Record<HistoryPeriod, 'daily' | 'weekly' | 'monthly'> = {
  day: 'daily', week: 'weekly', month: 'monthly',
};
// F · Share mapping: the active period → the existing SharePanelId
// (day→daily, week→weekly, month→monthly). SharePanelId + the share
// backend are unchanged; only this dynamic mapping is new.
const SHARE_PANEL: Record<HistoryPeriod, SharePanelId> = {
  day: 'daily', week: 'weekly', month: 'monthly',
};
const UNIT_PLURAL: Record<HistoryPeriod, string> = {
  day: 'days', week: 'weeks', month: 'months',
};

interface Keyed { key: string; nav: PeriodNavRow; period: PeriodRow; }

/** Build keyed rows (envelope order, newest-first) for the active period. */
function buildKeyed(period: HistoryPeriod, env: Envelope | null): Keyed[] {
  if (period === 'day') {
    const rows = env?.daily?.rows ?? [];
    return rows.map((r, i) => ({
      key: r.date,
      nav: { key: r.date, label: r.label, cost: r.cost_usd, isCurrent: r.is_today, isEmpty: r.cost_usd <= 0 },
      period: dailyToPeriodRow(r, rows[i + 1]),
    }));
  }
  const rows = period === 'week' ? (env?.weekly?.rows ?? []) : (env?.monthly?.rows ?? []);
  const variant: PeriodVariant = period === 'week' ? 'week' : 'month';
  return rows.map((r) => ({
    key: keyOf(r, variant),
    nav: { key: keyOf(r, variant), label: r.label, cost: r.cost_usd, isCurrent: r.is_current, isEmpty: r.cost_usd <= 0 },
    period: r,
  }));
}

export function HistoryModal() {
  const env = useSnapshot();

  // Period seed (once, on mount) — deep-link forces Day, else prefs.
  const [period, setPeriodState] = useState<HistoryPeriod>(() =>
    getState().openDailyDate != null ? 'day' : getState().prefs.historyPeriod,
  );
  const setPeriod = (p: HistoryPeriod): void => {
    setPeriodState(p);
    dispatch({ type: 'SET_HISTORY_PERIOD', period: p });
  };

  // #261 — Day·Week·Month is a WAI-ARIA radiogroup with roving tabindex. One
  // ref per radio (PERIOD_ORDER index) so ←/→ can pull DOM focus to the newly-
  // checked radio: focus() works before the re-render because tabIndex only
  // gates Tab navigation, not programmatic focus.
  const radioRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const movePeriod = (dir: 1 | -1): void => {
    const idx = PERIOD_ORDER.indexOf(period);
    const nextIdx = (idx + dir + PERIOD_ORDER.length) % PERIOD_ORDER.length;
    setPeriod(PERIOD_ORDER[nextIdx]);
    radioRefs.current[nextIdx]?.focus();
  };

  // Selection key — seeded from a deep-link date (Day) else null.
  const [selectedKey, setSelectedKey] = useState<string | null>(
    () => getState().openDailyDate,
  );

  const unit: PeriodVariant = period;
  const keyed = buildKeyed(period, env);
  const navRows = keyed.map((k) => k.nav);
  const periodRows = keyed.map((k) => k.period);

  // effectiveKey clamps to the first (current) row when the selected key
  // drops out of the active period's window (e.g. after a toggle switch).
  const keySet = new Set(keyed.map((k) => k.key));
  const firstKey = keyed[0]?.key ?? null;
  const effectiveKey =
    selectedKey != null && keySet.has(selectedKey) ? selectedKey : firstKey;
  const selectedRow = keyed.find((k) => k.key === effectiveKey)?.period ?? null;

  // ↑/↓ steps the NAVIGATOR's chronological order for ALL periods — the
  // same order PeriodMiniBars' ‹/› buttons and the bar layout use — so the
  // two steppers and the bars always agree, and ↑/↓ behaves identically
  // whether or not a table sort is active. (Day has only the navigator. A
  // week/month table sort changes only the table's row DISPLAY order, which
  // stays selectable via its SH-3 focusable rows / click. Binding ↑/↓ to the
  // sorted table instead would desync it from the always-chronological
  // navigator under an active sort — Milestone-B review P2.)
  const orderedKeys = navRows.map((n) => ({ key: n.key }));

  // Keymap: one registration, gated on the topmost layer (openModal ===
  // 'history' AND no share/composer overlay above it — Codex finding 6).
  // Re-registered each render so closures capture the latest period /
  // effectiveKey / orderedKeys (the accepted useKeymap re-registration cost).
  const isHistoryTopmost = (): boolean =>
    getState().openModal === 'history' &&
    getState().shareModal === null &&
    getState().composerModal === null;
  useKeymap([
    {
      key: 'ArrowDown', scope: 'modal', when: isHistoryTopmost,
      action: () => {
        const target = stepPeriod(orderedKeys, effectiveKey, 'older');
        if (target) setSelectedKey(target);
      },
    },
    {
      key: 'ArrowUp', scope: 'modal', when: isHistoryTopmost,
      action: () => {
        const target = stepPeriod(orderedKeys, effectiveKey, 'newer');
        if (target) setSelectedKey(target);
      },
    },
    // ←/→ are NOT bound here anymore (#261): the Day·Week·Month radiogroup
    // owns them as native roving-tabindex (see the toggle's onKeyDown below),
    // so they act only when a toggle radio is focused. ↑/↓ stay global so the
    // navigator steps from anywhere in the modal (the modal's second axis).
  ]);

  const sharePanel = SHARE_PANEL[period];
  const headerExtras = (
    <ShareIcon
      panel={sharePanel}
      panelLabel="History"
      triggerId="history-modal"
      onClick={() => dispatch(openShareModal(sharePanel, 'history-modal'))}
    />
  );

  const title =
    navRows.length > 0
      ? `History · last ${navRows.length} ${UNIT_PLURAL[period]}`
      : 'History';

  return (
    <Modal title={title} accentClass="accent-indigo" headerExtras={headerExtras}>
      {/* Day·Week·Month — WAI-ARIA radiogroup with roving tabindex (#261):
          only the checked radio is a tab stop, so Tab enters the group at the
          active period; ←/→ move focus AND selection within the group (with
          wraparound). ↑/↓ are deliberately NOT handled here — the modal
          reserves them for the period NAVIGATOR (its second axis, per the
          footer), so they bubble to the global keymap even while a radio is
          focused. This is an intentional divergence from strict APG, which
          would use all four arrows within the group. */}
      <div
        className="history-toggle"
        role="radiogroup"
        aria-label="History period"
        onKeyDown={(e) => {
          if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
            e.preventDefault();
            e.stopPropagation();
            movePeriod(e.key === 'ArrowRight' ? 1 : -1);
          }
        }}
      >
        {PERIOD_ORDER.map((p, i) => (
          <button
            key={p}
            ref={(el) => { radioRefs.current[i] = el; }}
            type="button"
            role="radio"
            aria-checked={period === p}
            tabIndex={period === p ? 0 : -1}
            className={`pill ${period === p ? 'on' : ''}`}
            onClick={() => setPeriod(p)}
          >
            {p === 'day' ? 'Day' : p === 'week' ? 'Week' : 'Month'}
          </button>
        ))}
      </div>
      {navRows.length === 0 ? (
        <div className="panel-empty">No usage history yet.</div>
      ) : (
        <>
          <PeriodMiniBars
            unit={unit}
            rows={navRows}
            selectedKey={effectiveKey}
            onSelect={setSelectedKey}
          />
          {selectedRow && (
            <PeriodDetailCard
              row={selectedRow}
              variant={DETAIL_VARIANT[period]}
              accentClass="accent-indigo"
            />
          )}
          {period !== 'day' && (
            <PeriodTable
              rows={periodRows}
              variant={period === 'week' ? 'weekly' : 'monthly'}
              accentClass="accent-indigo"
              selectedKey={effectiveKey}
              onSelect={setSelectedKey}
            />
          )}
        </>
      )}
      <KeyHintFooter
        hints={[
          { keys: <kbd>↑↓</kbd>, label: 'period' },
          { keys: <kbd>←→</kbd>, label: 'Day/Week/Month' },
          { keys: <kbd>Esc</kbd>, label: 'close' },
        ]}
      />
    </Modal>
  );
}
