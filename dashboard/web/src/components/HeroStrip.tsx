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
import { resolveSourceView } from '../store/sourceView';
import { dispatch, getState, subscribeStore } from '../store/store';
import type { AllSourceData, CodexSourceData, Envelope, SourceWarning } from '../types/envelope';

// HeroStrip (#264 S1, spec §4; #294 S5 §6.1) — the dashboard's full-width
// at-a-glance hero. Source-aware: Claude keeps its subscription-week hero
// (usage %, 5h, spent, $/1%, forecast — UNCHANGED); Codex renders provider-
// native tiles (cost + the five token counters + native quota windows + the
// calendar-period budget verdict — NO $/1%, NO subscription-week language);
// All shows the combined tiles (exactly {cost_usd, total_tokens}) or an explicit
// combined-unavailable state, with quota always side-by-side per provider.
// Mounted only on the dashboard branch of App.tsx.

export function HeroStrip() {
  const env = useSnapshot();
  const activeSource = useSyncExternalStore(subscribeStore, () => getState().activeSource);
  const h = env?.header;
  const cw = env?.current_week ?? null;
  const freshness = cw?.freshness ?? null;
  const display = useDisplayTz();
  const ctx: FmtCtx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  const verdict = resolveVerdict(h?.forecast_verdict ?? null);
  const heroLabel = heroFreshnessLabel(freshness?.age_seconds);

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

  // The Claude hero opens the (Claude) Current Week modal; the Codex/All heroes
  // have no equivalent modal in S5, so the region stays a focusable summary
  // without an activation.
  const activate = activeSource === 'claude' ? openCurrentWeek : undefined;

  let body: React.ReactNode;
  if (activeSource === 'codex') {
    const view = resolveSourceView(env, 'codex');
    body = <CodexHero data={view.entry?.data as CodexSourceData | undefined} />;
  } else if (activeSource === 'all') {
    const view = resolveSourceView(env, 'all');
    body = <AllHero env={env} data={view.entry?.data as AllSourceData | undefined} warnings={view.entry?.warnings} />;
  } else {
    body = (
      <ClaudeHero
        h={h}
        cw={cw}
        ctx={ctx}
        verdict={verdict}
        heroLabel={heroLabel}
        freshness={freshness}
      />
    );
  }

  return (
    <section
      ref={heroRef}
      className="hero-strip"
      role="region"
      tabIndex={0}
      aria-label="Week usage summary"
      data-hero-strip=""
      data-source={activeSource}
      onClick={activate ? cardRegionClick(activate) : undefined}
      onKeyDown={(e) => {
        if (!activate) return;
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

// ---- Codex hero (provider-native tiles, §6.1) -------------------------

function CodexHero({ data }: { data: CodexSourceData | undefined }) {
  if (data?.hero == null) {
    return <div className="hero-zone hero-empty" data-testid="codex-hero-empty">No Codex activity yet.</div>;
  }
  const hero = data.hero;
  const windows = data.quota ? joinCodexQuotaLabels(hero, data.quota) : [];
  const budget = hero.budget;
  return (
    <>
      <div className="hero-zone hero-spent" data-testid="codex-hero-spent">
        <div className="hs-label">CODEX SPEND</div>
        <div className="hs-big">{fmt.usd2(hero.cost_usd)}</div>
        <div className="hs-sub">
          <span>{fmt.tokens(hero.total_tokens)}</span> total tokens
        </div>
      </div>

      <div className="hero-zone hero-tokens" data-testid="codex-hero-tokens">
        <div className="hs-label">TOKENS</div>
        <ul className="codex-token-list">
          <li><span className="ctl-k">input</span> <span className="ctl-v">{fmt.tokens(hero.input_tokens)}</span></li>
          <li><span className="ctl-k">cached input</span> <span className="ctl-v">{fmt.tokens(hero.cached_input_tokens)}</span></li>
          <li><span className="ctl-k">output</span> <span className="ctl-v">{fmt.tokens(hero.output_tokens)}</span></li>
          <li><span className="ctl-k">reasoning</span> <span className="ctl-v">{fmt.tokens(hero.reasoning_output_tokens)}</span></li>
        </ul>
      </div>

      <div className="hero-zone hero-support" data-testid="codex-hero-support">
        {windows.length > 0 ? (
          windows.map((w) => (
            <div className="sup-row" key={w.key} data-quota-window={w.key}>
              <span className="sup-l">{w.label}</span>
              <span className="sup-v">{fmt.pct0(w.current.current_percent)}</span>
            </div>
          ))
        ) : (
          <div className="sup-row"><span className="sup-l">Quota</span><span className="sup-v">—</span></div>
        )}
        {budget != null && (
          <div className="sup-row" data-metric="codex-budget">
            <span className="sup-l">Budget</span>
            <span className={`sup-v is-${budget.verdict}`}>
              {fmt.pct0(budget.consumption_pct)} of {fmt.usd0(budget.budget_usd)}
            </span>
          </div>
        )}
      </div>
    </>
  );
}

// ---- All hero (combined tiles / combined-unavailable, §6.1) -----------

function combinedUnavailableCopy(warnings: SourceWarning[] | undefined): string {
  if (warnings != null && warnings.length > 0) return warnings[0].message;
  return 'Combined totals are unavailable while a provider is degraded.';
}

function AllHero({
  env,
  data,
  warnings,
}: {
  env: Envelope | null;
  data: AllSourceData | undefined;
  warnings: SourceWarning[] | undefined;
}) {
  const combined = data?.combined ?? null;
  // Provider-native quota chips side by side (never a merged gauge): Claude 7d,
  // Codex latest window.
  const claudeUsed = env?.header?.used_pct ?? null;
  const codexData = data?.providers?.codex ?? null;
  const codexWindows = codexData?.hero && codexData?.quota
    ? joinCodexQuotaLabels(codexData.hero, codexData.quota)
    : [];
  return (
    <>
      <div className="hero-zone hero-spent" data-testid="all-hero-combined">
        <div className="hs-label">COMBINED SPEND</div>
        {combined != null ? (
          <>
            <div className="hs-big">{fmt.usd2(combined.cost_usd)}</div>
            <div className="hs-sub">
              <span>{fmt.tokens(combined.total_tokens)}</span> total tokens
            </div>
          </>
        ) : (
          <div className="hs-sub combined-unavailable" data-testid="combined-unavailable">
            {combinedUnavailableCopy(warnings)}
          </div>
        )}
      </div>

      <div className="hero-zone hero-support" data-testid="all-hero-quota">
        <div className="sup-row">
          <span className="sup-l">Claude 7d</span>
          <span className="sup-v">{fmt.pct1(claudeUsed)}</span>
        </div>
        {codexWindows.length > 0 ? (
          codexWindows.map((w) => (
            <div className="sup-row" key={w.key} data-quota-window={w.key}>
              <span className="sup-l">Codex {w.label}</span>
              <span className="sup-v">{fmt.pct0(w.current.current_percent)}</span>
            </div>
          ))
        ) : (
          <div className="sup-row"><span className="sup-l">Codex quota</span><span className="sup-v">—</span></div>
        )}
      </div>
    </>
  );
}
