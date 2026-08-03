import { useEffect, useRef, useSyncExternalStore } from 'react';
import { useScopedSnapshot } from '../hooks/useScopedSnapshot';
import { useIsMobile } from '../hooks/useIsMobile';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { fmt, type FmtCtx } from '../lib/fmt';
import { resolveVerdict } from '../lib/verdict';
import { humanizeAge } from '../lib/syncFreshness';
import { heroFreshnessLabel } from '../lib/heroFreshness';
import { cardRegionClick } from '../lib/cardRegion';
import { joinCodexQuotaLabels } from '../lib/sourceRows';
import { sourceDomainFreshness, warningForDomain } from '../lib/sourceGating';
import { resolveSourceView } from '../store/sourceView';
import { useAccountScope } from '../hooks/useScopedSnapshot';
import { sourceAccounts } from '../store/accountFocus';
import { AccountHeroCards } from './AccountHeroCards';
import { dispatch, getState, subscribeStore } from '../store/store';
import type { AllSourceData, CodexSourceData, Envelope, FreshnessEnvelope } from '../types/envelope';

// HeroStrip (#264 S1, spec §4; #294 S5 §6.1) — the dashboard's full-width
// at-a-glance hero. The shared three-zone component keeps Claude's canonical
// anatomy while its adapter supplies Codex native-cycle or All combined values.
// Independent provider quota percentages are always labelled and never summed.
// Mounted only on the dashboard branch of App.tsx.

// #350 — the single disclosure string for a Codex hero bounded by stale quota
// evidence. Shared with the current-week modal so both surfaces agree.
export const CODEX_STALE_CYCLE_NOTE =
  'Codex quota evidence is stale — this spend is current, but the forecast is paused '
  + 'until Codex reports again.';

// public #5 — the Codex hook's ingest leg is budgeted, so on a large or freshly
// upgraded store some rollout history has not been read yet. Disclosure ONLY:
// the numbers shown are correct for what IS ingested, and the backlog drains on
// its own. Deliberately not tied to `availability`/`freshness`, which many
// unrelated gates read.
export function codexIngestBacklogNote(
  backlog: { files: number } | null | undefined,
): string | null {
  if (!backlog || backlog.files <= 0) return null;
  const plural = backlog.files === 1 ? 'session' : 'sessions';
  return `Codex history is still loading (${backlog.files} ${plural} left) — `
    + 'totals will rise as it finishes.';
}

// The VISIBLE form of the same disclosure (QA P2). The sentence above only ever
// reached a `title` on a non-interactive div — hover-only, so unreachable on
// touch — which does not tell anyone their totals are incomplete. This is what
// the hero prints; the sentence stays on the zone's `title`/`aria-label`.
//
// Short on purpose: `hero-spent` is the narrowest desktop zone and half a phone
// width on mobile, and a wrapped three-line caveat is a heavier disclosure than
// a transient loading state deserves. The `+` is the whole message in one glyph
// — the number on screen is going to grow.
export function codexIngestBacklogLabel(
  backlog: { files: number } | null | undefined,
): string | null {
  if (!backlog || backlog.files <= 0) return null;
  const plural = backlog.files === 1 ? 'session' : 'sessions';
  return `+${backlog.files} ${plural} still loading`;
}

// #459 — at phone widths the spent zone narrows to roughly 132px (390px
// viewport) and 62px (320px viewport). The ordinary label wraps into two and
// four lines respectively. Keep the explanatory sentence in the zone's title
// and accessible name; this is only the responsive sighted shorthand.
export function codexIngestBacklogCompactLabel(
  backlog: { files: number } | null | undefined,
): string | null {
  if (!backlog || backlog.files <= 0) return null;
  return `+${backlog.files} more`;
}

// Two independent disclosures can apply at once; joining beats picking, because
// suppressing either one hides a real caveat about the number on screen.
export function joinHeroNotes(...notes: Array<string | null>): string | null {
  const kept = notes.filter((note): note is string => Boolean(note));
  return kept.length ? kept.join(' ') : null;
}

