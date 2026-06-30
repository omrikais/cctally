import { useSnapshot } from '../hooks/useSnapshot';
import { fmt } from '../lib/fmt';
import { resolveVerdict } from '../lib/verdict';
import { dispatch } from '../store/store';

// HeroStrip (#248, spec §1) — the dashboard's full-width at-a-glance hero. It
// replaces the five header `.stat` blocks and the Current Week grid card: the
// single most important number (weekly Used %) dominates, flanked by four
// spelled-out metrics. The hero opens the (rich) Current Week modal on
// click/Enter/Space. Mounted only on the dashboard branch of App.tsx — never in
// the conversations view, nor the loading/error branches.

// "14:32:05" — clock-only freshness stamp (ported from the retired
// CurrentWeekPanel so the card's freshness reading lands here unchanged).
function formatHHMMSS(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (isNaN(d.getTime())) return null;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export function HeroStrip() {
  const env = useSnapshot();
  const h = env?.header;
  const cw = env?.current_week ?? null;
  const freshness = cw?.freshness ?? null;
  // Forecast metric tint — verdict drives calm-green / amber / red (H1/§4).
  const verdict = resolveVerdict(h?.forecast_verdict ?? null);

  const openCurrentWeek = () => dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });

  return (
    <section
      className="hero-strip"
      role="button"
      tabIndex={0}
      aria-label="Open Current Week detail"
      data-hero-strip=""
      onClick={openCurrentWeek}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          openCurrentWeek();
        }
      }}
    >
      <div className="hero-main">
        <div className="hero-eyebrow">
          WEEK USAGE
          {h?.week_label ? <span className="hero-week"> · {h.week_label}</span> : null}
        </div>
        <div className="hero-num">{fmt.pct1(h?.used_pct)}</div>
        <div className="hero-sub">
          resets in <span>{fmt.ddhh(cw?.reset_in_sec)}</span>
          {' · '}
          <span>{fmt.usd2(cw?.spent_usd)}</span> spent
        </div>
        {freshness && (
          <span
            className={`chip chip-${freshness.label}`}
            data-freshness={freshness.label}
            title={`Captured ${freshness.captured_at}`}
          >
            {freshness.label === 'stale' ? '⚠ ' : ''}
            as of {formatHHMMSS(freshness.captured_at) ?? freshness.captured_at}
            {' · '}
            {freshness.age_seconds}s ago
          </span>
        )}
      </div>
      <div className="hero-metrics">
        <div className="hero-metric" data-metric="cost-per-pct">
          <div className="hero-metric-label">cost / 1%</div>
          <div className="hero-metric-val">{fmt.usd2(h?.dollar_per_pct)}</div>
        </div>
        <div className="hero-metric" data-metric="forecast">
          <div className="hero-metric-label">Forecast</div>
          <div className={`hero-metric-val${verdict ? ` is-${verdict.cls}` : ''}`}>
            {fmt.pct0(h?.forecast_pct)}
          </div>
        </div>
        <div className="hero-metric" data-metric="five-hour">
          <div className="hero-metric-label">5-hour</div>
          <div className="hero-metric-val">{fmt.pct0(h?.five_hour_pct)}</div>
        </div>
        {/* "vs last week" $/1% delta. The SVG icon is the ONLY arrow — the
            visible value shows the magnitude; direction is conveyed by the
            icon, its color, and the aria-label (never a duplicated text arrow)
            so color-blind / screen-reader users get the direction without hue.
            Logic ported verbatim from the retired Header IIFE (#207 B1). */}
        {(() => {
          const d = h?.vs_last_week_delta;
          if (d == null) {
            return (
              <div className="hero-metric" data-metric="vs-last-week">
                <div className="hero-metric-label">vs last week</div>
                <div className="hero-metric-val">—</div>
              </div>
            );
          }
          const flat = Math.abs(d) < 0.02;            // parity with the TUI dim band
          const good = d < 0;                          // cheaper per 1% is better
          const icon = flat ? 'minus' : good ? 'trending-down' : 'trending-up';
          const color = flat
            ? 'var(--text-dim)'
            : good ? 'var(--accent-green)' : 'var(--accent-red)';
          const dirWord = flat ? 'flat' : good ? 'down' : 'up';
          const mag = fmt.usd2(Math.abs(d));           // e.g. "$0.12"
          const aria = flat
            ? '$/1% flat versus last week'
            : `$/1% ${dirWord} ${mag} versus last week`;
          return (
            <div className="hero-metric" data-metric="vs-last-week" aria-label={aria}>
              <div className="hero-metric-label">vs last week</div>
              <div className="hero-metric-val">
                <svg className="icon" aria-hidden="true" style={{ color }}>
                  <use href={`/static/icons.svg#${icon}`} />
                </svg>
                <span>{flat ? 'flat' : mag}</span>
              </div>
            </div>
          );
        })()}
      </div>
    </section>
  );
}
