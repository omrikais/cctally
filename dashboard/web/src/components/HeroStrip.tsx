import { useEffect, useRef, useSyncExternalStore } from 'react';
import { useScopedSnapshot } from '../hooks/useScopedSnapshot';
import { useIsMobile } from '../hooks/useIsMobile';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { fmt, spendWindowLabel, type FmtCtx } from '../lib/fmt';
import { resolveVerdict } from '../lib/verdict';
import { humanizeAge } from '../lib/syncFreshness';
import { heroFreshnessLabel } from '../lib/heroFreshness';
import { cardRegionClick } from '../lib/cardRegion';
import { joinCodexQuotaLabels } from '../lib/sourceRows';
import { combinedHeading, combinedPresentation, warningForDomain } from '../lib/sourceGating';
import { resolveSourceView } from '../store/sourceView';
import { useAccountScope } from '../hooks/useScopedSnapshot';
import { sourceAccounts } from '../store/accountFocus';
import { AccountHeroCards } from './AccountHeroCards';
import { dispatch, getState, subscribeStore } from '../store/store';
import type {
  AccountCard,
  AllCombinedLeg,
  AllCombinedPeriod,
  AllSourceData,
  CodexSourceData,
  DashboardSelection,
  Envelope,
  FreshnessEnvelope,
  SourceEntry,
  SourceName,
} from '../types/envelope';

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

// The spend zone's default spoken period claim. Named because the zone's
// `aria-label` compares against it to decide whether the label has to be
// announced on its own (#564 review P2).
const DEFAULT_SPENT_LABEL_SPOKEN = 'Spent this week';

// #564 — the decorated headline is the SUM of the cards beneath it, and a card
// with no live cycle is totalled over one native cycle width ending now rather
// than over the whole accounting range. The aggregate hero shows that sum with
// no date range of its own, so it states the fallback period here. Read from the
// published `spendWindow`, never inferred from a null `resetsAt`; the period is
// derived from the bounds so a clamped window names its true span. Every
// fallback card carries the same server-computed window, so the first one found
// describes them all.
export function codexSpendWindowNote(
  accounts: readonly AccountCard[] | null | undefined,
): string | null {
  const window = accounts?.find((card) => card.spendWindow != null)?.spendWindow;
  if (window == null) return null;
  return 'Includes accounts with no live cycle, counted over the '
    + `${spendWindowLabel(window)}.`;
}

// #564 ui-qa P2 — the mobile shorthand for the note above. The full sentence
// wrapped to five lines at 375px and nine at 320px, adding 160px of hero to
// qualify a 34px figure — worse than the ingest-backlog case the #459 shorthand
// mechanism was built for, and shipping with no shorthand of its own. The
// period claim is what must survive the narrowing, so it is what the short form
// keeps; the period is still DERIVED from the published bounds, never a fixed
// "7 days", so a clamped window shortens both forms together.
export function codexSpendWindowCompactNote(
  accounts: readonly AccountCard[] | null | undefined,
): string | null {
  const window = accounts?.find((card) => card.spendWindow != null)?.spendWindow;
  if (window == null) return null;
  return `Incl. no-cycle accounts · ${spendWindowLabel(window)}`;
}

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