export function HeroStrip() {
  const env = useScopedSnapshot();
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
      {/* #341 Task 4 — per-account hero cards (Q6 Option A). Self-hides unless
          the active physical source is decorated (>1 real account). */}
      <AccountHeroCards />
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
  const scope = useAccountScope();
  if (source === 'claude') {
    const claudeEntry = resolveSourceView(env, 'claude').entry;
    const accounts = sourceAccounts(claudeEntry);
    const focusedCard = scope.requestedKey == null
      ? null
      : accounts?.find((card) => card.accountKey === scope.requestedKey) ?? null;
    const perAccount = accounts != null && focusedCard == null;
    const focusedResetInSec = focusedCard?.resetsAt == null
      ? null
      : Math.max(0, (Date.parse(focusedCard.resetsAt) - Date.now()) / 1000);
    const mergedSpendUsd = accounts?.reduce((sum, card) => sum + card.spendUsd, 0) ?? null;
    return (
      <CanonicalHero
        weekLabel={accounts == null ? h?.week_label : null}
        usedPct={focusedCard?.weeklyPercent ?? (perAccount ? null : h?.used_pct)}
        fiveHourPct={focusedCard?.fiveHourPercent ?? (perAccount ? null : h?.five_hour_pct)}
        resetInSec={focusedCard != null ? focusedResetInSec : perAccount ? null : cw?.reset_in_sec}
        spentUsd={focusedCard?.spendUsd ?? (perAccount ? mergedSpendUsd : cw?.spent_usd)}
        // The account wire does not emit this metric. Do not manufacture it
        // from rounded card values: the canonical merged value is a different
        // accounting scope and cannot be borrowed while focused.
        dollarPerPct={accounts == null ? h?.dollar_per_pct : null}
        forecastPct={accounts == null ? h?.forecast_pct : null}
        vsLastWeekDelta={accounts == null ? h?.vs_last_week_delta : null}
        freshness={accounts == null ? cw?.freshness ?? null : null}
        ctx={ctx}
        verdict={accounts == null ? verdict : null}
        heroLabel={heroLabel}
        showFiveHour={!perAccount && (focusedCard?.fiveHourPercent != null || accounts == null)}
        perAccountNote={perAccount ? 'per account' : null}
      />
    );
  }
  const codexEntry = resolveSourceView(env, 'codex').entry;
  const codex = codexEntry?.data as CodexSourceData | undefined;
  const cycle = codex?.hero.cycle;
  const codexDecorated = sourceAccounts(codexEntry) != null;
  const suppressAccountBlindQuota = (
    source === 'codex' && scope.scopesSupported && scope.accountKey == null
  ) || (source === 'all' && codexDecorated);
  const quota = suppressAccountBlindQuota
    ? null
    : scope.accountKey != null ? scope.scope?.quota ?? null : codex?.quota ?? null;
  const windows = codex?.hero && quota ? joinCodexQuotaLabels(codex.hero, quota) : [];
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
  // #350 — disclosure ONLY. Codex has no background quota poll, so a weekly
  // observation goes stale after an idle hour while the spend, tokens and cycle
  // bounds it bounds stay correct. This must never gate rendering: the values
  // are real, and `Forecast @ reset` already pauses through `forecast.status`.
  const codexCycleStale = codex?.hero?.cycle_freshness === 'stale';
  const codexBacklogNote = codexIngestBacklogNote(codex?.ingest_backlog);
  const codexBacklogLabel = codexIngestBacklogLabel(codex?.ingest_backlog);
  const codexBacklogCompactLabel = codexIngestBacklogCompactLabel(codex?.ingest_backlog);
  const warning = warningForDomain(codexEntry?.warnings, 'hero');
  const quotaForecast = quota?.histories.find((row) => row.key === weekly?.key)?.forecast;
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
    // #416 D6 — "All accounts" merges only SPEND and TOKENS. Independent quota
    // percentages and resets are never summed, averaged or stood in for by one
    // account's window: under All accounts the headline percent, reset, $/1%,
    // forecast and week label go blank and the per-account cards below carry
    // each account's own numbers. Focus a chip and the account's own values
    // return. Gated on decoration, so a single-account install is unchanged.
    //
    // The gate is DECORATION, not "more than one live cycle" (#416 QA P1-C).
    // The headline spend merges every account unconditionally, so letting a
    // lone surviving cycle's percentage through would publish a blended $/1%
    // by construction — merged spend over one account's percent — and would
    // silently change what the headline means as cycles expire.
    const perAccount = scope.scopesSupported && scope.accountKey == null;
    return (
      <CanonicalHero
        // #416 QA P1-C: `hero.cycle` is the REPRESENTATIVE account's window.
        // Printing it on the line that blanks the percentage *because each
        // account has its own cycle* contradicts itself in one glance.
        weekLabel={perAccount ? null : weekLabel}
        usedPct={perAccount ? null : usedPct}
        fiveHourPct={perAccount ? null : fiveHour?.current.current_percent}
        resetInSec={perAccount ? null : resetSeconds}
        spentUsd={spentUsd}
        dollarPerPct={perAccount ? null : dollarPerPct}
        forecastPct={perAccount ? null : forecastPct}
        vsLastWeekDelta={perAccount ? null : vsLastWeekDelta}
        freshness={perAccount ? null : codexFreshness}
        ctx={ctx}
        verdict={perAccount ? null : codexVerdict}
        heroLabel={codexHeroLabel}
        showFiveHour={!perAccount && fiveHour != null}
        unavailableReason={!perAccount && codexUnavailable
          ? warning?.message ?? 'Cycle accounting unavailable'
          : null}
        // The two disclosures have DIFFERENT scopes and cannot share one gate.
        // The stale-cycle note is account-scoped — under focus `focusedHero`
        // derives `cycle_freshness` from that child's own quota summary, and
        // under All accounts the forecast slot it talks about already reads
        // "per account" — so #416's D6 blanking keeps it. The ingest backlog is
        // a store-wide INGEST condition with no per-account meaning, and All
        // accounts is the LANDING view whose merged headline spend is exactly
        // the incomplete number it qualifies; suppressing it there hid the
        // caveat in the one view that most needed it (QA P1).
        spentNote={joinHeroNotes(
          perAccount || !codexCycleStale ? null : CODEX_STALE_CYCLE_NOTE,
          codexBacklogNote,
        )}
        spentNoteLabel={codexBacklogLabel}
        spentNoteCompactLabel={codexBacklogCompactLabel}
        perAccountNote={perAccount ? 'per account' : null}
      />
    );
  }

  const allEntry = resolveSourceView(env, 'all').entry;
  const all = allEntry?.data as AllSourceData | undefined;
  const combined = all?.combined ?? null;
  // #456 — Combined contains the same incomplete Codex spend/tokens as the
  // Codex hero while bounded ingest is catching up. The backlog condition is
  // store-wide, so crossing the provider boundary does not make the caveat
  // disappear. Keep it subordinate to the existing unavailable state: with no
  // combined number on screen there is nothing for the backlog to qualify.
  const combinedBacklogNote = combined == null ? null : codexBacklogNote;
  const combinedBacklogLabel = combined == null ? null : codexBacklogLabel;
  const combinedBacklogCompactLabel = combined == null ? null : codexBacklogCompactLabel;
  const allWarning = warningForDomain(allEntry?.warnings, 'hero');
  const allWarningDetail = allWarning?.message
    ?? 'Combined totals are unavailable while a provider is degraded.';
  const combinedStale = combined != null
    && allEntry != null
    && sourceDomainFreshness(allEntry, 'hero') === 'stale';
  const combinedStaleNote = combinedStale ? allWarningDetail : null;
  const combinedDisclosureNote = joinHeroNotes(combinedStaleNote, combinedBacklogNote);
  // #416 QA — the COMBINED tab carries the same defect the Codex tab just shed,
  // one surface further out. `weekly` is joined off the PARENT hero, whose
  // `cycle` is `cycles_all[0]` — one representative account's window — so with
  // several Codex accounts this tab published one of them, unlabelled, as the
  // Codex 7-day percent, the countdown and the `Codex quota` row.
  //
  // D6 forbids blending independent allowances, and no summary statistic over
  // them (a max, a mean, "the most urgent") is the quantity the slot claims to
  // hold. So the three slots blank and the per-account strip — which now renders
  // on this tab too — carries each account's own percent, 5h, reset and spend
  // directly beneath them. Nothing is lost by the blanks: the strip is strictly
  // more information than the one number it replaces.
  //
  // Spend and tokens are untouched. They are the only axes D6 lets All merge,
  // the merge already happens server-side (a sum of the same cards), and
  // COMBINED SPEND is this tab's headline — blanking it would be the opposite
  // failure. Gated on Codex decoration, so a <=1-real-account install is
  // byte-identical (R8).
  const codexPerAccount = codexDecorated;
  const claudeEntry = resolveSourceView(env, 'claude').entry;
  const claudePerAccount = sourceAccounts(claudeEntry) != null;
  const codexPerAccountValue = codexPerAccount ? (
    <span
      className="hero-per-account-value"
      data-testid="hero-per-account-value"
      title="Each Codex account has its own quota cycle — independent percentages are never blended."
    >
      per account
    </span>
  ) : null;

  return (
    <>
      <div className="hero-zone hero-usage" data-testid="shared-hero-usage">
        <div className="hu-block">
          <div className="hu-label">
            CLAUDE 7-DAY
            {claudePerAccount ? <span className="hu-week"> · per account</span> : null}
          </div>
          <div className="hu-num">{claudePerAccount ? '—' : fmt.pct1(h?.used_pct)}</div>
        </div>
        <div className="hu-block">
          <div className="hu-label">
            CODEX 7-DAY
            {codexPerAccount ? <span className="hu-week"> · per account</span> : null}
          </div>
          <div className="hu-num hu-num--sm">
            {codexPerAccount ? '—' : fmt.pct0(weekly?.current.current_percent)}
          </div>
        </div>
        {codexPerAccount ? (
          // This countdown has always been the CODEX reset (Claude's own reset
          // is not shown on this tab), so the replacement names Codex — dropping
          // the provider would read as though Claude's reset were per-account too.
          <div
            className="hu-reset hu-reset--per-account"
            data-testid="hero-per-account-note"
            title="Each Codex account has its own quota cycle — independent resets are never blended."
          >
            Codex usage + reset <span>per account</span>
          </div>
        ) : (
          <div className="hu-reset">resets in <span>{fmt.ddhh(resetSeconds)}</span></div>
        )}
      </div>

      <div
        className="hero-zone hero-spent"
        data-testid="shared-hero-spent"
        title={combinedDisclosureNote ?? undefined}
        role={combinedDisclosureNote == null ? undefined : 'group'}
        aria-label={combinedDisclosureNote == null
          ? undefined
          : `Combined spend. ${combinedDisclosureNote}`}
      >
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
        {combinedStaleNote != null ? (
          <div className="hs-sub">
            <span
              className="chip chip-stale"
              data-testid="shared-hero-stale-marker"
              title={combinedStaleNote}
              aria-label={combinedStaleNote}
            >
              Stale quota
            </span>
          </div>
        ) : null}
        {combinedBacklogLabel != null ? (
          <div className="hs-sub" data-testid="hero-spent-note" aria-hidden="true">
            <span className={combinedBacklogCompactLabel == null
              ? undefined : 'hero-ingest-backlog-label-full'}>{combinedBacklogLabel}</span>
            {combinedBacklogCompactLabel == null ? null : (
              <span className="hero-ingest-backlog-label-compact">
                {combinedBacklogCompactLabel}
              </span>
            )}
          </div>
        ) : null}
      </div>

      <div className="hero-zone hero-support" data-testid="shared-hero-support">
        <div className="sup-row">
          <span className="sup-l">Claude quota</span>
          <span className="sup-v">
            {claudePerAccount
              ? (
                <span
                  className="hero-per-account-value"
                  data-testid="hero-per-account-value"
                  title="Each Claude account has its own quota cycle — independent percentages are never blended."
                >
                  per account
                </span>
              )
              : fmt.pct1(h?.used_pct)}
          </span>
        </div>
        <div className="sup-row">
          <span className="sup-l">Codex quota</span>
          <span className="sup-v">
            {codexPerAccountValue ?? fmt.pct1(weekly?.current.current_percent)}
          </span>
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
  spentNote = null,
  spentNoteLabel = null,
  spentNoteCompactLabel = null,
  perAccountNote = null,
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
  // #350 — disclosure for a hero whose values ARE present but whose bounding
  // quota evidence is stale. Distinct from `unavailableReason`, which explains
  // an ABSENT hero; when both apply the unavailable reason wins.
  spentNote?: string | null;
  // public #5 QA P2 — the VISIBLE short form of `spentNote`, printed under the
  // `$/1%` sub-line. `spentNote` alone only ever reached a `title` on a
  // non-interactive div, which is hover-only and therefore unreachable on
  // touch; a disclosure nobody can see does not disclose. Set only for the
  // store-wide ingest backlog: the account-scoped stale-cycle note keeps its
  // #350 tooltip-only disposition, and widening that is a separate decision.
  spentNoteLabel?: string | null;
  // #459 — mobile-only shorthand for the same disclosure. The full sentence
  // remains in `spentNote`, so responsive sighted copy never weakens the
  // accessible explanation of what is loading or how totals will change.
  spentNoteCompactLabel?: string | null;
  // #416 D6 — set when the headline percentage/reset are deliberately BLANK
  // because each account owns an independent quota cycle. Replaces the reset
  // countdown AND every other deliberately-blank slot with a pointer to the
  // per-account cards; it is never a failure state.
  perAccountNote?: string | null;
}) {
  // #416 QA P2-D — a bare em-dash reads as missing data, not as a deliberate
  // blank. The reset slot already carried the caption; `Forecast @ reset`,
  // `$/1% vs last week` and `$/1% used` did not, so the QA gate's honest read
  // was that those three looked broken. One shared pointer, one vocabulary
  // (the italic `per account` span the reset slot already uses).
  const perAccountValue = perAccountNote == null ? null : (
    <span
      className="hero-per-account-value"
      data-testid="hero-per-account-value"
      title="Each account has its own quota cycle — independent percentages are never blended."
    >
      {perAccountNote}
    </span>
  );
  return (
    <>
      <div className="hero-zone hero-usage">
        <div className="hu-block">
          <div className="hu-label">
            WEEK USAGE
            {weekLabel ? <span className="hu-week"> · {weekLabel}</span> : null}
          </div>
          {/* #416 QA P3-C — a bare em-dash at KPI weight in full-brightness
              text colour reads as a loading skeleton, not as a deliberate
              blank. Every other slot on this hero carries the dimmed
              `per account` caption; this one carries the glyph alone, so it
              takes `--text-dim` to match. Only the deliberate blank is dimmed:
              a real percentage is untouched. */}
          <div
            className={`hu-num${perAccountNote != null && usedPct == null ? ' is-blank' : ''}`}
          >
            {fmt.pct1(usedPct)}
          </div>
        </div>
        {showFiveHour && (
          <div className="hu-block" data-testid="hero-five-hour">
            <div className="hu-label">5-HOUR</div>
            <div className="hu-num hu-num--sm">{fmt.pct0(fiveHourPct)}</div>
          </div>
        )}
        {perAccountNote == null ? (
          <div className="hu-reset">
            resets in <span>{fmt.ddhh(resetInSec)}</span>
          </div>
        ) : (
          <div
            className="hu-reset hu-reset--per-account"
            data-testid="hero-per-account-note"
            title="Each account has its own quota cycle — independent percentages are never blended."
          >
            usage + reset <span>{perAccountNote}</span>
          </div>
        )}
      </div>

      <div
        className="hero-zone hero-spent"
        title={unavailableReason ?? spentNote ?? undefined}
        // public #5 QA P2: `aria-label` is NOT honoured on the implicit
        // `generic` role a bare `<div>` carries, so the label below was never
        // the reliable channel the #350 fix assumed — and the visible note is
        // `aria-hidden`, which left BOTH channels unreliable. `role="group"` is
        // the minimal container role that supports an accessible name, so the
        // label is announced on entry and the visible line can stay hidden
        // rather than saying the same sentence twice. It matters most for
        // `unavailableReason`, which has no visible text at all.
        role="group"
        // #350 QA [P2]: a `title` on a non-interactive div is hover-only — it is
        // unreachable on touch and not reliably announced by screen readers. The
        // visual design stays as chosen (spec §3.8, no new hero visual), but the
        // reason must at least reach assistive tech.
        aria-label={
          unavailableReason ?? spentNote
            ? `Spent this week. ${unavailableReason ?? spentNote}`
            : undefined
        }
      >
        <div className="hs-label">SPENT THIS WEEK</div>
        <div className="hs-big">{fmt.usd0(spentUsd)}</div>
        <div className="hs-sub">
          {perAccountValue == null
            ? <><span>{fmt.usd2(dollarPerPct)}</span> / 1% used</>
            : <>$ / 1% used {perAccountValue}</>}
        </div>
        {/* public #5 QA P2 — a second `hs-sub` line: the zone's own dim
            `--fs-meta` / `--text-dim` vocabulary. The #459 responsive spans
            explicitly inherit that dim style, so the line stays subordinate
            to the bright `$/1%` metric above it. `aria-hidden` because the
            zone's `aria-label` already reads the full sentence — announcing
            both would say it twice. Suppressed while `unavailableReason` is
            showing, so the visible line and the tooltip never disagree about
            which note wins. Omitted entirely when null: no empty element, no
            stray separator. */}
        {unavailableReason == null && spentNoteLabel != null ? (
          <div className="hs-sub" data-testid="hero-spent-note" aria-hidden="true">
            <span className={spentNoteCompactLabel == null
              ? undefined : 'hero-ingest-backlog-label-full'}>{spentNoteLabel}</span>
            {spentNoteCompactLabel == null ? null : (
              <span className="hero-ingest-backlog-label-compact">
                {spentNoteCompactLabel}
              </span>
            )}
          </div>
        ) : null}
      </div>

      <div className="hero-zone hero-support">
        <div className="sup-row">
          <span className="sup-l">Forecast @ reset</span>
          <span className={`sup-v${verdict ? ` is-${verdict.cls}` : ''}`}>
            {perAccountValue ?? fmt.pct0(forecastPct)}
          </span>
        </div>
        {(() => {
          const d = vsLastWeekDelta;
          if (d == null) {
            return (
              <div className="sup-row" data-metric="vs-last-week">
                <span className="sup-l">$/1% vs last week</span>
                <span className="sup-v">{perAccountValue ?? '—'}</span>
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
              // #350 QA [P3]: this is the one element a desktop user hovers to
              // interrogate the red warning glyph, so it must say WHY it is red —
              // the visible text is only an age and never the word "stale".
              title={
                `Captured ${fmt.datetimeShort(freshness.captured_at, ctx)}`
                + (heroLabel === 'stale' ? ' · stale' : '')
              }
            >
              {/* #350 QA [P2]: name the state, don't only tint it. A red glyph
                  plus a colour shift is the ENTIRE visible delta from fresh
                  otherwise, which a colourblind or touch user cannot read. */}
              {heroLabel === 'stale' ? '⚠ stale · ' : ''}
              {humanizeAge(freshness.age_seconds)}
            </span>
          </div>
        )}
      </div>
    </>
  );
}
