import { fmt } from '../lib/fmt';
import { stepPeriod } from './periodNav';

// PeriodMiniBars — the shared mini-bar navigator for the History modal
// (S8, issue #254, WM-2). Generalized from the former Daily mini-bar strip
// so Day, Week, and Month all get a bar-strip navigator. Reuses the existing
// `.daily-modal-bars*` / `.bar` CSS so it renders byte-identically to that
// former strip.
//
// Behavior ported from the former Daily mini-bar strip:
//   - rows arrive newest-first (envelope order); reversed internally so
//     the strip reads oldest-left → newest-right.
//   - bar height is linear in cost/max(cost); zero-cost ("empty") bars get
//     a 4% floor track and are non-clickable (disabled).
//   - the current period gets the dashed `today` outline; the selected
//     period gets `sel` + aria-pressed.
//   - ‹/› step buttons and the parent modal's ↑/↓ share stepPeriod.
//
// Axis labels route through lib/fmt (day → fmt.calDate on the date key;
// week/month use the server-formatted `label`), replacing the hand-rolled
// formatAxis.
//
// DA-1 fix: the axis renders first / current / last spans. When the
// current period IS the last (newest, rightmost) bar — the normal case —
// drop the duplicate centre label and suffix the RIGHT label with
// `· today` (day) / `· now` (week/month). When the current period is an
// interior bar, keep the three-span layout with a suffixed centre label.

export interface PeriodNavRow {
  key: string;
  label: string;
  cost: number;
  isCurrent: boolean;
  isEmpty: boolean;
}

interface Props {
  /** Newest-first, as the envelope ships them. Reversed internally. */
  rows: PeriodNavRow[];
  selectedKey: string | null;
  /** Fired on bar click / step. NOT fired for zero-cost (empty) bars. */
  onSelect: (key: string) => void;
  /** Drives axis-label formatting + the "· today"/"· now" suffix copy. */
  unit: 'day' | 'week' | 'month';
}

function axisLabel(row: PeriodNavRow, unit: Props['unit']): string {
  // Day keys are calendar dates (YYYY-MM-DD) — route through lib/fmt for
  // the "Apr 26" form; fall back to the pre-formatted label when the key
  // isn't a bare date. Week/month rows carry a server-formatted label.
  if (unit === 'day') return fmt.calDate(row.key) ?? row.label;
  return row.label;
}

export function PeriodMiniBars({ rows, selectedKey, onSelect, unit }: Props) {
  // Render order: oldest-left → newest-right. Envelope is newest-first, so
  // copy + reverse rather than mutating the prop.
  const ordered = [...rows].reverse();
  const maxCost = ordered.reduce((m, r) => Math.max(m, r.cost), 0);

  const selectedRow = rows.find((r) => r.key === selectedKey) ?? null;
  const selectedLabel = selectedRow ? axisLabel(selectedRow, unit) : null;

  const suffix = unit === 'day' ? '· today' : '· now';
  const firstLabel = ordered.length > 0 ? axisLabel(ordered[0], unit) : '';
  const lastRow = ordered.length > 0 ? ordered[ordered.length - 1] : null;
  const lastLabel = lastRow ? axisLabel(lastRow, unit) : '';
  const currentRow = ordered.find((r) => r.isCurrent) ?? null;
  const currentIsLast =
    currentRow != null && lastRow != null && currentRow.key === lastRow.key;

  const olderKey = stepPeriod(rows, selectedKey, 'older');
  const newerKey = stepPeriod(rows, selectedKey, 'newer');

  return (
    <section className="daily-modal-bars" aria-label={`Cost by ${unit}`}>
      <div className="daily-modal-bars-head">
        <span className="daily-bars-nav">
          <button
            type="button"
            className="daily-step-btn"
            aria-label="Step to older period"
            disabled={olderKey === null}
            onClick={() => { if (olderKey) onSelect(olderKey); }}
          >
            ‹
          </button>
          <span>selected: {selectedLabel ?? '—'}</span>
          <button
            type="button"
            className="daily-step-btn"
            aria-label="Step to newer period"
            disabled={newerKey === null}
            onClick={() => { if (newerKey) onSelect(newerKey); }}
          >
            ›
          </button>
        </span>
        <span className="hint hint-desktop">↑↓ navigate · click any bar</span>
        <span className="hint hint-mobile">‹ › to step · tap a bar</span>
      </div>
      <div className="daily-modal-bars-grid" role="img" aria-label={`Cost by ${unit} histogram`}>
        {ordered.map((r) => {
          const isSelected = r.key === selectedKey;
          // 4% floor so empty bars are still visible as a track.
          const heightPct = r.isEmpty
            ? 4
            : maxCost > 0 ? (r.cost / maxCost) * 100 : 0;
          const cls = [
            'bar',
            r.isEmpty ? 'zero' : '',
            isSelected ? 'sel' : '',
            r.isCurrent ? 'today' : '',
          ].filter(Boolean).join(' ');
          return (
            <button
              key={r.key}
              data-key={r.key}
              className={cls}
              style={{ height: `${heightPct}%` }}
              disabled={r.isEmpty}
              aria-pressed={isSelected}
              aria-label={`${r.label} cost ${r.cost > 0 ? fmt.usd2(r.cost) : 'no usage'}`}
              title={r.cost > 0 ? `${r.label} · ${fmt.usd2(r.cost)}` : `${r.label} · —`}
              onClick={() => { if (!r.isEmpty) onSelect(r.key); }}
            />
          );
        })}
      </div>
      {ordered.length > 0 && (
        <div className="daily-modal-bars-axis">
          <span>{firstLabel}</span>
          {currentRow && !currentIsLast && (
            <span>{axisLabel(currentRow, unit)} {suffix}</span>
          )}
          <span>{currentIsLast ? `${lastLabel} ${suffix}` : lastLabel}</span>
        </div>
      )}
    </section>
  );
}
