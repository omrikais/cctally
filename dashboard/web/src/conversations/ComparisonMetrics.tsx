import { fmt } from '../lib/fmt';
import { deltaIntent, semanticState, type Polarity } from './semantics';
import type { ComparisonMetrics as Metrics } from './comparisonMetricsCalc';

// #217 S7 F10 / #228 S5 E1 — the A-vs-B metrics-delta strip. Six cells (Cost,
// Tokens, Prompts, Errors, Duration, Files), each "A → B" with a delta. Every
// delta routes through S1's semantics chokepoint (deltaIntent → semanticState),
// so the cell carries an arrow + colour + signed value + a screen-reader label
// and NEVER conveys state by colour alone (a11y). Lower-is-better metrics (cost
// / errors / duration) that worsen earn the red ▲ "regression" just as visibly
// as an improvement earns the green ▼; the truly-neutral metrics (tokens /
// prompts / files) show a grey directional arrow with no value-judgment.
//
// All human-readable numbers route through lib/fmt so the strip matches the
// rest of the dashboard's typographic standard (currency, k/M token humanizer,
// the comparison-local "7m" / "1h 56m" compact duration, U+2212 signed minus).

interface Row {
  key: 'cost' | 'tokens' | 'prompts' | 'errors' | 'duration' | 'files';
  label: string;
  a: number | null;
  b: number | null;
  // Lower-is-better → a B<A delta is an improvement. Neutral metrics never earn
  // an improve/regress intent (more tokens / files is neither good nor bad).
  lowerBetter: boolean;
  fmtVal: (v: number | null) => string;
  fmtDelta: (delta: number) => string;
}

const intDelta = (d: number): string => (d === 0 ? '±0' : d > 0 ? `+${d}` : `−${Math.abs(d)}`);
const tokensDelta = (d: number): string =>
  d === 0 ? '±0' : `${d > 0 ? '+' : '−'}${fmt.tokens(Math.abs(d))}`;
const durationDelta = (d: number): string =>
  d === 0 ? '±0' : `${d > 0 ? '+' : '−'}${fmt.durationCompact(Math.abs(d))}`;

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
      fmtVal: fmt.durationCompact, fmtDelta: durationDelta },
    { key: 'files', label: 'Files', a: a.files, b: b.files, lowerBetter: false,
      fmtVal: (v) => (v == null ? '—' : String(v)), fmtDelta: intDelta },
  ];

  return (
    // #240 / #242 — the wrapper is the inline-size query container for the strip
    // below. An element can't @container-query itself, so the grid's 6→3→2→1
    // reflow keys on this wrapper's actual width (NOT the viewport): on desktop the
    // discovery rail stays mounted and narrows the comparison column while the
    // viewport is still ≥1100px, a squeeze a viewport media query can't see. The
    // 1-up step (≤340px container) kills the residual Cost/Duration ellipsis in
    // the rail-mounted 650–720px band and on sub-343px mobile (#242).
    <div className="conv-cmp-metrics-wrap">
      <div className="conv-cmp-metrics" role="group" aria-label="Comparison metrics">
        {rows.map((r) => {
          const haveBoth = r.a != null && r.b != null;
          const delta = haveBoth ? (r.b as number) - (r.a as number) : null;
          const polarity: Polarity = r.lowerBetter ? 'lower-better' : 'neutral';
          const { direction, intent } = deltaIntent(polarity, r.a, r.b);
          const pres = semanticState(intent, direction);
          const showDelta = delta != null && delta !== 0;
          return (
            <div key={r.key} className="conv-cmp-metric" data-metric={r.key}>
              <div className="conv-cmp-metric-label">{r.label}</div>
              <div className="conv-cmp-metric-vals">
                <span className="conv-cmp-metric-a">{r.fmtVal(r.a)}</span>
                <span className="conv-cmp-metric-arrow" aria-hidden="true"> → </span>
                <span className="conv-cmp-metric-b">{r.fmtVal(r.b)}</span>
              </div>
              {showDelta && (
                <div className={`conv-cmp-metric-delta ${pres.className}`}>
                  {pres.glyph && <span className="conv-cmp-metric-dir" aria-hidden="true">{pres.glyph} </span>}
                  {r.fmtDelta(delta as number)}
                  <span className="sr-only"> ({pres.srLabel})</span>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
