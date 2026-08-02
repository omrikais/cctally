// CacheReportPanel — anomaly watchdog for the dashboard.
// Spec 2026-05-21 §2.
//
// Visual states (calm-when-healthy / loud-when-anomalous):
//
//   Healthy:               accent-teal border, ✓ glyph, today's % +
//                          14d median compare, sparkline + mini
//                          net-bars rendered, "14d net: +$X.XX" subline.
//   Anomalous:             accent-amber border, ⚠ glyph, worst-trigger
//                          headline, sparkline rendered with amber
//                          today-marker, mini net-bars rendered.
//   Insufficient baseline: accent-teal border, ~ glyph, "Building
//                          baseline N/5 days" — sparkline + bars omitted.
//   Empty (no activity):   accent-teal border, − glyph, "No Claude
//                          activity yet".
//
// Mini net-bars sit under the sparkline to fill the panel body.
// Single-direction version of CacheNetBars (green=positive net day,
// amber=negative), scaled to max |net| so trends are visible at 28 px
// tall. The 14d-net subline below them is the panel's headline
// summary number; the ⚠-days count is surfaced in the modal table
// rather than competing for panel space.
//
// Click anywhere on the panel body dispatches OPEN_MODAL kind:
// 'cache-report'. The PanelGrip touch handle inside the header is
// drag-only (managed by dnd-kit at the panel-host level), so it does
// not steal the click; the panel is wrapped by PanelHost upstream which
// installs the dnd-kit pointer listeners on the surrounding element.
//
// No ShareIcon in v1 — cache-report is not in SHARE_CAPABLE_PANELS
// (spec §2.6).
import { useAccountScope, useScopedSnapshot } from '../hooks/useScopedSnapshot';
import { useSyncExternalStore } from 'react';
import { useIsMobile } from '../hooks/useIsMobile';
import { dispatch, getState, subscribeStore } from '../store/store';
import { PanelGrip } from '../components/PanelGrip';
import { PanelSkeleton } from '../components/PanelSkeleton';
import { ExpandButton } from '../components/ExpandButton';
import { CacheSparkline } from '../modals/CacheSparkline';
import { CacheNetBars } from '../modals/CacheNetBars';
import { cardRegionClick } from '../lib/cardRegion';
import { fmt } from '../lib/fmt';
import { CACHE_REPORT_MIN_BASELINE_DAYS } from '../lib/cache-report-constants';
import { cacheReportVerdict } from '../lib/cacheReportVerdict';
import { cachePercentText, cacheVocabulary } from '../lib/cacheReportVocabulary';
import {
  presentationCacheReportComposition,
  presentationProviders,
  type ProviderPresentationSection,
} from '../lib/dashboardPresentation';
import type { CacheReportEnvelope } from '../types/envelope';
import { SourceChip } from './sourcePanel';

const TEAL = 'var(--accent-teal)';
const AMBER = 'var(--accent-amber)';
const GREEN = 'var(--accent-green)';

function CacheProviderSummary({
  section,
}: {
  section: ProviderPresentationSection<CacheReportEnvelope>;
}) {
  const report = section.value;
  // Each summary renders in its OWN provider's vocabulary (#443 S2 §4.4).
  const vocab = cacheVocabulary(section.source);
  const windowNetUsd = report?.days.reduce((sum, day) => sum + day.net_usd, 0) ?? 0;
  // #443 F6 — this summary applied no insufficient gate, so it read "anomaly"
  // for data the panel beside it called "building baseline". Both now call the
  // same helper, which resolves the contradiction as a consequence.
  const verdict = report == null ? null : cacheReportVerdict(report);
  return (
    <div
      className="source-provider-section provider-summary-card cache-provider-summary"
      data-provider-section={section.source}
      aria-label={`${section.label} cache report`}
    >
      <div className="source-provider-head">
        <SourceChip source={section.source} />
        {section.status !== 'available' && (
          <span className="provider-section-status">{section.status}</span>
        )}
      </div>
      {report == null ? (
        <div className="provider-section-reason">{section.reason}</div>
      ) : (
        <>
          <div className="provider-summary-kpis">
            <div>
              <span className="provider-summary-label">{vocab.percentLabel}</span>
              <strong className="provider-summary-value">
                {verdict!.todayObserved
                  ? cachePercentText(report.today) ?? '—'
                  : '—'}
              </strong>
            </div>
            <div>
              <span className="provider-summary-label">{report.window_days}d net</span>
              <strong className={windowNetUsd < 0 ? 'provider-summary-value warn' : 'provider-summary-value ok'}>
                {fmt.usdSigned(windowNetUsd)}
              </strong>
            </div>
          </div>
          <div className="provider-summary-foot">
            <span>
              {verdict!.insufficient
                ? `~ Building baseline · ${report.today.baseline_daily_row_count}/${CACHE_REPORT_MIN_BASELINE_DAYS}`
                : verdict!.chromeAmber
                  ? '⚠ anomaly'
                  : verdict!.todayObserved
                    ? '✓ healthy'
                    : '· no activity today'}
            </span>
            <span>{report.window_days}d native report</span>
          </div>
          {section.reason && <div className="provider-section-reason">{section.reason}</div>}
        </>
      )}
    </div>
  );
}

