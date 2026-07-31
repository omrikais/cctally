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
import { useScopedSnapshot } from '../hooks/useScopedSnapshot';
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
import {
  presentationCacheDays,
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
              <span className="provider-summary-label">Cache hit</span>
              <strong className="provider-summary-value">
                {verdict!.todayObserved
                  ? `${fmt.pctFloor(report.today.cache_hit_percent)}%`
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

  const adaptedDays = presentationCacheDays(env, activeSource);
  const nativeReport = env?.sources?.codex?.data?.cache_report ?? undefined;
  const newest = adaptedDays?.[0];
  const adapted: CacheReportEnvelope | undefined = activeSource === 'claude' ? undefined : adaptedDays == null ? undefined : {
    window_days: adaptedDays.length,
    anomaly_threshold_pp: 15,
    anomaly_window_days: 14,
    today: {
      date: newest?.date ?? '',
      cache_hit_percent: newest?.cache_hit_percent ?? 0,
      baseline_median_percent: null,
      delta_pp: null,
      net_usd: 0,
      saved_usd: 0,
      wasted_usd: 0,
      anomaly_triggered: false,
      anomaly_reasons: [],
      baseline_daily_row_count: adaptedDays.length,
    },
    days: adaptedDays,
    by_project: [],
    by_model: [],
    seven_day_net_usd: 0,
    seven_day_anomaly_count: 0,
    fourteen_day_counterfactual_usd: 0,
    fourteen_day_efficiency_ratio: 0,
    is_empty: adaptedDays.length === 0,
  };
  const cr = activeSource === 'claude' ? env?.cache_report : nativeReport ?? adapted;
  // #443 F5 — the composition was already computed above and then discarded
  // outside the `all` branch, so a stale or degraded source rendered a
  // confident verdict here while the `all` view showed a warning chip beside
  // the same data. Reuse it. Deliberately NOT a SourceChip: the bare
  // single-source panel is a layering contract pinned by cacheReportSource.
  const section = composition.sections[0];
  const hydrating = presentationProviders(env, activeSource).hydrating;
  // The chip and reason describe the availability of the SECTION's own report.
  // When Codex has daily rows but no native report the rendered object is the
  // `adapted` compatibility fallback, not `section.value`, and labelling that
  // with the section's "Codex cache report is unavailable." would pair a full
  // verdict with a chip denying the report exists. Saying nothing there is the
  // honest option until S2 (F3/F4) designs the fallback's own presentation.
  const rendersSectionValue = (cr ?? null) === (section?.value ?? null);
  const statusChip = section && rendersSectionValue && section.status !== 'available' ? (
    <span className="provider-section-status">{section.status}</span>
  ) : null;
  const statusReason = section && rendersSectionValue ? section.reason : null;
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
  // progressive load.
  if (!cr && hydrating) {
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

  // Branch 2 — #443 F8. A null report that is NOT hydrating is a build
  // failure, and printing "(loading)" for it was an indefinite lie.
  if (!cr) {
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
            <h2 style={{ color: TEAL }}>Cache Report</h2>
            {statusChip}
          </div>
          <div className="panel-header-actions">
            <ExpandButton label="Cache Report" onOpen={openModal} />
            <PanelGrip />
          </div>
        </div>
        <div className="cr-status-row">
          <span className="cr-glyph thin">−</span>
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

  // Empty-state — no Claude activity in the window.
  if (cr.is_empty) {
    // Spec §4.2 branch 2 keeps this card's copy unchanged, so an ordinary
    // cold start must not gain a second, redundant way of saying "nothing
    // here yet". Branch 4 (degraded AND empty) still needs its label, so the
    // suppression is keyed on the status rather than on emptiness.
    const emptyChip = section?.status === 'empty' ? null : statusChip;
    const emptyReason = section?.status === 'empty' ? null : statusReason;
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
            <h2 style={{ color: TEAL }}>Cache Report</h2>
            {emptyChip}
          </div>
          <div className="panel-header-actions">
            <ExpandButton label="Cache Report" onOpen={openModal} />
            <PanelGrip />
          </div>
        </div>
        <div className="cr-status-row">
          <span className="cr-glyph thin">−</span>
          <div>
            <div className="cr-headline">No {activeSource === 'codex' ? 'Codex' : 'Claude'} activity yet</div>
            <div className="cr-subline">Run a session to start tracking</div>
            {emptyReason && <div className="cr-subline">{emptyReason}</div>}
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
        Today: cache hit {fmt.pctFloor(cr.today.cache_hit_percent)}% · net{' '}
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
          Today: cache hit <span className="delta-bad">↓ {drop}pp</span>
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
        vs 14d median{' '}
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
        vs 14d median{' '}
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
        Today: cache hit{' '}
        <span className="delta-good">{fmt.pctFloor(cr.today.cache_hit_percent)}%</span>
      </>
    );
    sublineFirst = (
      <>
        vs 14d median{' '}
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
      14d net:{' '}
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
          />
          {/* flex: 1 wrapper — the bars edge-to-edge fill whatever
              vertical room is left in the panel between the sparkline
              and the 14d-net subline. */}
          <div className="cr-netbars-mini-wrap">
            <CacheNetBars days={cr.days} size="mini" />
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
