import { fmt } from '../lib/fmt';
import { cacheVocabulary } from '../lib/cacheReportVocabulary';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { ModelCostBars } from './ModelCostBars';
import type { DailyPanelRow, PeriodRow } from '../types/envelope';
import { modelChipStyle } from '../lib/model';
import {
  CREDIT_MARKER_LABEL, CREDIT_MARKER_TITLE, withheldDollarPerPctLabel,
} from '../lib/creditMarker';

interface Props {
  row: PeriodRow;
  variant: 'weekly' | 'monthly' | 'daily';
  accentClass: 'accent-cyan' | 'accent-pink' | 'accent-indigo';
  periodNoun?: string;
  windowLabel?: string;
  // #556 S2 §6.3 — the per-provider breakdown #312 §7.4 requires in exchange
  // for aggregating daily cost. Supplied only under All; a provider with no
  // activity that day is `null` rather than a $0.00 row, so the card can say
  // it had none instead of showing a figure nobody measured.
  providerLegs?: { claude: DailyPanelRow | null; codex: DailyPanelRow | null };
}

function DailyProviderLegs({
  legs,
}: { legs: { claude: DailyPanelRow | null; codex: DailyPanelRow | null } }) {
  const entries = (['claude', 'codex'] as const).map((source) => ({
    source,
    label: source === 'claude' ? 'Claude' : 'Codex',
    row: legs[source],
  }));
  return (
    <div className="daily-provider-legs" data-testid="daily-provider-legs">
      {entries.map(({ source, label, row }) => (
        <div key={source} className="daily-provider-leg" data-provider-leg={source}>
          <span className={`source-chip source-chip--${source}`}>{label}</span>
          {row == null ? (
            <span className="muted">No activity</span>
          ) : (
            <>
              <span className="cost">{fmt.usd2(row.cost_usd)}</span>
              {/* §6.2 — chips render PER LEG. The merged row carries no model
                  set at all, because one stack over two providers' families
                  sharing one denominator is not a model split of anything. */}
              <span className="models-chips">
                {row.models.map((m) => (
                  <span
                    key={m.model}
                    className={`chip ${m.chip}`}
                    style={modelChipStyle(m.model)}
                  >
                    {m.display}
                  </span>
                ))}
              </span>
            </>
          )}
        </div>
      ))}
    </div>
  );
}

export function PeriodDetailCard({
  row, variant, accentClass, periodNoun, windowLabel, providerLegs,
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
  // Only a weekly row carries a `$/1%` at all, so only a weekly row can
  // withhold one. A rendered ratio always wins: the server publishes a cause
  // alongside a value on no path, and preferring the value keeps a stale cause
  // from replacing a figure that is present.
  const withheldLabel = variant === 'weekly' && row.dollar_per_pct == null
    ? withheldDollarPerPctLabel(row.dollar_per_pct_withheld)
    : null;
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
          {variant === 'weekly' && row.credited && (
            <span className="pill-credited" title={CREDIT_MARKER_TITLE}>
              {CREDIT_MARKER_LABEL}
            </span>
          )}
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
      {providerLegs && <DailyProviderLegs legs={providerLegs} />}

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
            {/* #443 S2 — source-aware: on a Codex row this figure is
                cached_input / input, which is token reuse, not a cache hit. */}
            <span className="k">{cacheVocabulary(row.source ?? 'claude').percentLabel}</span>
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
          {/* The weekly variant fills two of this grid's five tracks, so the
              `$/1%` cell is 85px wide at desktop. A figure fits; a 21-character
              cause wrapped to three lines with a word orphaned on the label
              line, tripling the row's height while ~255px of grid sat empty to
              its right. `s-wide` gives the prose cell the rest of the row.
              Not a media query: at the mobile breakpoint the grid is already
              `1fr 1fr` and `2 / -1` resolves to the same single track it has
              today, where the cause reads well in two lines. */}
          <div className={`s${withheldLabel != null ? ' s-wide' : ''}`}>
            <span className="k">$/1%</span>
            {/* #703 + #707 §6.3 — a withheld ratio prints its CAUSE, following
                the `explain` command's rule. `fmt.usd2(null)` renders an
                em-dash, which reads as "no usage recorded" and is a different,
                wrong statement about a week that holds a credit. */}
            {withheldLabel != null
              ? <span className="v withheld" title={CREDIT_MARKER_TITLE}>{withheldLabel}</span>
              : <span className="v">{fmt.usd2(row.dollar_per_pct)}</span>}
          </div>
        </div>
      )}
    </div>
  );
}