// Per-source region names (§5). Each selection says which cycle it summarizes,
// so a screen-reader user landing on the region learns that before the numbers.
const HERO_REGION_NAME: Record<DashboardSelection, string> = {
  all: 'Combined usage summary',
  claude: 'Claude week usage summary',
  codex: 'Codex cycle usage summary',
};

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
      aria-label={HERO_REGION_NAME[activeSource]}
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
      {/* #556 S1 §5 — criterion 10 cannot be verified by screenshot, so the
          region carries a real heading as well as an accessible name. Visually
          hidden: nothing about the hero's appearance changes. */}
      <h2 className="sr-only">{HERO_REGION_NAME[activeSource]}</h2>
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
  // #556 S5 §5.4 — one scope PER PROVIDER. Under All the view and the provider
  // are no longer the same thing, and the two providers carry independent
  // focus slots, so a single view-shaped scope cannot answer for both.
  const claudeScope = useAccountScope(source, 'claude');
  const codexScope = useAccountScope(source, 'codex');
  // Every countdown on this hero is measured from the SERVER's snapshot
  // instant, never from the browser clock. The Claude tab already prints a
  // server-computed `current_week.reset_in_sec`, so a skewed client clock used
  // to make the same Claude cycle read differently on the All tab than on the
  // Claude tab. `generated_at` is refreshed on every tick, so a countdown
  // anchored to it ages exactly the way `reset_in_sec` does. `Date.now()`
  // remains the fallback for an envelope that carries no instant.
  const nowMs = snapshotNowMs(env);
  if (source === 'claude') {
    const claudeEntry = resolveSourceView(env, 'claude').entry;
    const accounts = sourceAccounts(claudeEntry);
    const focusedCard = claudeScope.requestedKey == null
      ? null
      : accounts?.find((card) => card.accountKey === claudeScope.requestedKey) ?? null;
    const perAccount = accounts != null && focusedCard == null;
    const focusedResetInSec = remainingSeconds(focusedCard?.resetsAt, nowMs);
    const mergedSpendUsd = accounts?.reduce((sum, card) => sum + card.spendUsd, 0) ?? null;
    return (
      <CanonicalHero
        weekLabel={accounts == null ? h?.week_label : null}
        // #556 S5 round-2 QA P1 — a FOCUSED card answers for every slot it owns,
        // including when its answer is "no figure". `??` made a null per-account
        // percent fall through to the merged `header.used_pct`, which published
        // the whole provider's weekly consumption under one account's chip and
        // printed the same number for every account. Claude's per-account quota
        // evidence is far sparser than its accounting, so that null is the
        // COMMON case, not an edge one. Each slot switches on card PRESENCE.
        usedPct={focusedCard != null ? focusedCard.weeklyPercent : perAccount ? null : h?.used_pct}
        fiveHourPct={focusedCard != null
          ? focusedCard.fiveHourPercent
          : perAccount ? null : h?.five_hour_pct}
        resetInSec={focusedCard != null ? focusedResetInSec : perAccount ? null : cw?.reset_in_sec}
        spentUsd={focusedCard != null
          ? focusedCard.spendUsd
          : perAccount ? mergedSpendUsd : cw?.spent_usd}
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
        withheldUsedPct={focusedCard != null && focusedCard.weeklyPercent == null}
      />
    );
  }
  const codexEntry = resolveSourceView(env, 'codex').entry;
  const codex = codexEntry?.data as CodexSourceData | undefined;
  const cycle = codex?.hero.cycle;
  const codexDecorated = sourceAccounts(codexEntry) != null;
  // #556 S5 §5.8 — a Codex focus under All UN-BLANKS the provider-native quota
  // surfaces and reads the scoped child. `env` is the scoped envelope, so
  // `codex` is already that child's composition; the only thing that changes
  // here is the gate. Combined SPEND stays withheld through the unchanged
  // `combinedPresentation` predicate and is never recomputed from a child.
  const suppressAccountBlindQuota = (
    source === 'codex' && codexScope.scopesSupported && codexScope.accountKey == null
  ) || (source === 'all' && codexDecorated && codexScope.accountKey == null);
  const quota = suppressAccountBlindQuota
    ? null
    : codexScope.accountKey != null
      ? codexScope.scope?.quota ?? null
      : codex?.quota ?? null;
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
  const resetSeconds = remainingSeconds(weekly?.current.resets_at, nowMs);
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
    const perAccount = codexScope.scopesSupported && codexScope.accountKey == null;
    // #564 — the two states disclose the fallback period differently. The
    // aggregate figure is the sum of every card, so it takes a note whenever ANY
    // published card was totalled over the bounded window. Under focus the
    // figure is that one account's card, so the disclosure belongs in the spend
    // label, which otherwise reads a flat "SPENT THIS WEEK" over a total that
    // does not cover a week.
    const spendWindowNote = perAccount
      ? codexSpendWindowNote(sourceAccounts(codexEntry))
      : null;
    const spendWindowCompactNote = perAccount
      ? codexSpendWindowCompactNote(sourceAccounts(codexEntry))
      : null;
    const focusedSpendWindow = perAccount ? null : codexScope.card?.spendWindow ?? null;
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
          spendWindowNote,
          codexBacklogNote,
        )}
        spentNoteLabel={joinHeroNotes(spendWindowNote, codexBacklogLabel)}
        // #564 ui-qa P2 — the fallback note now has its own shorthand, so the
        // compact line is set whenever EITHER disclosure has one. It was
        // previously gated on the backlog shorthand alone, which left the
        // fallback sentence with no mobile form at all: the responsive swap
        // needs a compact sibling to swap TO, so it simply never engaged and the
        // full sentence rendered at every width. When both apply the mobile line
        // still carries both rather than dropping the period.
        spentNoteCompactLabel={
          spendWindowCompactNote == null && codexBacklogCompactLabel == null
            ? null
            : joinHeroNotes(
              spendWindowCompactNote ?? spendWindowNote,
              codexBacklogCompactLabel ?? codexBacklogLabel,
            )}
        spentLabel={focusedSpendWindow == null
          ? undefined
          : `SPENT · ${spendWindowLabel(focusedSpendWindow).toUpperCase()}`}
        spentLabelSpoken={focusedSpendWindow == null
          ? undefined
          : `Spent over the ${spendWindowLabel(focusedSpendWindow)}`}
        perAccountNote={perAccount ? 'per account' : null}
      />
    );
  }

  // #556 S1 §4.4 / §5 — the All hero reads the combined figure through ONE
  // predicate and through nothing else. It does not consult
  // `sourceDomainFreshness(allEntry, 'hero')`, `allEntry.freshness` or the
  // warning tuple: pairing any of those with a published number is what put
  // "Combined totals are unavailable" beside a figure that was on screen.
  const allEntry = resolveSourceView(env, 'all').entry as
    SourceEntry<AllSourceData> | undefined;
  const combined = combinedPresentation(allEntry ?? null);
  // Three states, not two. A figure is PUBLISHED, or it is WITHHELD for a named
  // reason, or the entry has not produced either yet — a hydrating All entry
  // reaches here with `data: null` and no warnings, so it has no figure AND no
  // reason. Treating "no figure" alone as withheld printed the withheld chip
  // and "A combined total is not published for this state." over a still-empty
  // bootstrap, where an honest blank is the whole answer.
  const published = combined.value != null;
  const withheldReason = published ? null : combined.unavailable;
  const claudeLeg = combined.legs?.claude ?? null;
  const codexLeg = combined.legs?.codex ?? null;
  // A WITHHELD figure is not "no data". Under decoration both providers have
  // accounting and the composition declines to sum it, pointing at the
  // per-account cards instead; telling the reader there is no data would be
  // false. `CURRENT CYCLES · NO DATA` is reserved for the published both-empty
  // state, where the answer really is that nothing was spent and nothing is
  // known.
  const heading = published
    ? combinedHeading(combined.contributors)
    : 'COMBINED · CURRENT CYCLES';
  // A resolved zero is DATA. Both legs empty is the one state that publishes a
  // number the reader must not see as observed spend, so the figure blanks
  // while the heading says `CURRENT CYCLES · NO DATA`. A zero inside a resolved
  // cycle keeps printing `$0`.
  const figure = published && combined.contributors.length > 0
    ? combined.value
    : null;

  // #416 QA — the COMBINED tab carries the same defect the Codex tab just shed,
  // one surface further out. `weekly` is joined off the PARENT hero, whose
  // `cycle` is `cycles_all[0]` — one representative account's window — so with
  // several Codex accounts this tab published one of them, unlabelled, as the
  // Codex 7-day percent, the countdown and the `Codex quota` row.
  //
  // D6 forbids blending independent allowances, and no summary statistic over
  // them (a max, a mean, "the most urgent") is the quantity the slot claims to
  // hold. So those slots blank and the per-account strip — which now renders on
  // this tab too — carries each account's own percent, 5h, reset and spend
  // directly beneath them. Nothing is lost by the blanks: the strip is strictly
  // more information than the one number it replaces.
  //
  // Spend and tokens are untouched. They are the only axes D6 lets All merge,
  // and COMBINED SPEND is this tab's headline — blanking it would be the
  // opposite failure. Gated on decoration, so a <=1-real-account install is
  // byte-identical (R8).
  //
  // #556 S5 §5.8/§5.9 — a focus under All un-blanks the focused provider's own
  // slots. Codex reads its scoped child; Claude substitutes the focused card's
  // weekly percent and reset, which is the SAME substitution the Claude tab
  // performs (§1.4) and is what the row's `hero and alerts only` qualifier
  // states. The panels stay unscoped for Claude, because Claude publishes no
  // `account_scopes`.
  const codexPerAccount = codexDecorated && codexScope.accountKey == null;
  const claudeEntry = resolveSourceView(env, 'claude').entry;
  const claudeAccounts = sourceAccounts(claudeEntry);
  const claudeFocusedCard = claudeScope.requestedKey == null
    ? null
    : claudeAccounts?.find((card) => card.accountKey === claudeScope.requestedKey) ?? null;
  const claudePerAccount = claudeAccounts != null && claudeFocusedCard == null;
  // #556 S5 round-2 QA P1 — the focused card answers, including when its answer
  // is "no figure". Falling through to `h.used_pct` published the PROVIDER-WIDE
  // weekly percentage as one account's own, so two accounts that had consumed
  // nothing measurable both showed the whole provider's number — while the leg
  // beside it said `no data` and the account's own card said `Weekly —`. This
  // is the headline layer of the rule `store/accountScope.ts` states for the
  // panels: under focus, never fall back to the parent. Claude's per-account
  // quota evidence is much sparser than its accounting, so a null weekly
  // percent is the common case rather than an edge one.
  const claudeHeadlinePct = claudeFocusedCard != null
    ? claudeFocusedCard.weeklyPercent
    : claudePerAccount ? null : h?.used_pct ?? null;
  // Decorated and without a figure is a DELIBERATE blank, so it is dimmed. The
  // undecorated case keeps its undimmed em-dash byte-for-byte (R8).
  const claudeHeadlineBlank = claudeAccounts != null && claudeHeadlinePct == null;

  // Each provider block carries its OWN labelled reset (§5, retiring A6).
  //
  // For a CONTRIBUTING leg the instant comes from THAT leg's published period
  // and from nowhere else, so the countdown and the heading always describe the
  // same cycle. A contributing leg that cannot name its cycle therefore shows
  // no reset line, which is the only thing rev5 suppresses about it — it still
  // counts toward the sum and is still named in the heading.
  //
  // Everywhere else the provider's OWN reset is the honest remaining answer:
  // the quota cycle did not stop existing because the two providers' spend will
  // not be summed. That covers the withheld figure (no legs at all) and equally
  // an `empty` leg, which names no cycle because the provider contributed no
  // accounting — not because its cycle is unknown. `One provider empty` is a
  // published row of the matrix, so without this an install with Codex quota
  // observations but no Codex accounting rows showed a percentage with no
  // countdown beneath it, where the previous hero showed one.
  const claudeResetSeconds = claudeFocusedCard != null
    ? remainingSeconds(claudeFocusedCard.resetsAt, nowMs)
    : claudeLeg?.state === 'current'
      ? periodResetSeconds(claudeLeg.period, nowMs)
      : cw?.reset_in_sec ?? null;
  const codexResetSeconds = codexLeg?.state === 'current'
    ? periodResetSeconds(codexLeg.period, nowMs)
    : resetSeconds;

  return (
    <>
      <div className="hero-zone hero-usage" data-testid="shared-hero-usage">
        <div className="hu-block" data-provider-block="claude">
          <div className="hu-label">
            CLAUDE · WEEK
            {claudePerAccount ? <span className="hu-week"> · per account</span> : null}
          </div>
          {/* A7 — the deliberate blank carries `.is-blank`, so it reads as an
              intentional absence rather than an unfinished load. */}
          <div className={claudeHeadlineBlank ? 'hu-num is-blank' : 'hu-num'}>
            {fmt.pct1(claudeHeadlinePct)}
          </div>
          <ProviderReset
            provider="claude"
            perAccount={claudePerAccount}
            resetInSec={claudeResetSeconds}
          />
        </div>
        <div className="hu-block" data-provider-block="codex">
          <div className="hu-label">
            CODEX · 7-DAY CYCLE
            {codexPerAccount ? <span className="hu-week"> · per account</span> : null}
          </div>
          {/* One precision across both blocks, and no `.hu-num--sm`: two
              quantities of the same kind, rendered the same size. */}
          <div className={codexPerAccount ? 'hu-num is-blank' : 'hu-num'}>
            {codexPerAccount ? '—' : fmt.pct1(weekly?.current.current_percent)}
          </div>
          <ProviderReset
            provider="codex"
            perAccount={codexPerAccount}
            resetInSec={codexResetSeconds}
          />
        </div>
      </div>

      <div
        className="hero-zone hero-spent"
        data-testid="shared-hero-spent"
        title={withheldReason?.message}
      >
        <div className="hs-label" data-testid="hero-combined-heading">{heading}</div>
        <div className={figure == null ? 'hs-big is-blank' : 'hs-big'}>
          {figure == null ? '—' : fmt.usd0(figure.costUsd)}
        </div>
        <div className="hs-sub">
          {withheldReason != null
            ? (
              <span
                className="panel-degraded-chip"
                data-testid="shared-hero-warning"
                title={withheldReason.message}
                aria-label={`Combined total withheld: ${withheldReason.message}`}
              >
                Combined withheld
              </span>
            )
            : figure != null
              ? <><span>{fmt.tokens(figure.totalTokens)}</span> total tokens</>
              : published
                ? (
                  <span data-testid="hero-combined-no-data">
                    no accounting in either current cycle
                  </span>
                )
                : null}
        </div>
        {withheldReason != null ? (
          <div className="hs-sub hero-combined-reason" data-testid="hero-combined-reason">
            {withheldReason.message}
          </div>
        ) : null}
        {combined.qualifications.length > 0 ? (
          <div className="hs-sub hero-combined-qualifications">
            {combined.qualifications.map((qualification) => (
              <span
                key={qualification.code + (qualification.provider ?? '')}
                className="chip hero-combined-qualification"
                data-testid="hero-combined-qualification"
                data-code={qualification.code}
                title={qualification.message}
                aria-label={qualification.message}
              >
                {qualificationChipLabel(qualification)}
              </span>
            ))}
          </div>
        ) : null}
      </div>

      <div className="hero-zone hero-support" data-testid="shared-hero-support">
        <div className="sup-row">
          <span className="sup-l">Claude · week to date</span>
          <span className="sup-v" data-testid="hero-leg-claude">
            <LegAmount leg={claudeLeg} perAccount={claudePerAccount} provider="claude" />
          </span>
        </div>
        <div className="sup-row">
          <span className="sup-l">Codex · cycle to date</span>
          <span className="sup-v" data-testid="hero-leg-codex">
            <LegAmount leg={codexLeg} perAccount={codexPerAccount} provider="codex" />
          </span>
        </div>
      </div>
    </>
  );
}

