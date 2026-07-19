import { useEffect, useRef, useSyncExternalStore } from 'react';
import { useSnapshot } from '../hooks/useSnapshot';
import { useIsMobile } from '../hooks/useIsMobile';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { fmt, type FmtCtx } from '../lib/fmt';
import { resolveVerdict } from '../lib/verdict';
import { humanizeAge } from '../lib/syncFreshness';
import { heroFreshnessLabel } from '../lib/heroFreshness';
import { cardRegionClick } from '../lib/cardRegion';
import { joinCodexQuotaLabels } from '../lib/sourceRows';
import { warningForDomain } from '../lib/sourceGating';
import { resolveSourceView } from '../store/sourceView';
import { dispatch, getState, subscribeStore } from '../store/store';
import type { AllSourceData, CodexSourceData, Envelope } from '../types/envelope';

// HeroStrip (#264 S1, spec §4; #294 S5 §6.1) — the dashboard's full-width
// at-a-glance hero. The shared three-zone component keeps Claude's canonical
// anatomy while its adapter supplies Codex native-cycle or All combined values.
// Independent provider quota percentages are always labelled and never summed.
// Mounted only on the dashboard branch of App.tsx.

export function HeroStrip() {
  const env = useSnapshot();
  const activeSource = useSyncExternalStore(subscribeStore, () => getState().activeSource);
  const h = env?.header;
  const cw = env?.current_week ?? null;
  const freshness = cw?.freshness ?? null;
  const verdict = resolveVerdict(h?.forecast_verdict ?? null);
  const heroLabel = heroFreshnessLabel(freshness?.age_seconds);
  const display = useDisplayTz();
  const ctx: FmtCtx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };

  // #248 §6 — mobile-only sticky-collapse (unchanged; source-agnostic).
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
      { threshold: 0, rootMargin: '-64px 0px 0px 0px' },
    );
    io.observe(el);
    return () => {
      io.disconnect();
      dispatch({ type: 'SET_HERO_SCROLLED', scrolled: false });
    };
  }, [isMobile]);

  const openCurrentWeek = () => dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });

  const activate = activeSource === 'claude'
    ? openCurrentWeek
    : () => dispatch({ type: 'SHOW_STATUS_TOAST', text: `${activeSource === 'codex' ? 'Codex' : 'Combined'} cycle details remain source-bound in the dashboard cards.` });

  const body = <SharedHero source={activeSource} env={env} ctx={ctx} verdict={verdict} heroLabel={heroLabel} />;

  return (
    <section
      ref={heroRef}
      className="hero-strip"
      role="region"
      tabIndex={0}
      aria-label="Week usage summary"
      data-hero-strip=""
      data-source={activeSource}
      onClick={cardRegionClick(activate)}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget) return;
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          activate();
        }
      }}
    >
      {body}
    </section>
  );
}

