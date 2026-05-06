import { fmt } from '../lib/fmt';
import type { DailyPanelRow } from '../types/envelope';

interface Props {
  /** Newest-first as the envelope ships them. The component reverses internally
   *  to render oldest-left → newest-right (natural reading direction). */
  rows: DailyPanelRow[];
  /** Date string (YYYY-MM-DD) of the currently selected day, or null. */
  selectedDate: string | null;
  /** Fired on bar click. NOT fired for zero-cost bars (which are disabled). */
  onSelect: (date: string) => void;
}

/**
 * 30-bar SVG-free mini chart at the top of the Daily modal.
 *
 * Doubles as the navigation control — clicking a bar / pressing ↑↓
 * outside this component re-selects which day the modal is bound to.
 *
 * Today's bar gets a dashed amber outline so it stays visually
 * identifiable when a different day is selected (Q5 of the design).
 *
 * Bar heights are linear in cost relative to max(cost). Zero-cost
 * bars get a faint flat track and are non-clickable (cursor:default,
 * disabled button). Per the design (Q4), this avoids users
 * accidentally selecting a day with no data and seeing an empty
 * detail card.
 */
export function DailyMiniBars({ rows, selectedDate, onSelect }: Props) {
  // Render order: oldest-left → newest-right. Envelope is newest-first,
  // so we copy + reverse rather than mutating the prop.
  const ordered = [...rows].reverse();
  const maxCost = ordered.reduce((m, r) => Math.max(m, r.cost_usd), 0);

  const firstLabel = ordered.length > 0 ? formatAxis(ordered[0].date) : '';
  const lastLabel = ordered.length > 0 ? formatAxis(ordered[ordered.length - 1].date) : '';
  const todayBar = ordered.find((r) => r.is_today);
  const todayLabel = todayBar ? `${formatAxis(todayBar.date)} · today` : '';

  return (
    <section className="daily-modal-bars" aria-label="30-day cost">
      <div className="daily-modal-bars-head">
        <span>30-day cost · selected: {selectedDate ? formatAxis(selectedDate) : '—'}</span>
        <span className="hint">↑↓ navigate · click any bar</span>
      </div>
      <div className="daily-modal-bars-grid" role="img" aria-label="30-day cost histogram">
        {ordered.map((r) => {
          const isZero = r.cost_usd <= 0;
          const isSelected = r.date === selectedDate;
          // 4% floor so zero bars are still visible as a track.
          const heightPct = isZero
            ? 4
            : maxCost > 0 ? (r.cost_usd / maxCost) * 100 : 0;
          const cls = [
            'bar',
            isZero ? 'zero' : '',
            isSelected ? 'sel' : '',
            r.is_today ? 'today' : '',
          ].filter(Boolean).join(' ');
          return (
            <button
              key={r.date}
              data-date={r.date}
              className={cls}
              style={{ height: `${heightPct}%` }}
              disabled={isZero}
              aria-pressed={isSelected}
              aria-label={`${r.label} cost ${r.cost_usd > 0 ? fmt.usd2(r.cost_usd) : 'no usage'}`}
              title={r.cost_usd > 0 ? `${r.label} · ${fmt.usd2(r.cost_usd)}` : `${r.label} · —`}
              onClick={() => { if (!isZero) onSelect(r.date); }}
            />
          );
        })}
      </div>
      {ordered.length > 0 && (
        <div className="daily-modal-bars-axis">
          <span>{firstLabel}</span>
          {todayLabel && <span>{todayLabel}</span>}
          <span>{lastLabel}</span>
        </div>
      )}
    </section>
  );
}

function formatAxis(iso: string): string {
  // "2026-04-26" → "Apr 26" — matches the panel's idiom.
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const parts = iso.split('-').map(Number);
  return `${months[parts[1] - 1]} ${parts[2]}`;
}