const PROVIDER_LABEL: Record<SourceName, string> = {
  claude: 'Claude',
  codex: 'Codex',
};

// The server's own snapshot instant, which is what every hero countdown is
// measured from. It is refreshed on each tick beside the values it bounds, so
// the pair always come from one clock; a browser whose clock is wrong can no
// longer make one tab's countdown disagree with another's. An envelope with no
// usable instant falls back to the browser clock, which is the previous
// behaviour and the best available answer.
function snapshotNowMs(env: Envelope | null): number {
  // `Date.parse('')` is NaN, so the absent and the unparseable instant take the
  // same branch without a separate null test.
  const parsed = Date.parse(env?.generated_at ?? '');
  return Number.isFinite(parsed) ? parsed : Date.now();
}

function remainingSeconds(
  instant: string | null | undefined, nowMs: number,
): number | null {
  if (instant == null) return null;
  const endMs = Date.parse(instant);
  return Number.isFinite(endMs) ? Math.max(0, (endMs - nowMs) / 1000) : null;
}

function periodResetSeconds(
  period: AllCombinedPeriod | undefined, nowMs: number,
): number | null {
  if (period == null) return null;
  const remaining = remainingSeconds(period.end_at, nowMs);
  // A leg whose named cycle has already ended AT THE SERVER'S OWN snapshot
  // instant has no countdown to print. `fmt.ddhh(0)` renders "resets in 0d 0h",
  // which reads as "resetting right now" rather than as evidence that the
  // published bounds are behind the data. Suppressing the line is what §5
  // already does for a contributing leg that cannot name its cycle at all.
  return remaining != null && remaining > 0 ? remaining : null;
}

