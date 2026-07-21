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
import type { AllSourceData, CodexSourceData, Envelope, FreshnessEnvelope } from '../types/envelope';

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
  const activate = openCurrentWeek;

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
    return (
      <CanonicalHero
        weekLabel={h?.week_label}
        usedPct={h?.used_pct}
        fiveHourPct={h?.five_hour_pct}
        resetInSec={cw?.reset_in_sec}
        spentUsd={cw?.spent_usd}
        dollarPerPct={h?.dollar_per_pct}
        forecastPct={h?.forecast_pct}
        vsLastWeekDelta={h?.vs_last_week_delta}
        freshness={cw?.freshness ?? null}
        ctx={ctx}
        verdict={verdict}
        heroLabel={heroLabel}
        showFiveHour
      />
    );
  }
  const codexEntry = resolveSourceView(env, 'codex').entry;
  const codex = codexEntry?.data as CodexSourceData | undefined;
  const cycle = codex?.hero.cycle;
  const windows = codex?.hero && codex.quota ? joinCodexQuotaLabels(codex.hero, codex.quota) : [];
  const weekly = [...windows].sort((a, b) => {
    const aMatchesCycle = a.current.resets_at === cycle?.resets_at;
    const bMatchesCycle = b.current.resets_at === cycle?.resets_at;
    if (aMatchesCycle !== bMatchesCycle) return aMatchesCycle ? -1 : 1;
    const aIsWeekly = a.windowMinutes === 10_080;
    const bIsWeekly = b.windowMinutes === 10_080;
    if (aIsWeekly !== bIsWeekly) return aIsWeekly ? -1 : 1;
    return b.current.current_percent - a.current.current_percent
      || Date.parse(b.current.captured_at) - Date.parse(a.current.captured_at);
  })[0];
  const fiveHour = windows.find((window) => window.windowMinutes === 300);
  const codexUnavailable = codexEntry?.capabilities?.hero?.status === 'unavailable'
    || codex?.hero?.cost_usd == null;
  const warning = warningForDomain(codexEntry?.warnings, 'hero');
  const quotaForecast = codex?.quota.histories.find((row) => row.key === weekly?.key)?.forecast;
  const resetSeconds = weekly?.current.resets_at ? Math.max(0, (Date.parse(weekly.current.resets_at) - Date.now()) / 1000) : null;
  const capturedMs = weekly ? Date.parse(weekly.current.captured_at) : Number.NaN;
  const ageSeconds = Number.isFinite(capturedMs)
    ? Math.max(0, (Date.now() - capturedMs) / 1000)
    : null;
  const codexHeroLabel = heroFreshnessLabel(ageSeconds);
  const usedPct = weekly?.current.current_percent ?? null;
  const spentUsd = codexUnavailable ? null : codex?.hero.cost_usd;
  const dollarPerPct = spentUsd != null && usedPct != null && usedPct > 0
    ? spentUsd / usedPct
    : null;
  const cycleStartMs = codex?.hero.cycle?.start_at ? Date.parse(codex.hero.cycle.start_at) : Number.NaN;
  const previousDollarPerPct = codex?.periods.weekly.rows
    .filter((row) => {
      const endMs = row.end_at ? Date.parse(row.end_at) : Number.NaN;
      return row.dollar_per_pct != null
        && Number.isFinite(cycleStartMs)
        && Number.isFinite(endMs)
        && endMs <= cycleStartMs;
    })
    .sort((a, b) => Date.parse(b.end_at ?? '') - Date.parse(a.end_at ?? ''))[0]
    ?.dollar_per_pct ?? null;
  const vsLastWeekDelta = dollarPerPct != null && previousDollarPerPct != null
    ? dollarPerPct - previousDollarPerPct
    : null;
  const forecastPct = quotaForecast?.status === 'ok'
    ? quotaForecast.projected_percent
    : null;
  const codexVerdict = forecastPct == null
    ? null
    : resolveVerdict(forecastPct >= 100 ? 'capped' : forecastPct >= 90 ? 'cap' : 'ok');
  const codexFreshness: FreshnessEnvelope | null = weekly && ageSeconds != null
    ? { ...weekly.current, label: codexHeroLabel, age_seconds: ageSeconds }
    : null;
  const cycleStartLabel = cycle ? fmt.dateShort(cycle.start_at, ctx) : null;
  const cycleEndLabel = cycle ? fmt.dateShort(cycle.resets_at, ctx) : null;
  const weekLabel = cycleStartLabel && cycleEndLabel
    ? `${cycleStartLabel}–${cycleEndLabel}`
    : cycleStartLabel ?? cycleEndLabel;

  if (source === 'codex') {
    return (
      <CanonicalHero
        weekLabel={weekLabel}
        usedPct={usedPct}
        fiveHourPct={fiveHour?.current.current_percent}
        resetInSec={resetSeconds}
        spentUsd={spentUsd}
        dollarPerPct={dollarPerPct}
        forecastPct={forecastPct}
        vsLastWeekDelta={vsLastWeekDelta}
        freshness={codexFreshness}
        ctx={ctx}
        verdict={codexVerdict}
        heroLabel={codexHeroLabel}
        showFiveHour={fiveHour != null}
        unavailableReason={codexUnavailable
          ? warning?.message ?? 'Cycle accounting unavailable'
          : null}
      />
    );
  }

  const allEntry = resolveSourceView(env, 'all').entry;
  const all = allEntry?.data as AllSourceData | undefined;
  const combined = all?.combined ?? null;
  const allWarning = warningForDomain(allEntry?.warnings, 'hero');
  const allWarningDetail = allWarning?.message
    ?? 'Combined totals are unavailable while a provider is degraded.';

  return (
    <>
      <div className="hero-zone hero-usage" data-testid="shared-hero-usage">
        <div className="hu-block">
          <div className="hu-label">CLAUDE 7-DAY</div>
          <div className="hu-num">{fmt.pct1(h?.used_pct)}</div>
        </div>
        <div className="hu-block">
          <div className="hu-label">CODEX 7-DAY</div>
          <div className="hu-num hu-num--sm">{fmt.pct0(weekly?.current.current_percent)}</div>
        </div>
        <div className="hu-reset">resets in <span>{fmt.ddhh(resetSeconds)}</span></div>
      </div>

      <div className="hero-zone hero-spent" data-testid="shared-hero-spent">
        <div className="hs-label">COMBINED SPEND</div>
        <div className="hs-big">{combined?.cost_usd == null ? '—' : fmt.usd0(combined.cost_usd)}</div>
        <div className="hs-sub">
          {combined == null
            ? (
              <span
                className="panel-degraded-chip hero-warning-chip"
                data-testid="shared-hero-warning"
                title={allWarningDetail}
                aria-label={`Combined totals unavailable: ${allWarningDetail}`}
              >
                Combined unavailable
              </span>
            )
            : <><span>{fmt.tokens(combined.total_tokens)}</span> total tokens</>}
        </div>
      </div>

      <div className="hero-zone hero-support" data-testid="shared-hero-support">
        <div className="sup-row">
          <span className="sup-l">Claude quota</span>
          <span className="sup-v">{fmt.pct1(h?.used_pct)}</span>
        </div>
        <div className="sup-row">
          <span className="sup-l">Codex quota</span>
          <span className="sup-v">{fmt.pct1(weekly?.current.current_percent)}</span>
        </div>
        <div className="sup-row">
          <span className="sup-l">Providers</span>
          <span className="sup-v">Claude · Codex</span>
        </div>
      </div>
    </>
  );
}

