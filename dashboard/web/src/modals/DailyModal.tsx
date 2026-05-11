import { useEffect, useState, useSyncExternalStore } from 'react';
import { Modal } from './Modal';
import { DailyMiniBars } from './DailyMiniBars';
import { PeriodDetailCard } from './PeriodDetailCard';
import { ShareIcon } from '../components/ShareIcon';
import { useSnapshot } from '../hooks/useSnapshot';
import { registerKeymap } from '../store/keymap';
import { dispatch, getState, subscribeStore } from '../store/store';
import { openShareModal } from '../store/shareSlice';
import type { DailyPanelRow, PeriodRow } from '../types/envelope';

/**
 * Daily detail modal (snapshot-driven; no per-modal refetch endpoint).
 *
 * Composition: <DailyMiniBars> + <PeriodDetailCard variant='daily'>.
 * The bars at top double as 30-day visual context AND the in-modal
 * navigation control (click any bar / ↑↓ to re-select).
 *
 * Selection storage is date-keyed (`selectedDate: string | null`) so
 * date rollover at midnight does not silently move the user's selection
 * onto a different calendar day. If the selected date is no longer in
 * `daily.rows[]` (extreme edge: stored value is older than the 30-day
 * window), we snap to today silently.
 *
 * Live updates: every SSE tick re-renders the modal. Today's row's
 * cost_usd / tokens / cache_hit_pct may update; non-today rows are
 * static history. Selection survives tick-induced re-renders because
 * it's date-keyed, not index-keyed.
 */
export function DailyModal() {
  const env = useSnapshot();
  const rows: DailyPanelRow[] = env?.daily?.rows ?? [];
  const initialBound = useSyncExternalStore(
    subscribeStore,
    () => getState().openDailyDate,
  );

  // Mount-only: read openDailyDate once into local state. After mount,
  // the modal owns its own selection state — clicks on bars and ↑↓ key
  // bindings call setSelectedDate. Closing + reopening the modal
  // re-mounts this component, so a fresh openDailyDate is picked up.
  const [selectedDate, setSelectedDate] = useState<string | null>(null);
  useEffect(() => {
    if (initialBound) setSelectedDate(initialBound);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []); // mount only — see comment above

  // Effective selection: explicit setSelectedDate wins; else fall back
  // to today (rows[0]). If the selected date is no longer in rows[]
  // (extreme edge: dashboard left open across many days, scrolled off
  // the 30-day window), snap to today silently — no toast/error.
  const knownDates = new Set(rows.map((r) => r.date));
  const fallback = rows[0]?.date ?? null;
  const effectiveDate =
    selectedDate && knownDates.has(selectedDate) ? selectedDate : fallback;

  // Modal-scoped keymap for ↑↓ navigation. Re-register when rows or
  // effectiveDate changes so the index math stays valid (e.g. after a
  // mid-modal SSE tick that prepends a new today row).
  useEffect(() => {
    return registerKeymap([
      {
        key: 'ArrowDown',
        scope: 'modal',
        when: () => getState().openModal === 'daily',
        action: () => {
          const cur = effectiveDate;
          if (!cur) return;
          const idx = rows.findIndex((r) => r.date === cur);
          // rows[] is newest-first → idx+1 is one day OLDER.
          if (idx < 0 || idx >= rows.length - 1) return;
          setSelectedDate(rows[idx + 1].date);
        },
      },
      {
        key: 'ArrowUp',
        scope: 'modal',
        when: () => getState().openModal === 'daily',
        action: () => {
          const cur = effectiveDate;
          if (!cur) return;
          const idx = rows.findIndex((r) => r.date === cur);
          // rows[] is newest-first → idx-1 is one day NEWER.
          if (idx <= 0) return;
          setSelectedDate(rows[idx - 1].date);
        },
      },
    ]);
  }, [rows, effectiveDate]);

  const headerExtras = (
    <ShareIcon
      panel="daily"
      panelLabel="Daily"
      triggerId="daily-modal"
      onClick={() => dispatch(openShareModal('daily', 'daily-modal'))}
    />
  );

  if (rows.length === 0) {
    return (
      <Modal
        title="Daily history · last 30"
        accentClass="accent-indigo"
        headerExtras={headerExtras}
      >
        <div className="panel-empty">No usage history yet.</div>
      </Modal>
    );
  }

  const selectedIdx = rows.findIndex((r) => r.date === effectiveDate);
  const safeIdx = selectedIdx >= 0 ? selectedIdx : 0;
  const selected = rows[safeIdx];
  // rows[] is newest-first, so rows[safeIdx + 1] is the prior (older) day.
  const prior = rows[safeIdx + 1];
  const periodRow = dailyToPeriodRow(selected, prior);

  return (
    <Modal
      title="Daily history · last 30"
      accentClass="accent-indigo"
      headerExtras={headerExtras}
    >
      <DailyMiniBars
        rows={rows}
        selectedDate={effectiveDate}
        onSelect={setSelectedDate}
      />
      <PeriodDetailCard
        row={periodRow}
        variant="daily"
        accentClass="accent-indigo"
      />
    </Modal>
  );
}

/**
 * Adapt a DailyPanelRow into a PeriodRow shape for the detail card.
 * Computes Δ% vs the prior (older) day inline. used_pct / dollar_per_pct
 * stay null (the detail card gates that stats row to the weekly variant).
 */
function dailyToPeriodRow(
  row: DailyPanelRow,
  prior?: DailyPanelRow,
): PeriodRow {
  const delta =
    prior && prior.cost_usd > 0
      ? (row.cost_usd - prior.cost_usd) / prior.cost_usd
      : null;
  return {
    label: row.label,
    cost_usd: row.cost_usd,
    total_tokens: row.total_tokens,
    input_tokens: row.input_tokens,
    output_tokens: row.output_tokens,
    cache_creation_tokens: row.cache_creation_tokens,
    cache_read_tokens: row.cache_read_tokens,
    used_pct: null,
    dollar_per_pct: null,
    delta_cost_pct: delta,
    is_current: row.is_today,
    models: row.models,
    cache_hit_pct: row.cache_hit_pct,
  };
}
