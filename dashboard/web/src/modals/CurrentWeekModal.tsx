import { useSyncExternalStore, type ReactNode } from 'react';
import { useSnapshot } from '../hooks/useSnapshot';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { Modal } from './Modal';
import { ShareIcon } from '../components/ShareIcon';
import { fmt, type FmtCtx } from '../lib/fmt';
import { dispatch, getState, subscribeStore } from '../store/store';
import { openShareModal } from '../store/shareSlice';
import { shouldShowMilestoneTicks } from '../lib/milestoneTicks';
import { warningForDomain } from '../lib/sourceGating';
import { SourceChip } from '../panels/sourcePanel';
import type {
  CodexQuotaMilestoneRow,
  CodexSourceData,
  Envelope,
  Milestone,
  FiveHourMilestone,
  FiveHourCredit,
} from '../types/envelope';

function clamp0_100(v: number | null | undefined): number {
  if (v == null || !isFinite(v)) return 0;
  return Math.max(0, Math.min(100, v));
}

function formatWeekWindow(
  weekLabel: string | null | undefined,
  resetIso: string | null | undefined,
  ctx: FmtCtx,
): string {
  // F1: literal " UTC" suffixes are gone — `fmt.datetimeShortZ` (used for
  // the reset cell below) carries the offset itself, and the week-label
  // pill is a pure date range, so no offset-tail is appropriate here.
  const endShort = fmt.dateShort(resetIso, ctx);
  if (weekLabel && endShort) return `${weekLabel} → ${endShort}`;
  if (weekLabel) return weekLabel;
  if (endShort) return `→ ${endShort}`;
  return '—';
}

// Split a percent float into integer and ".decimal%" tail so the modal
// can style them as two spans (<span class="int">17</span><span
// class="unit">.4%</span>).
function splitBigNum(pct: number | null | undefined): [string, string] {
  if (pct == null || !isFinite(pct)) return ['—', ''];
  const s = (+pct).toFixed(1);
  const dot = s.indexOf('.');
  if (dot === -1) return [s, '.0%'];
  return [s.slice(0, dot), s.slice(dot) + '%'];
}

// Dedup milestones < 3% apart; keep first, drop near follow-ups.
function dedupeTicks<T extends { percent: number | null | undefined }>(ms: T[]): T[] {
  const kept: T[] = [];
  const sorted = [...ms].sort((a, b) => (a.percent ?? 0) - (b.percent ?? 0));
  for (const m of sorted) {
    if (m.percent == null) continue;
    if (kept.length && m.percent - (kept[kept.length - 1].percent ?? 0) < 3) continue;
    kept.push(m);
  }
  return kept;
}

function msSub(ms: Milestone[]): string | null {
  if (!Array.isArray(ms) || ms.length < 2) return null;
  const marg = ms.map((m) => m.marginal_usd).filter((v): v is number => v != null && isFinite(v));
  const avg = marg.length ? marg.reduce((a, b) => a + b, 0) / marg.length : null;
  const latestPct = ms[ms.length - 1].percent;
  const parts: string[] = [];
  if (avg != null) parts.push('avg marginal $' + avg.toFixed(2));
  if (latestPct != null) parts.push('latest at ' + latestPct + '%');
  return parts.length ? parts.join(' · ') : null;
}

// Spec §5.3 — entry kinds for the merged 5h milestone stream. Credits
// and milestones interleave chronologically; the rendered row varies
// per ``kind``.
type FhStreamEntry =
  | { kind: 'milestone'; ts: string; data: FiveHourMilestone }
  | { kind: 'credit'; ts: string; data: FiveHourCredit };

function buildFhStream(
  milestones: FiveHourMilestone[],
  credits: FiveHourCredit[],
): FhStreamEntry[] {
  const entries: FhStreamEntry[] = [];
  for (const m of milestones) {
    entries.push({ kind: 'milestone', ts: m.captured_at_utc, data: m });
  }
  for (const c of credits) {
    entries.push({ kind: 'credit', ts: c.effective_reset_at_utc, data: c });
  }
  // Lexicographic compare is chronological on ISO-8601 UTC-Z strings.
  entries.sort((a, b) => a.ts.localeCompare(b.ts));
  return entries;
}

