import { fmt } from '../lib/fmt';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { ModelCostBars } from './ModelCostBars';
import type { PeriodRow } from '../types/envelope';

interface Props {
  row: PeriodRow;
  variant: 'weekly' | 'monthly' | 'daily';
  accentClass: 'accent-cyan' | 'accent-pink' | 'accent-indigo';
  periodNoun?: string;
  windowLabel?: string;
}

export function PeriodDetailCard({
  row, variant, accentClass, periodNoun, windowLabel,
}: Props) {
  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  // F1: was a hand-rolled UTC formatter built on getUTCMonth / getUTCDate
  // / getUTCHours / getUTCMinutes that hard-coded "UTC" in the output.
  // Replaced with `fmt.datetimeShort`, which honors ctx.tz and emits the
  // correct offset suffix from ctx.offsetLabel for the active display zone.
  const fmtSubscriptionWindow = (start: string, end: string): string =>
    `${fmt.datetimeShort(start, ctx)} → ${fmt.datetimeShort(end, ctx)}`;
  const deltaCls =
    row.delta_cost_pct == null ? 'flat' :
    row.delta_cost_pct > 0 ? 'up' : row.delta_cost_pct < 0 ? 'down' : 'flat';
  const noun =
    periodNoun ?? (variant === 'weekly' ? 'week' :
    variant === 'monthly' ? 'month' : 'day');
  const weeklyWindowLabel = windowLabel ?? 'Subscription window';
  // "Today" for daily; "Now" for weekly/monthly. Only rendered when
  // is_current is true (today's date / current week / current month).
  const currentLabel = variant === 'daily' ? 'Today' : 'Now';
  return (
    <div className={`detail-card ${accentClass}`}>
      <div className="head">
        <div className="big">
          {row.source != null && row.source !== 'all' && (
            <span
              className={`source-chip source-chip--${row.source}`}
              data-period-source={row.source}
            >
              {row.source === 'claude' ? 'Claude' : 'Codex'}
            </span>
          )}
          {row.label}
          {row.is_current && <span className="pill-current">{currentLabel}</span>}
        </div>
        <div>
          <span className="cost" style={{ color: 'var(--text)', fontWeight: 700 }}>{fmt.usd2(row.cost_usd)}</span>
          {' '}
          {row.delta_cost_pct == null
            ? <span className="delta flat">—</span>
            : <span className={`delta ${deltaCls}`}>{fmt.deltaPct(row.delta_cost_pct)} vs prior {noun}</span>}
        </div>
      </div>
      {variant === 'weekly' && row.week_start_at && row.week_end_at && (
        <div className="window">
          {weeklyWindowLabel}: {fmtSubscriptionWindow(row.week_start_at, row.week_end_at)}
        </div>
      )}
      <ModelCostBars rows={row.models.map((m) => ({ model: m.model, cost_usd: m.cost_usd, label: m.display }))} />

      <div className="tokens-row">
        {row.codex_tokens ? (
          <>
            <div className="t"><span className="k">Input</span><span className="v">{fmt.compact(row.codex_tokens.input_tokens)}</span></div>
            <div className="t"><span className="k">Cached input</span><span className="v">{fmt.compact(row.codex_tokens.cached_input_tokens)}</span></div>
            <div className="t"><span className="k">Output</span><span className="v">{fmt.compact(row.codex_tokens.output_tokens)}</span></div>
            <div className="t"><span className="k">Reasoning</span><span className="v">{fmt.compact(row.codex_tokens.reasoning_output_tokens)}</span></div>
            <div className="t"><span className="k">Total</span><span className="v">{fmt.compact(row.codex_tokens.total_tokens)}</span></div>
          </>
        ) : (
          <>
            <div className="t"><span className="k">Input</span><span className="v">{fmt.compact(row.input_tokens)}</span></div>
            <div className="t"><span className="k">Output</span><span className="v">{fmt.compact(row.output_tokens)}</span></div>
            <div className="t"><span className="k">Cache+</span><span className="v">{fmt.compact(row.cache_creation_tokens)}</span></div>
            <div className="t"><span className="k">Cache-read</span><span className="v">{fmt.compact(row.cache_read_tokens)}</span></div>
            <div className="t"><span className="k">Total</span><span className="v">{fmt.compact(row.total_tokens)}</span></div>
          </>
        )}
        {row.cache_hit_pct != null && (
          <div className="t cache">
            <span className="k">Cache hit</span>
            <span className="v">{row.cache_hit_pct.toFixed(1)}%</span>
            <div className="bar">
              <div
                className="fill"
                style={{ width: `${Math.min(100, Math.max(0, row.cache_hit_pct))}%` }}
              />
            </div>
          </div>
        )}
      </div>
      {variant === 'weekly' && (
        <div className="stats2">
          <div className="s"><span className="k">Used %</span><span className="v">{fmt.pct0(row.used_pct)}</span></div>
          <div className="s"><span className="k">$/1%</span><span className="v">{fmt.usd2(row.dollar_per_pct)}</span></div>
        </div>
      )}
    </div>
  );
}