// The per-provider labelled reset. Naming the provider is the whole point: the
// single unlabelled countdown this replaces was A6, and a reader could not tell
// which of the two cycles it belonged to.
function ProviderReset({
  provider,
  perAccount,
  resetInSec,
}: {
  provider: SourceName;
  perAccount: boolean;
  resetInSec: number | null;
}) {
  const label = PROVIDER_LABEL[provider];
  if (perAccount) {
    return (
      <div
        className="hu-reset hu-reset--per-account"
        data-testid={`hero-${provider}-reset`}
        title={`Each ${label} account has its own quota cycle — independent resets are never blended.`}
      >
        {label} reset <span>per account</span>
      </div>
    );
  }
  if (resetInSec == null) return null;
  return (
    <div className="hu-reset" data-testid={`hero-${provider}-reset`}>
      {label} resets in <span>{fmt.ddhh(resetInSec)}</span>
    </div>
  );
}

// One provider's contribution to the figure beside it. `no data` is the honest
// reading for both an absent leg and an `empty` one: neither adds spend.
function LegAmount({
  leg,
  perAccount,
  provider,
}: {
  leg: AllCombinedLeg | null;
  perAccount: boolean;
  provider: SourceName;
}) {
  if (perAccount) {
    return (
      <span
        className="hero-per-account-value"
        data-testid="hero-per-account-value"
        title={`Each ${PROVIDER_LABEL[provider]} account has its own cycle — independent spend is shown per account below.`}
      >
        per account
      </span>
    );
  }
  if (leg == null || leg.state === 'empty') {
    return <span className="hero-leg-no-data">no data</span>;
  }
  return <>{fmt.usd2(leg.cost_usd)}</>;
}

