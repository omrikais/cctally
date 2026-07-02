import { useEffect, useRef } from 'react';
import { useSnapshot } from '../hooks/useSnapshot';
import { useIsMobile } from '../hooks/useIsMobile';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { fmt, type FmtCtx } from '../lib/fmt';
import { resolveVerdict } from '../lib/verdict';
import { humanizeAge } from '../lib/syncFreshness';
import { heroFreshnessLabel } from '../lib/heroFreshness';
import { dispatch } from '../store/store';

// HeroStrip (#264 S1, spec §4) — the dashboard's full-width at-a-glance hero,
// rebuilt into THREE zones that answer the two questions users actually ask —
// "how much have I used?" and "how much have I spent?" — with two dominant
// numbers instead of the #248 flex band's empty centre:
//   • usage  — WEEK USAGE big % + paired 5-HOUR + reset countdown
//   • spent  — SPENT THIS WEEK whole-$ hero + $/1% sub
//   • support— Forecast @ reset · $/1% vs last week · Snapshot age
// The hero opens the (rich) Current Week modal on click/Enter/Space. Mounted
// only on the dashboard branch of App.tsx — never in the conversations view,
// nor the loading/error branches. Freshness is de-alarmed client-side
// (heroFreshnessLabel) so a benign 8-minute snapshot reads calm (FRESH-1).

export function HeroStrip() {
  const env = useSnapshot();
  const h = env?.header;
  const cw = env?.current_week ?? null;
  const freshness = cw?.freshness ?? null;
  const display = useDisplayTz();
  const ctx: FmtCtx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  // Forecast metric tint — verdict drives calm-green / amber / red (H1/§4).
  const verdict = resolveVerdict(h?.forecast_verdict ?? null);
  // #264 S1 (FRESH-1/HERO-4) — re-derive the freshness tint from the
  // already-shipped age_seconds with dashboard-appropriate thresholds; the
  // server `freshness.label` is untouched (shared with TUI + refresh-usage).
  const heroLabel = heroFreshnessLabel(freshness?.age_seconds);

  // #248 §6 — mobile-only sticky-collapse. Watch the hero block; once it scrolls
  // out of the viewport, flip `heroScrolled` so the Header reveals its condensed
  // Used%/reset readout (keeping the sticky bar one row ≤64px). Guarded for a
  // missing IntersectionObserver (JSDOM / SSR — mirrors ConversationReader);
  // disconnects + resets the flag on unmount / view switch (HeroStrip unmounts
  // when leaving the dashboard view, so the readout never lingers).
  const heroRef = useRef<HTMLElement>(null);
  const isMobile = useIsMobile();
  useEffect(() => {
    if (!isMobile) return;
    const el = heroRef.current;
    if (!el || typeof IntersectionObserver === 'undefined') return;
    const io = new IntersectionObserver(
      (entries) => {
        const entry = entries[0];
        if (!entry) return;
        dispatch({ type: 'SET_HERO_SCROLLED', scrolled: !entry.isIntersecting });
      },
      // Negative top rootMargin = the hero is "gone" the moment it slips behind
      // the ≤64px sticky bar, not only when it fully clears the viewport top —
      // so the condensed readout reveals in lockstep with the hero hiding.
      { threshold: 0, rootMargin: '-64px 0px 0px 0px' },
    );
    io.observe(el);
    return () => {
      io.disconnect();
      dispatch({ type: 'SET_HERO_SCROLLED', scrolled: false });
    };
  }, [isMobile]);

  const openCurrentWeek = () => dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });

  return (
    <section
      ref={heroRef}
      className="hero-strip"
      // House pattern (matches all grid panels): a focusable region, NOT
      // role="button" — a button's accessible name would flatten the KPIs out
      // of the AT browse tree, defeating the at-a-glance read for SR users. The
      // region stays keyboard-activatable (Enter/Space) to open the modal.
      role="region"
      tabIndex={0}
      aria-label="Week usage summary"
      data-hero-strip=""
      onClick={openCurrentWeek}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          openCurrentWeek();
        }
      }}
    >
      <div className="hero-zone hero-usage">
        <div className="hu-block">
          <div className="hu-label">
            WEEK USAGE
            {h?.week_label ? <span className="hu-week"> · {h.week_label}</span> : null}
          </div>
          <div className="hu-num">{fmt.pct1(h?.used_pct)}</div>
        </div>
        <div className="hu-block">
          <div className="hu-label">5-HOUR</div>
          <div className="hu-num hu-num--sm">{fmt.pct0(h?.five_hour_pct)}</div>
        </div>
        <div className="hu-reset">
          resets in <span>{fmt.ddhh(cw?.reset_in_sec)}</span>
        </div>
      </div>

      <div className="hero-zone hero-spent">
        <div className="hs-label">SPENT THIS WEEK</div>
        <div className="hs-big">{fmt.usd0(cw?.spent_usd)}</div>
        <div className="hs-sub">
          <span>{fmt.usd2(h?.dollar_per_pct)}</span> / 1% used
        </div>
      </div>

      <div className="hero-zone hero-support">
        <div className="sup-row">
          <span className="sup-l">Forecast @ reset</span>
          <span className={`sup-v${verdict ? ` is-${verdict.cls}` : ''}`}>
            {fmt.pct0(h?.forecast_pct)}
          </span>
        </div>
        {/* "vs last week" $/1% delta. The SVG icon is the ONLY arrow — the
            visible value shows the magnitude; direction is conveyed by the
            icon, its color, and the aria-label (never a duplicated text arrow)
            so color-blind / screen-reader users get the direction without hue.
            Logic ported verbatim from the #248 hero metric (originally the
            retired Header IIFE, #207 B1); re-wrapped as a support row. */}
        {(() => {
          const d = h?.vs_last_week_delta;
          if (d == null) {
            return (
              <div className="sup-row" data-metric="vs-last-week">
                <span className="sup-l">$/1% vs last week</span>
                <span className="sup-v">—</span>
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
            <div className="sup-row" data-metric="vs-last-week" aria-label={aria}>
              <span className="sup-l">$/1% vs last week</span>
              <span className="sup-v">
                <svg className="icon" aria-hidden="true" style={{ color }}>
                  <use href={`/static/icons.svg#${icon}`} />
                </svg>
                <span>{flat ? 'flat' : mag}</span>
              </span>
            </div>
          );
        })()}
        {freshness && (
          <div className="sup-row">
            <span className="sup-l">Snapshot</span>
            <span
              className={`sup-v sup-fresh chip-${heroLabel}`}
              data-freshness={heroLabel}
              title={`Captured ${fmt.datetimeShort(freshness.captured_at, ctx)}`}
            >
              {heroLabel === 'stale' ? '⚠ ' : ''}
              {/* #259 — humanize the raw-seconds age ("97928s ago" → "1d 3h
                  ago") so this reads consistently with the sync chip. */}
              {humanizeAge(freshness.age_seconds)}
            </span>
          </div>
        )}
      </div>
    </section>
  );
}