function milestoneFiveHourPercent(
  weekly: CodexQuotaMilestoneRow,
  fiveHour: CodexQuotaMilestoneRow[],
): number | null {
  if (weekly.five_hour_percent != null) return weekly.five_hour_percent;
  const crossedAt = Date.parse(weekly.captured_at);
  const eligible = fiveHour.filter((row) => {
    const capturedAt = Date.parse(row.captured_at);
    const resetsAt = row.resets_at ? Date.parse(row.resets_at) : Number.POSITIVE_INFINITY;
    return capturedAt <= crossedAt && crossedAt < resetsAt;
  });
  eligible.sort((a, b) => Date.parse(b.captured_at) - Date.parse(a.captured_at));
  return eligible[0]?.percent ?? null;
}

function CurrentWeekShell({
  embedded,
  title,
  accentClass,
  headerExtras,
  children,
}: {
  embedded: boolean;
  title: string;
  accentClass: string;
  headerExtras: ReactNode;
  children: ReactNode;
}) {
  if (embedded) return <>{children}</>;
  return (
    <Modal title={title} accentClass={accentClass} headerExtras={headerExtras}>
      {children}
    </Modal>
  );
}

function CodexCurrentCycleModal({
  env,
  ctx,
  embedded = false,
}: {
  env: Envelope | null;
  ctx: FmtCtx;
  embedded?: boolean;
}) {
  const codex = env?.sources?.codex?.data as CodexSourceData | undefined;
  const hero = codex?.hero;
  const cycle = hero?.cycle;
  const weeklyHistories = codex?.quota.histories
    .filter((row) => row.window_minutes === 10_080) ?? [];
  const activeWeeklyKeys = new Set(
    hero?.quota.active
      .filter((row) => row.resets_at === cycle?.resets_at)
      .map((row) => row.key) ?? [],
  );
  const history = [...weeklyHistories]
    .sort((a, b) => {
      const aActive = activeWeeklyKeys.has(a.key) || a.forecast.resets_at === cycle?.resets_at;
      const bActive = activeWeeklyKeys.has(b.key) || b.forecast.resets_at === cycle?.resets_at;
      if (aActive !== bActive) return aActive ? -1 : 1;
      return (b.current_percent ?? -1) - (a.current_percent ?? -1)
        || (b.captured_at ?? '').localeCompare(a.captured_at ?? '');
    })[0];
  const currentPercent = history?.current_percent
    ?? hero?.quota.active.find((row) => row.key === history?.key)?.current_percent
    ?? 0;
  const pct = clamp0_100(currentPercent);
  const [bigInt, bigUnit] = splitBigNum(currentPercent);
  const dpp = hero?.cost_usd != null && currentPercent > 0
    ? hero.cost_usd / currentPercent
    : null;
  const cycleStart = cycle?.start_at ? Date.parse(cycle.start_at) : Number.NaN;
  const cycleEnd = cycle?.resets_at ? Date.parse(cycle.resets_at) : Number.NaN;
  const allMilestones = codex?.quota.milestones ?? [];
  const inCycle = (row: CodexQuotaMilestoneRow) => {
    const capturedAt = Date.parse(row.captured_at);
    return Number.isFinite(cycleStart) && Number.isFinite(cycleEnd)
      ? cycleStart <= capturedAt && capturedAt < cycleEnd
      : true;
  };
  const weeklyMilestones = allMilestones
    .filter((row) => row.window_minutes === 10_080
      && row.quota_key === history?.key
      && row.resets_at === cycle?.resets_at
      && inCycle(row))
    .sort((a, b) => a.percent - b.percent || a.captured_at.localeCompare(b.captured_at));
  const weeklyTicks = dedupeTicks(weeklyMilestones);
  const fiveHourHistory = codex?.quota.histories.find((row) => row.window_minutes === 300);
  const fiveHourMilestones = allMilestones
    .filter((row) => row.window_minutes === 300
      && row.quota_key === fiveHourHistory?.key
      && inCycle(row));
  const pill = cycle
    ? `${fmt.dateShort(cycle.start_at, ctx)} → ${fmt.dateShort(cycle.resets_at, ctx)}`
    : 'Native 7-day cycle unavailable';
  const singleId = (value: string) => embedded ? undefined : value;

  return (
    <CurrentWeekShell
      embedded={embedded}
      title="Current Cycle — per-percent milestones"
      accentClass="accent-orange"
      headerExtras={
        <ShareIcon
          panel="current-week"
          panelLabel="Current cycle"
          triggerId="current-week-modal"
          onClick={() => dispatch(openShareModal('current-week', 'current-week-modal'))}
        />
      }
    >
      <section className="modal-current-week" data-source="codex">
        <div className="m-chipstrip" id={singleId('mcw-badges')}>
          <span className="m-pill accent-orange" id={singleId('mcw-week-pill')}>{pill}</span>
          <span className="m-pill accent-orange">Codex · native 7-day quota</span>
        </div>

        <div className="mcw-herobar">
          <div className="mcw-bignum" id={singleId('mcw-bignum')}>
            <span className="int">{bigInt}</span>
            <span className="unit">{bigUnit}</span>
          </div>
          <div className="mcw-pbar-wrap">
            <div className="mcw-pbar">
              <div className="fill" id={singleId('mcw-fill')} style={{ width: pct + '%' }} />
              {shouldShowMilestoneTicks(pct) && (
                <div className="ticks" id={singleId('mcw-ticks')}>
                  {weeklyTicks.map((row) => (
                    <div key={row.key} className="tick" data-p={String(row.percent)} style={{ left: clamp0_100(row.percent) + '%' }} />
                  ))}
                </div>
              )}
              <div className="marker" id={singleId('mcw-marker')} style={{ left: pct + '%' }} />
            </div>
            <div className="mcw-pscale">
              <span>0%</span><span>25%</span><span>50%</span><span>75%</span><span>100%</span>
            </div>
          </div>
          <div className="mcw-mini" id={singleId('mcw-mini')}>
            <div className="s"><span className="k">spent</span><span className="v v-magenta">{fmt.usd2(hero?.cost_usd)}</span></div>
            <div className="s"><span className="k">$ / 1%</span><span className="v v-cyan">{fmt.usd3(dpp)}</span></div>
            <div className="s"><span className="k">reset</span><span className="v">{fmt.datetimeShortZ(cycle?.resets_at, ctx)}</span></div>
          </div>
        </div>

        <h3 className="m-sec sec-ms">
          <svg className="icon" aria-hidden="true"><use href="/static/icons.svg#hash" /></svg>
          Milestones
        </h3>
        <div className="mcw-mshead">
          <span className="m-pill accent-orange" id={singleId('mcw-ms-count')}>{weeklyMilestones.length} crossed</span>
          <span className="mcw-ms-sub">Derived from retained OpenAI quota observations</span>
        </div>
        <table className="m-histable mcw-table" id={singleId('mcw-table')}>
          <thead>
            <tr>
              <th>%</th>
              <th>Crossed ({ctx.offsetLabel})</th>
              <th className="num">Cumulative $</th>
              <th className="num">Marginal $</th>
              <th className="num">5h %</th>
            </tr>
          </thead>
          <tbody id={singleId('mcw-rows')}>
            {weeklyMilestones.length === 0 ? (
              <tr><td colSpan={5} className="empty-state">No integer-percent crossing has been retained in this cycle yet.</td></tr>
            ) : weeklyMilestones.map((row) => (
              <tr key={row.key}>
                <td><span className="m-pill accent-orange pct-cell">{row.percent}</span></td>
                <td className="d">{fmt.startedShort(row.captured_at, ctx, { noSuffix: true })}</td>
                <td className="num">{fmt.usd2(row.cumulative_usd)}</td>
                <td className="num"><span className="m-marginal">{fmt.usd2(row.marginal_usd)}</span></td>
                <td className="num"><span className="m-fh">{fmt.pct0(milestoneFiveHourPercent(row, fiveHourMilestones))}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </CurrentWeekShell>
  );
}

function ClaudeCurrentWeekModal({
  env,
  ctx,
  display,
  embedded = false,
}: {
  env: Envelope | null;
  ctx: FmtCtx;
  display: ReturnType<typeof useDisplayTz>;
  embedded?: boolean;
}) {
  const cw = env?.current_week ?? null;
  const header = env?.header ?? null;
  const ms = Array.isArray(cw?.milestones) ? cw!.milestones : [];
  const fhMs: FiveHourMilestone[] = Array.isArray(cw?.five_hour_milestones)
    ? cw!.five_hour_milestones!
    : [];
  const fhCredits: FiveHourCredit[] = Array.isArray(cw?.five_hour_block?.credits)
    ? cw!.five_hour_block!.credits!
    : [];
  const fhStream = buildFhStream(fhMs, fhCredits);
  const pct = clamp0_100(cw?.used_pct);
  const [bigInt, bigUnit] = splitBigNum(cw?.used_pct);
  const weekPillText = cw
    ? formatWeekWindow(header?.week_label, cw.reset_at_utc, ctx)
    : '—';
  const ticks = dedupeTicks(ms);
  const subText = msSub(ms);
  const singleId = (value: string) => embedded ? undefined : value;

  return (
    <CurrentWeekShell
      embedded={embedded}
      title="Current Week — per-percent milestones"
      accentClass="accent-green"
      headerExtras={
        <ShareIcon
          panel="current-week"
          panelLabel="Current week"
          triggerId="current-week-modal"
          onClick={() => dispatch(openShareModal('current-week', 'current-week-modal'))}
        />
      }
    >
      <section className="modal-current-week" data-source="claude">
        <div className="m-chipstrip" id={singleId('mcw-badges')}>
          <span className="m-pill accent-green" id={singleId('mcw-week-pill')}>
            {weekPillText}
          </span>
        </div>

        <div className="mcw-herobar">
          <div className="mcw-bignum" id={singleId('mcw-bignum')}>
            <span className="int">{bigInt}</span>
            <span className="unit">{bigUnit}</span>
          </div>
          <div className="mcw-pbar-wrap">
            <div className="mcw-pbar">
              <div className="fill" id={singleId('mcw-fill')} style={{ width: pct + '%' }} />
              {shouldShowMilestoneTicks(pct) && (
                <div className="ticks" id={singleId('mcw-ticks')}>
                  {ticks.map((m) => (
                    <div
                      key={m.percent}
                      className="tick"
                      data-p={String(m.percent)}
                      style={{ left: clamp0_100(m.percent) + '%' }}
                    />
                  ))}
                </div>
              )}
              <div className="marker" id={singleId('mcw-marker')} style={{ left: pct + '%' }} />
            </div>
            <div className="mcw-pscale">
              <span>0%</span>
              <span>25%</span>
              <span>50%</span>
              <span>75%</span>
              <span>100%</span>
            </div>
          </div>
          <div className="mcw-mini" id={singleId('mcw-mini')}>
            <div className="s">
              <span className="k">spent</span>
              <span className="v v-magenta" id={singleId('mcw-spent')}>{fmt.usd2(cw?.spent_usd)}</span>
            </div>
            <div className="s">
              <span className="k">$ / 1%</span>
              <span className="v v-cyan" id={singleId('mcw-dpp')}>{fmt.usd3(cw?.dollar_per_pct)}</span>
            </div>
            <div className="s">
              <span className="k">reset</span>
              <span className="v" id={singleId('mcw-reset')}>{fmt.datetimeShortZ(cw?.reset_at_utc, ctx)}</span>
            </div>
          </div>
        </div>

        <h3 className="m-sec sec-ms">
          <svg className="icon" aria-hidden="true">
            <use href="/static/icons.svg#hash" />
          </svg>
          Milestones
        </h3>
        <div className="mcw-mshead">
          <span className="m-pill accent-purple" id={singleId('mcw-ms-count')}>
            {ms.length} crossed
          </span>
          <span className="mcw-ms-sub" id={singleId('mcw-ms-sub')} hidden={!subText}>
            {subText ?? ''}
          </span>
        </div>
        {ms.length === 0 ? (
          <p className="empty-state" id={singleId('mcw-empty')}>
            No milestones yet — earliest crosses at 1&nbsp;%.
          </p>
        ) : (
          <table className="m-histable mcw-table" id={singleId('mcw-table')}>
            <thead>
              <tr>
                <th>%</th>
                <th>Crossed ({display.offsetLabel})</th>
                <th className="num">Cumulative $</th>
                <th className="num">Marginal $</th>
                <th className="num">5h %</th>
              </tr>
            </thead>
            <tbody id={singleId('mcw-rows')}>
              {ms.map((m) => (
                <tr key={m.percent}>
                  <td>
                    <span className="m-pill accent-purple pct-cell">
                      {m.percent ?? '—'}
                    </span>
                  </td>
                  <td className="d">
                    {fmt.startedShort(m.crossed_at_utc, ctx, { noSuffix: true })}
                  </td>
                  <td className="num">
                    {m.cumulative_usd != null ? '$' + m.cumulative_usd.toFixed(2) : '—'}
                  </td>
                  <td className="num">
                    <span className="m-marginal">
                      {m.marginal_usd != null ? '$' + m.marginal_usd.toFixed(2) : '—'}
                    </span>
                  </td>
                  <td className="num">
                    <span className="m-fh">
                      {m.five_hour_pct_at_cross != null
                        ? Math.round(m.five_hour_pct_at_cross) + '%'
                        : '—'}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {/* Spec §5.3 — 5h milestone timeline (NEW). Parallel to the
            weekly milestone table above. Renders the merged
            (milestone + credit) chronological stream so the user sees
            both pre-credit and post-credit crossings of the active 5h
            block; credit rows render as a ⚡ CREDIT divider with the
            delta-pp + HH:MM. Suppressed entirely when both streams are
            empty so the modal stays compact for pre-v1.7.x users. */}
        {fhStream.length > 0 && (
          <>
            <h3 className="m-sec sec-ms sec-5h">
              <svg className="icon" aria-hidden="true">
                <use href="/static/icons.svg#activity" />
              </svg>
              5h milestones
            </h3>
            <div className="mcw-mshead">
              <span className="m-pill accent-purple" id={singleId('mcw-5h-count')}>
                {fhMs.length} crossed
              </span>
            </div>
            <table className="m-histable mcw-5h-table" id={singleId('mcw-5h-table')}>
              <thead>
                <tr>
                  <th>%</th>
                  <th>When ({display.offsetLabel})</th>
                  <th className="num">Block $</th>
                  <th className="num">Marginal $</th>
                  <th className="num">7d %</th>
                </tr>
              </thead>
              <tbody>
                {fhStream.map((ev, i) => {
                  if (ev.kind === 'credit') {
                    return (
                      <tr key={`credit-${i}-${ev.ts}`} className="mcw-5h-credit-row">
                        <td colSpan={5} className="mcw-5h-credit-cell">
                          ⚡ CREDIT&nbsp;{' '}
                          {ev.data.delta_pp > 0 ? '+' : ''}
                          {ev.data.delta_pp.toFixed(0)}pp&nbsp;@{' '}
                          {fmt.startedShort(ev.ts, ctx, { noSuffix: true })}
                        </td>
                      </tr>
                    );
                  }
                  const m = ev.data;
                  // React row key: ``percent_threshold`` alone would
                  // collide on post-credit threshold repeats (same
                  // threshold seen twice across pre/post segments).
                  // ``reset_event_id`` is the segment discriminator.
                  return (
                    <tr key={`fhms-${m.percent_threshold}-${m.reset_event_id}`}>
                      <td>
                        <span className="m-pill accent-purple pct-cell">
                          {m.percent_threshold}
                        </span>
                      </td>
                      <td className="d">
                        {fmt.startedShort(m.captured_at_utc, ctx, { noSuffix: true })}
                      </td>
                      <td className="num">
                        {'$' + m.block_cost_usd.toFixed(2)}
                      </td>
                      <td className="num">
                        <span className="m-marginal">
                          {m.marginal_cost_usd != null
                            ? '$' + m.marginal_cost_usd.toFixed(2)
                            : '—'}
                        </span>
                      </td>
                      <td className="num">
                        <span className="m-fh">
                          {m.seven_day_pct_at_crossing != null
                            ? Math.round(m.seven_day_pct_at_crossing) + '%'
                            : '—'}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </>
        )}
      </section>
    </CurrentWeekShell>
  );
}

function providerReason(env: Envelope | null, source: 'claude' | 'codex'): string | null {
  const entry = env?.sources?.[source];
  const warning = warningForDomain(entry?.warnings, 'hero');
  if (warning != null) return warning.message;
  if (entry?.availability === 'unavailable') {
    return `${source === 'claude' ? 'Claude' : 'Codex'} source data is unavailable.`;
  }
  if (entry?.capabilities?.hero?.status === 'unavailable') {
    return source === 'claude'
      ? 'Claude current-week usage is unavailable.'
      : 'Codex native reset cycle is unavailable.';
  }
  if (source === 'claude' && env?.current_week == null) {
    return 'Claude current-week usage is unavailable.';
  }
  const codex = entry?.data as CodexSourceData | null | undefined;
  if (source === 'codex' && codex?.hero?.cycle == null) {
    return 'Codex native reset cycle is unavailable.';
  }
  return null;
}

function AllCurrentWeekModal({
  env,
  ctx,
  display,
}: {
  env: Envelope | null;
  ctx: FmtCtx;
  display: ReturnType<typeof useDisplayTz>;
}) {
  const claudeReason = providerReason(env, 'claude');
  const codexReason = providerReason(env, 'codex');
  return (
    <Modal
      title="Current Usage — provider cycles"
      accentClass="accent-blue"
      wide
      headerExtras={
        <ShareIcon
          panel="current-week"
          panelLabel="Current usage"
          triggerId="current-week-modal"
          onClick={() => dispatch(openShareModal('current-week', 'current-week-modal'))}
        />
      }
    >
      <div className="provider-composition provider-composition--modal current-week-provider-composition">
        <section className="source-provider-section provider-composition-section current-week-provider-section" data-provider-section="claude">
          <div className="source-provider-head provider-composition-head">
            <SourceChip source="claude" />
            <span>subscription week</span>
          </div>
          {claudeReason && <p className="provider-section-reason">{claudeReason}</p>}
          <ClaudeCurrentWeekModal env={env} ctx={ctx} display={display} embedded />
        </section>
        <section className="source-provider-section provider-composition-section current-week-provider-section" data-provider-section="codex">
          <div className="source-provider-head provider-composition-head">
            <SourceChip source="codex" />
            <span>native 7-day quota</span>
          </div>
          {codexReason && <p className="provider-section-reason">{codexReason}</p>}
          <CodexCurrentCycleModal env={env} ctx={ctx} embedded />
        </section>
      </div>
    </Modal>
  );
}

export function CurrentWeekModal() {
  const env = useSnapshot();
  const source = useSyncExternalStore(
    subscribeStore,
    () => getState().openModalSource ?? getState().activeSource,
  );
  const display = useDisplayTz();
  const ctx: FmtCtx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  if (source === 'codex') return <CodexCurrentCycleModal env={env} ctx={ctx} />;
  if (source === 'all') return <AllCurrentWeekModal env={env} ctx={ctx} display={display} />;
  return <ClaudeCurrentWeekModal env={env} ctx={ctx} display={display} />;
}