// Qualifications arrive as `{code, message}`. The chip is the short sighted
// form; the full sentence stays in `title` and the accessible name.
function qualificationChipLabel(
  qualification: { code: string; provider?: SourceName },
): string {
  if (qualification.code === 'codex_ingest_backlog') return 'Codex still loading';
  if (qualification.code === 'provider_empty') {
    return `${qualification.provider ? PROVIDER_LABEL[qualification.provider] : 'A provider'} no data`;
  }
  return 'Qualified';
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
  spentLabel = 'SPENT THIS WEEK',
  spentLabelSpoken = DEFAULT_SPENT_LABEL_SPOKEN,
  perAccountNote = null,
  withheldUsedPct = false,
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
  // touch; a disclosure nobody can see does not disclose. Set for the
  // store-wide ingest backlog and, since #564, for the fallback-window note:
  // the merged figure is the only period claim the aggregate hero makes, so a
  // tooltip-only form would leave that claim unqualified for touch users. The
  // account-scoped stale-cycle note keeps its #350 tooltip-only disposition.
  spentNoteLabel?: string | null;
  // #459 — mobile-only shorthand for the same disclosure. The full sentence
  // remains in `spentNote`, so responsive sighted copy never weakens the
  // accessible explanation of what is loading or how totals will change.
  spentNoteCompactLabel?: string | null;
  // #564 — the spend zone's own period claim. It defaults to the week because
  // that is what a cycle-bounded hero covers; a focused account whose card was
  // totalled over the bounded fallback overrides it with the window it actually
  // covers. Two props because the visible label is upper case, which several
  // screen readers spell out letter by letter, so the spoken form is supplied
  // separately rather than derived from the display string.
  spentLabel?: string;
  spentLabelSpoken?: string;
  // #416 D6 — set when the headline percentage/reset are deliberately BLANK
  // because each account owns an independent quota cycle. Replaces the reset
  // countdown AND every other deliberately-blank slot with a pointer to the
  // per-account cards; it is never a failure state.
  perAccountNote?: string | null;
  // #556 S5 round-2 QA P1 — set when ONE account is focused and publishes no
  // weekly percentage. The blank is deliberate, so it takes the same dimmed
  // presentation the per-account blank uses, but it must not also rewrite the
  // reset line and the three other slots the way `perAccountNote` does: those
  // slots have their own focused answers.
  withheldUsedPct?: boolean;
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
            className={`hu-num${
              usedPct == null && (perAccountNote != null || withheldUsedPct) ? ' is-blank' : ''
            }`}
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
        // #564 review P2: the note is what USED to gate this, which left the
        // spoken label unreachable in the one state it was written for. A
        // focused fallback account overrides `spentLabelSpoken` but carries no
        // note (`spendWindowNote` is aggregate-only), so assistive tech fell
        // back to reading the raw upper-case `SPENT · LAST 7 DAYS`. Whenever
        // the zone makes a period claim other than the default, the label is
        // announced whether or not a note accompanies it.
        aria-label={((): string | undefined => {
          const note = unavailableReason ?? spentNote;
          if (note) return `${spentLabelSpoken}. ${note}`;
          return spentLabelSpoken === DEFAULT_SPENT_LABEL_SPOKEN
            ? undefined
            : spentLabelSpoken;
        })()}
      >
        <div className="hs-label">{spentLabel}</div>
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
            {/* #564 ui-qa P2 — the dim style comes from `hero-spent-note-text`,
                which every note carries; the responsive `-full` class is added
                only when a compact sibling exists to swap with. Before #564 the
                two were always both present, so one class carried both jobs and
                a note without a compact form fell through to the bright
                `.hs-sub span` metric treatment. */}
            <span className={spentNoteCompactLabel == null
              ? 'hero-spent-note-text'
              : 'hero-spent-note-text hero-ingest-backlog-label-full'}>{spentNoteLabel}</span>
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