// Provider-aware cache forensics. Claude keeps the legacy top-level report;
// Codex publishes the same computed report shape from native inclusive-input
// cache counters. The card and modal therefore share one canonical renderer.
export function CacheReportPanel() {
  const env = useScopedSnapshot();
  const activeSource = useSyncExternalStore(subscribeStore, () => getState().activeSource);
  const isMobile = useIsMobile();
  // MUST stay above the `activeSource === 'all'` early return below. This
  // panel is mounted keyed by panel id, not by source, so the instance
  // SURVIVES a source flip — and `useAccountScope` is three
  // `useSyncExternalStore` calls. Below the return it made the hook count
  // 3-in-all / 6-in-single, so flipping the selector threw "Rendered more
  // hooks than during the previous render" and, with no error boundary in
  // the app, unmounted the whole dashboard to a blank page.
  const scope = useAccountScope();
  const collapseClass = isMobile ? ' cache-report-collapsed' : '';
  const openModal = () => {
    dispatch({ type: 'OPEN_MODAL', kind: 'cache-report' });
  };
  const composition = presentationCacheReportComposition(env, activeSource);

  if (activeSource === 'all') {
    return (
      <section
        className={`panel accent-teal${collapseClass}`}
        id="panel-cache-report"
        data-panel-kind="cache-report"
        data-source="all"
        role="region"
        aria-label="Cache Report · Claude and Codex"
        onClick={cardRegionClick(openModal)}
        style={{ cursor: 'pointer' }}
      >
        <div className="panel-header" style={{ justifyContent: 'space-between' }}>
          <div className="cr-panel-header-inner">
            <svg className="icon" aria-hidden="true"><use href="/static/icons.svg#activity" /></svg>
            <h2 style={{ color: TEAL }}>Cache Report <span className="sub">by provider</span></h2>
          </div>
          <div className="panel-header-actions">
            <ExpandButton label="Cache Report" onOpen={openModal} />
            <PanelGrip />
          </div>
        </div>
        <div className="panel-body source-all-sections provider-composition provider-composition--panel">
          {composition.sections.map((section) => (
            <CacheProviderSummary key={section.source} section={section} />
          ))}
        </div>
      </section>
    );
  }

  // #443 S2 (F3/F4/F10) — the `adapted` compatibility fallback is DELETED. It
  // fabricated a whole report whenever Codex had no native one: a row count
  // presented as a window, a hard-coded 15pp threshold, a null baseline, and an
  // `is_empty` derived from the row count that disagreed with the modal's
  // hard-coded `false` on identical data. Both surfaces now render the SAME
  // composition section, so card and modal cannot disagree by construction
  // rather than by test.
  //
  // Deliberately NOT a SourceChip: the bare single-source panel is a layering
  // contract pinned by cacheReportSource.
  const section = composition.sections[0];
  const cr = section?.value ?? null;
  const hydrating = presentationProviders(env, activeSource).hydrating;
  // With the fallback gone the rendered object IS the section's value, so the
  // chip and reason are unconditional — S1's `rendersSectionValue` guard has
  // nothing left to guard.
  const statusChip = section && section.status !== 'available' ? (
    <span className="provider-section-status">{section.status}</span>
  ) : null;
  const statusReason = section?.reason ?? null;
  // Emptiness reaches this panel by three routes, and only one of them is a
  // status. `presentationCacheReportComposition` nulls `section.value` for an
  // available-and-empty report (S1's F7 fix) and reports `empty`; a `degraded`
  // section deliberately KEEPS its value, so there emptiness still arrives as
  // `is_empty`; and a focused account with no Codex child is synthesized
  // client-side with `is_empty: true` and `cache_report: null` while the
  // ENTRY's availability stays 'ok', so its section reads `unavailable`. That
  // last account has no cache activity — it is not a snapshot that failed to
  // build, and telling the user otherwise is the false statement this session
  // exists to remove.
  const accountEmpty = cr == null
    && scope.source === activeSource
    && scope.accountKey !== null
    && scope.isEmpty;
  const isEmpty = section?.status === 'empty' || cr?.is_empty === true || accountEmpty;
  const vocab = cacheVocabulary(activeSource);
  // Mobile-driven collapse (< 720 px). The `.cache-report-collapsed`
  // modifier class hides the sparkline + secondary subline via the
  // existing @media rule at index.css:4186 so the panel reads as a
  // single-line summary on phones. Mirrors the daily-collapsed /
  // sessions-collapsed convention but is viewport-driven (no user pref).
  // #293 S4 — the region describes (role=region + aria-label); the Expand
  // button is the sole keyboard/SR open path. The guarded pointer body-click is
  // preserved via cardRegionClick so a nested control / grip never double-fires.

  // No data yet — minimal placeholder (envelope cold-start). The panel
  // still renders so panelOrder / drag-and-drop / keymap routing have
  // a real DOM target; click is wired so the modal can open even before
  // the first sync tick.
  // Branch 1 — cold start. Gated on the value being ABSENT, not on `hydrating`
  // alone: types/envelope.ts:104-106 guarantees a populated-but-incomplete
  // snapshot stays visible, and A2's progressive republish actually produces
  // one, so testing `hydrating` first would hide real data during every
  // progressive load. `isEmpty` is checked below it rather than above because
  // an emptiness signal is a real answer and a skeleton is not.
  if (!cr && hydrating && !isEmpty) {
    return (
      <section
        className={`panel accent-teal${collapseClass}`}
        id="panel-cache-report"
        data-panel-kind="cache-report"
        data-source={activeSource}
        role="region"
        aria-label="Cache Report"
        onClick={cardRegionClick(openModal)}
        style={{ cursor: 'pointer' }}
      >
        <div className="panel-header" style={{ justifyContent: 'space-between' }}>
          <div className="cr-panel-header-inner">
            <svg className="icon" aria-hidden="true"><use href="/static/icons.svg#activity" /></svg>
            <h2 style={{ color: TEAL }}>
              Cache Report <span className="sub">(loading)</span>
            </h2>
          </div>
          <div className="panel-header-actions">
            <ExpandButton label="Cache Report" onOpen={openModal} />
            <PanelGrip />
          </div>
        </div>
        <div className="panel-body">
          <PanelSkeleton lines={2} />
        </div>
      </section>
    );
  }

  // Branch 2 — empty. Reached with or without a surviving report (see the
  // `isEmpty` note above), so it must precede the failure branch: after S2 an
  // available-and-empty source arrives here with a NULL value.
  if (isEmpty) {
    // Spec §4.2 branch 2 keeps this card's copy unchanged, so an ordinary
    // cold start must not gain a second, redundant way of saying "nothing
    // here yet". Branch 4 (degraded AND empty) still needs its label, so the
    // suppression is keyed on the status rather than on emptiness. The
    // account-empty route is suppressed too: its section says "Codex cache
    // report is unavailable.", which contradicts the body above it.
    const bare = section?.status === 'empty' || accountEmpty;
    const emptyChip = bare ? null : statusChip;
    const emptyReason = bare ? null : statusReason;
    return (
      <section
        className={`panel accent-teal${collapseClass}`}
        id="panel-cache-report"
        data-panel-kind="cache-report"
        data-source={activeSource}
        role="region"
        aria-label="Cache Report · empty"
        onClick={cardRegionClick(openModal)}
        style={{ cursor: 'pointer' }}
      >
        <div className="panel-header" style={{ justifyContent: 'space-between' }}>
          <div className="cr-panel-header-inner">
            <svg className="icon" aria-hidden="true"><use href="/static/icons.svg#activity" /></svg>
            <h2 style={{ color: TEAL }}>Cache Report</h2>
            {emptyChip}
          </div>
          <div className="panel-header-actions">
            <ExpandButton label="Cache Report" onOpen={openModal} />
            <PanelGrip />
          </div>
        </div>
        <div className="cr-status-row">
          <span className="cr-glyph empty" aria-hidden="true">−</span>
          <div>
            <div className="cr-headline">No {section?.label ?? 'Claude'} activity yet</div>
            <div className="cr-subline">Run a session to start tracking</div>
            {emptyReason && <div className="cr-subline">{emptyReason}</div>}
          </div>
        </div>
      </section>
    );
  }

  // Branch 3 — #443 F8. A null report that is NOT hydrating and NOT empty is a
  // build failure, and printing "(loading)" for it was an indefinite lie.
  if (!cr) {
    return (
      <section
        className={`panel accent-amber${collapseClass}`}
        id="panel-cache-report"
        data-panel-kind="cache-report"
        data-source={activeSource}
        role="region"
        aria-label="Cache Report · failed"
        onClick={cardRegionClick(openModal)}
        style={{ cursor: 'pointer' }}
      >
        <div className="panel-header" style={{ justifyContent: 'space-between' }}>
          <div className="cr-panel-header-inner">
            <svg className="icon" aria-hidden="true"><use href="/static/icons.svg#activity" /></svg>
            <h2 style={{ color: AMBER }}>Cache Report</h2>
            {statusChip}
          </div>
          <div className="panel-header-actions">
            <ExpandButton label="Cache Report" onOpen={openModal} />
            <PanelGrip />
          </div>
        </div>
        <div className="cr-status-row">
          <span className="cr-glyph fail" aria-hidden="true">!</span>
          <div>
            <div className="cr-headline">Cache Report unavailable</div>
            <div className="cr-subline">
              The snapshot could not be built. Retrying on the next sync.
            </div>
            {statusReason && <div className="cr-subline">{statusReason}</div>}
          </div>
        </div>
      </section>
    );
  }

  const verdict = cacheReportVerdict(cr);
  const anomalous = cr.today.anomaly_triggered;
  const { insufficient } = verdict;
  const todayObserved = verdict.todayObserved;

  // Accent class flip (anomalous => amber). The header color follows the
  // same flip so the title text reads correctly against the bordered
  // panel.
  //
  // Gate the chrome flip on `!insufficient`: during the first 1–4
  // captured days a `net_negative` today already sets
  // `cr.today.anomaly_triggered = true` (the server-side classifier
  // skips only `cache_drop` when samples are thin), but the watchdog is
  // supposed to read as neutral "Building baseline" until the 5-day
  // floor exists. Flipping the border / header / sparkline-marker to
  // amber here would render a false warning before the baseline is
  // established and contradict the headline copy below
  // (CacheReportModal mirrors the same gate on .modal-card).
  const chromeAmber = verdict.chromeAmber;
  const accentClass = chromeAmber ? 'accent-amber' : 'accent-teal';
  const headerColor = chromeAmber ? AMBER : TEAL;
  const todayMarker = chromeAmber ? AMBER : GREEN;

  let glyph: { icon: string; cls: string };
  let headline: React.ReactNode;
  let sublineFirst: React.ReactNode = null;

  if (insufficient) {
    glyph = { icon: '~', cls: 'thin' };
    const n = cr.today.baseline_daily_row_count;
    headline = <>Building baseline · {n}/{CACHE_REPORT_MIN_BASELINE_DAYS} days</>;
    sublineFirst = todayObserved ? (
      <>
        Today: {vocab.percentLabelInline} {cachePercentText(cr.today) ?? '—'} · net{' '}
        {fmt.usdSigned(cr.today.net_usd)}
      </>
    ) : (
      <>No activity today</>
    );
  } else if (anomalous) {
    glyph = { icon: '⚠', cls: 'warn' };
    // Worst trigger picks the headline; cache_drop wins when both fire.
    const reasons = cr.today.anomaly_reasons;
    if (reasons.includes('cache_drop') && cr.today.delta_pp !== null) {
      // Snap-up-floor on the absolute value (matches Spotlight at
      // CacheReportSpotlight.tsx:48). Floor-then-abs would round a
      // negative delta away from zero (-16.7 -> floor=-17 -> abs=17),
      // disagreeing with the modal by 1.
      const drop = fmt.pctFloor(Math.abs(cr.today.delta_pp));
      headline = (
        <>
          Today: {vocab.percentLabelInline} <span className="delta-bad">↓ {drop}pp</span>
        </>
      );
    } else {
      headline = (
        <>
          Today: net{' '}
          <span className="delta-bad">{fmt.usdSigned(cr.today.net_usd)}</span>
        </>
      );
    }
    sublineFirst = (
      <>
        vs {cr.window_days}d median{' '}
        {cr.today.baseline_median_percent !== null
          ? fmt.pctFloor(cr.today.baseline_median_percent) + '%'
          : '—'}{' '}
        · net{' '}
        <span className="warn">{fmt.usdSigned(cr.today.net_usd)}</span>
      </>
    );
  } else if (!todayObserved) {
    // Neutral glyph, not a check: both predicates are unevaluated for a
    // synthetic today row, so a check would be a verdict over nothing.
    glyph = { icon: '·', cls: 'thin' };
    headline = <>No activity today</>;
    sublineFirst = (
      <>
        vs {cr.window_days}d median{' '}
        {cr.today.baseline_median_percent !== null
          ? fmt.pctFloor(cr.today.baseline_median_percent) + '%'
          : '—'}{' '}
        · net —
      </>
    );
  } else {
    glyph = { icon: '✓', cls: 'ok' };
    headline = (
      <>
        Today: {vocab.percentLabelInline}{' '}
        <span className="delta-good">{cachePercentText(cr.today) ?? '—'}</span>
      </>
    );
    sublineFirst = (
      <>
        vs {cr.window_days}d median{' '}
        {cr.today.baseline_median_percent !== null
          ? fmt.pctFloor(cr.today.baseline_median_percent) + '%'
          : '—'}{' '}
        · net{' '}
        <span className="ok">{fmt.usdSigned(cr.today.net_usd)}</span>
      </>
    );
  }

  // 14-day net = sum of per-day net (positive = caching paid off net
  // of waste; negative = caching cost more than it saved). Reduce
  // computes from the same array the mini bars render, so the headline
  // number and the bar magnitudes can never disagree.
  const fourteenDayNet = cr.days.reduce((acc, d) => acc + d.net_usd, 0);
  const fourteenDayNetClass = fourteenDayNet >= 0 ? 'ok' : 'warn';

  const sublineSecond = insufficient ? (
    <>Watchdog activates at {CACHE_REPORT_MIN_BASELINE_DAYS} days of history</>
  ) : (
    <>
      {cr.window_days}d net:{' '}
      <span className={fourteenDayNetClass}>{fmt.usdSigned(fourteenDayNet)}</span>
    </>
  );

  return (
    <section
      className={`panel ${accentClass}${collapseClass}`}
      id="panel-cache-report"
      data-panel-kind="cache-report"
      data-source={activeSource}
      role="region"
      aria-label="Cache Report"
      onClick={cardRegionClick(openModal)}
      style={{ cursor: 'pointer' }}
    >
      <div className="panel-header" style={{ justifyContent: 'space-between' }}>
        <div className="cr-panel-header-inner">
          <svg className="icon" aria-hidden="true"><use href="/static/icons.svg#activity" /></svg>
          <h2 style={{ color: headerColor }}>
            Cache Report
            {chromeAmber && <span className="sub">⚠ Today</span>}
          </h2>
          {statusChip}
        </div>
        <div className="panel-header-actions">
          <ExpandButton label="Cache Report" onOpen={openModal} />
          <PanelGrip />
        </div>
      </div>

      <div className="cr-status-row">
        <span className={`cr-glyph ${glyph.cls}`}>{glyph.icon}</span>
        <div>
          <div className="cr-headline">{headline}</div>
          <div className="cr-subline">{sublineFirst}</div>
        </div>
      </div>

      {!insufficient && cr.days.length > 0 && (
        <>
          <CacheSparkline
            days={cr.days}
            baseline_median_percent={cr.today.baseline_median_percent}
            today_marker_color={todayMarker}
            size="mini"
            source={activeSource}
          />
          {/* flex: 1 wrapper — the bars edge-to-edge fill whatever
              vertical room is left in the panel between the sparkline
              and the 14d-net subline. */}
          <div className="cr-netbars-mini-wrap">
            <CacheNetBars days={cr.days} size="mini" source={activeSource} />
          </div>
        </>
      )}

      <div className="cr-subline second">{sublineSecond}</div>
      {statusReason && (
        <div className="provider-section-reason">{statusReason}</div>
      )}
    </section>
  );
}
