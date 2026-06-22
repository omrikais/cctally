import { fmt } from '../lib/fmt';
import type { ComparisonMetrics as Metrics } from './comparisonMetricsCalc';

// #217 S7 F10 — the A-vs-B metrics-delta strip. Six cells (Cost, Tokens,
// Prompts, Errors, Duration, Files), each "A → B" with a delta. The subtle
// "improved" arrow (▼ + the green `conv-cmp-metric-down` class) lands ONLY on
// the unambiguously lower-is-better metrics (cost / errors / duration) when
// B < A; tokens / prompts / files show a plain signed delta with NO value-
// judgment color (more tokens / files is neither good nor bad on its own).
//
// All human-readable numbers route through lib/fmt so the strip matches the
// rest of the dashboard's typographic standard (currency, k/M token humanizer,
// "Xh YYm" duration, U+2212 signed minus).

interface Row {
  key: 'cost' | 'tokens' | 'prompts' | 'errors' | 'duration' | 'files';
  label: string;
  a: number | null;
  b: number | null;
  // Lower-is-better → a B<A delta is an improvement (the only metrics that earn
  // the green ▼). Neutral metrics never color.
  lowerBetter: boolean;
  fmtVal: (v: number | null) => string;
  fmtDelta: (delta: number) => string;
}

const intDelta = (d: number): string => (d === 0 ? '±0' : d > 0 ? `+${d}` : `−${Math.abs(d)}`);
const tokensDelta = (d: number): string =>
  d === 0 ? '±0' : `${d > 0 ? '+' : '−'}${fmt.tokens(Math.abs(d))}`;
const durationDelta = (d: number): string =>
  d === 0 ? '±0' : `${d > 0 ? '+' : '−'}${fmt.hhmm(Math.abs(d))}`;

export function ComparisonMetrics({ a, b }: { a: Metrics; b: Metrics }) {
  const rows: Row[] = [
    { key: 'cost', label: 'Cost', a: a.cost, b: b.cost, lowerBetter: true,
      fmtVal: fmt.usd2, fmtDelta: fmt.usdSigned },
    { key: 'tokens', label: 'Tokens', a: a.tokens, b: b.tokens, lowerBetter: false,
      fmtVal: fmt.tokens, fmtDelta: tokensDelta },
    { key: 'prompts', label: 'Prompts', a: a.prompts, b: b.prompts, lowerBetter: false,
      fmtVal: (v) => (v == null ? '—' : String(v)), fmtDelta: intDelta },
    { key: 'errors', label: 'Errors', a: a.errors, b: b.errors, lowerBetter: true,
      fmtVal: (v) => (v == null ? '—' : String(v)), fmtDelta: intDelta },
    { key: 'duration', label: 'Duration', a: a.durationSeconds, b: b.durationSeconds, lowerBetter: true,
      fmtVal: fmt.hhmm, fmtDelta: durationDelta },
    { key: 'files', label: 'Files', a: a.files, b: b.files, lowerBetter: false,
      fmtVal: (v) => (v == null ? '—' : String(v)), fmtDelta: intDelta },
  ];

  return (
    <div className="conv-cmp-metrics" role="group" aria-label="Comparison metrics">
      {rows.map((r) => {
        const haveBoth = r.a != null && r.b != null;
        const delta = haveBoth ? (r.b as number) - (r.a as number) : null;
        const improved = r.lowerBetter && delta != null && delta < 0;
        const cls = ['conv-cmp-metric', improved ? 'conv-cmp-metric-down' : '']
          .filter(Boolean)
          .join(' ');
        return (
          <div key={r.key} className={cls} data-metric={r.key}>
            <div className="conv-cmp-metric-label">{r.label}</div>
            <div className="conv-cmp-metric-vals">
              <span className="conv-cmp-metric-a">{r.fmtVal(r.a)}</span>
              <span className="conv-cmp-metric-arrow" aria-hidden="true"> → </span>
              <span className="conv-cmp-metric-b">{r.fmtVal(r.b)}</span>
            </div>
            {delta != null && delta !== 0 && (
              <div className="conv-cmp-metric-delta">
                {improved && <span className="conv-cmp-metric-dir" aria-hidden="true">▼ </span>}
                {r.fmtDelta(delta)}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