// ---- Canonical provider hero (Claude is the structure reference) -------

function CanonicalHero({
  weekLabel,
  usedPct,
  fiveHourPct,
  resetInSec,
  spentUsd,
  dollarPerPct,
  forecastPct,
  vsLastWeekDelta,
  ctx,
  verdict,
  heroLabel,
  freshness,
  showFiveHour,
  unavailableReason = null,
}: {
  weekLabel: string | null | undefined;
  usedPct: number | null | undefined;
  fiveHourPct: number | null | undefined;
  resetInSec: number | null | undefined;
  spentUsd: number | null | undefined;
  dollarPerPct: number | null | undefined;
  forecastPct: number | null | undefined;
  vsLastWeekDelta: number | null | undefined;
  ctx: FmtCtx;
  verdict: ReturnType<typeof resolveVerdict>;
  heroLabel: string;
  freshness: FreshnessEnvelope | null;
  showFiveHour: boolean;
  unavailableReason?: string | null;
}) {
  return (
    <>
      <div className="hero-zone hero-usage">
        <div className="hu-block">
          <div className="hu-label">
            WEEK USAGE
            {weekLabel ? <span className="hu-week"> · {weekLabel}</span> : null}
          </div>
          <div className="hu-num">{fmt.pct1(usedPct)}</div>
        </div>
        {showFiveHour && (
          <div className="hu-block" data-testid="hero-five-hour">
            <div className="hu-label">5-HOUR</div>
            <div className="hu-num hu-num--sm">{fmt.pct0(fiveHourPct)}</div>
          </div>
        )}
        <div className="hu-reset">
          resets in <span>{fmt.ddhh(resetInSec)}</span>
        </div>
      </div>

      <div className="hero-zone hero-spent" title={unavailableReason ?? undefined}>
        <div className="hs-label">SPENT THIS WEEK</div>
        <div className="hs-big">{fmt.usd0(spentUsd)}</div>
        <div className="hs-sub">
          <span>{fmt.usd2(dollarPerPct)}</span> / 1% used
        </div>
      </div>

      <div className="hero-zone hero-support">
        <div className="sup-row">
          <span className="sup-l">Forecast @ reset</span>
          <span className={`sup-v${verdict ? ` is-${verdict.cls}` : ''}`}>
            {fmt.pct0(forecastPct)}
          </span>
        </div>
        {(() => {
          const d = vsLastWeekDelta;
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