function SharedHero({
  source,
  env,
  ctx,
  verdict,
  heroLabel,
}: {
  source: 'claude' | 'codex' | 'all';
  env: Envelope | null;
  ctx: FmtCtx;
  verdict: ReturnType<typeof resolveVerdict>;
  heroLabel: string;
}) {
  const h = env?.header;
  const cw = env?.current_week ?? null;
  if (source === 'claude') {
    return <ClaudeHero h={h} cw={cw} ctx={ctx} verdict={verdict} heroLabel={heroLabel} freshness={cw?.freshness ?? null} />;
  }
  const codexEntry = resolveSourceView(env, 'codex').entry;
  const codex = codexEntry?.data as CodexSourceData | undefined;
  const allEntry = resolveSourceView(env, 'all').entry;
  const all = allEntry?.data as AllSourceData | undefined;
  const windows = codex?.hero && codex.quota ? joinCodexQuotaLabels(codex.hero, codex.quota) : [];
  const weekly = windows.find((window) => window.windowMinutes === 10_080) ?? windows[0];
  const fiveHour = windows.find((window) => window.windowMinutes === 300);
  const codexUnavailable = codexEntry?.capabilities?.hero?.status === 'unavailable'
    || codex?.hero?.cost_usd == null || codex?.hero?.total_tokens == null;
  const warning = warningForDomain(codexEntry?.warnings, 'hero');
  const combined = all?.combined ?? null;
  const allWarning = warningForDomain(allEntry?.warnings, 'hero');
  const quotaForecast = codex?.quota.histories.find((row) => row.key === weekly?.key)?.forecast;
  const budget = codex?.budget.status;

  const primaryLabel = source === 'codex' ? '7-DAY LIMIT' : 'CLAUDE 7-DAY';
  const primaryValue = source === 'codex' ? fmt.pct0(weekly?.current.current_percent) : fmt.pct1(h?.used_pct);
  const secondaryLabel = source === 'all' ? 'CODEX 7-DAY' : '5-HOUR';
  const secondaryValue = source === 'codex' ? fmt.pct0(fiveHour?.current.current_percent) : fmt.pct0(weekly?.current.current_percent);
  const resetSeconds = weekly?.current.resets_at ? Math.max(0, (Date.parse(weekly.current.resets_at) - Date.now()) / 1000) : null;
  const spentLabel = source === 'codex' ? 'SPENT THIS CYCLE' : 'COMBINED SPEND';
  const spent = source === 'codex' ? codex?.hero.cost_usd : combined?.cost_usd;
  const totalTokens = source === 'codex' ? codex?.hero.total_tokens : combined?.total_tokens;

  return (
    <>
      <div className="hero-zone hero-usage" data-testid="shared-hero-usage">
        <div className="hu-block">
          <div className="hu-label">{primaryLabel}</div>
          <div className="hu-num">{primaryValue}</div>
        </div>
        <div className="hu-block">
          <div className="hu-label">{secondaryLabel}</div>
          <div className="hu-num hu-num--sm">{secondaryValue}</div>
        </div>
        <div className="hu-reset">resets in <span>{fmt.ddhh(resetSeconds)}</span></div>
      </div>

      <div className="hero-zone hero-spent" data-testid="shared-hero-spent">
        <div className="hs-label">{spentLabel}</div>
        <div className="hs-big">{spent == null ? '—' : fmt.usd0(spent)}</div>
        <div className="hs-sub">
          {codexUnavailable && source === 'codex'
              ? warning?.message ?? 'Cycle accounting unavailable'
              : source === 'all' && combined == null
                ? allWarning?.message ?? 'Combined totals are unavailable while a provider is degraded.'
              : <><span>{fmt.tokens(totalTokens)}</span> total tokens</>}
        </div>
      </div>

      <div className="hero-zone hero-support" data-testid="shared-hero-support">
        <div className="sup-row">
          <span className="sup-l">{source === 'codex' ? 'Forecast @ reset' : 'Claude quota'}</span>
          <span className="sup-v">
            {source === 'codex' ? fmt.pct0(quotaForecast?.projected_percent) : fmt.pct1(h?.used_pct)}
          </span>
        </div>
        <div className="sup-row">
          <span className="sup-l">{source === 'codex' ? 'Budget' : 'Codex quota'}</span>
          <span className="sup-v">{source === 'codex' ? budget == null ? '—' : `${fmt.pct0(budget.consumption_pct)} of ${fmt.usd0(budget.budget_usd)}` : fmt.pct1(weekly?.current.current_percent)}</span>
        </div>
        <div className="sup-row">
          <span className="sup-l">{source === 'codex' ? 'Snapshot' : 'Providers'}</span>
          <span className="sup-v">
            {source === 'codex' ? weekly?.current.freshness ?? 'unavailable' : 'Claude · Codex'}
          </span>
        </div>
      </div>
    </>
  );
}

// ---- Claude hero (unchanged subscription-week vocabulary) -------------

function ClaudeHero({
  h,
  cw,
  ctx,
  verdict,
  heroLabel,
  freshness,
}: {
  h: Envelope['header'] | undefined;
  cw: Envelope['current_week'] | null;
  ctx: FmtCtx;
  verdict: ReturnType<typeof resolveVerdict>;
  heroLabel: string;
  freshness: NonNullable<Envelope['current_week']>['freshness'];
}) {
  return (
    <>
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
          const flat = Math.abs(d) < 0.02;
          const good = d < 0;
          const icon = flat ? 'minus' : good ? 'trending-down' : 'trending-up';
          const color = flat
            ? 'var(--text-dim)'
            : good ? 'var(--accent-green)' : 'var(--accent-red)';
          const dirWord = flat ? 'flat' : good ? 'down' : 'up';
          const mag = fmt.usd2(Math.abs(d));
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
              {humanizeAge(freshness.age_seconds)}
            </span>
          </div>
        )}
      </div>
    </>
  );
}
