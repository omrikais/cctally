import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from 'react';
import { useSnapshot } from '../hooks/useSnapshot';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { useKeymap } from '../hooks/useKeymap';
import { Modal } from './Modal';
import { ShareIcon } from '../components/ShareIcon';
import { fmt, type FmtCtx } from '../lib/fmt';
import { dispatch, getState, subscribeStore, topmostStoreFocusLayer } from '../store/store';
import type { Binding } from '../store/keymap';
import { openShareModal } from '../store/shareSlice';
import { shouldShowMilestoneTicks } from '../lib/milestoneTicks';
import { warningForDomain } from '../lib/sourceGating';
import { SourceChip } from '../panels/sourcePanel';
import { fetchWeekDetail, stepWeek } from './milestoneHistory';
import type {
  CodexQuotaMilestoneRow,
  CodexSourceData,
  Envelope,
  Milestone,
  FiveHourMilestone,
  FiveHourCredit,
  WeekDetailBlock,
  WeekDetailPayload,
  WeekIndexEntry,
} from '../types/envelope';

const EMPTY_BINDINGS: Binding[] = [];

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

function formatCycleRange(entry: WeekIndexEntry, ctx: FmtCtx): string {
  const start = fmt.dateShort(entry.start_at_utc, ctx);
  const end = fmt.dateShort(entry.end_at_utc, ctx);
  return start && end ? `${start}–${end}` : entry.label;
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

// ── Hero-modal historical-milestone navigation (spec §4) ───────────────
//
// Shared per-provider navigation state: which week/cycle is selected (null
// == the live current week), whether a block-step wants the current week's
// full payload, the fetched detail (or fetch error), and the selected block.
// Selection is modal-local and resets on (re)mount — `ModalRoot` unmounts
// the modal on close, so a fresh open always starts on the current week.

interface MilestoneNav {
  index: WeekIndexEntry[];
  weekKey: string | null;              // null == current week
  setWeekKey: (k: string | null) => void;
  selectedEntry: WeekIndexEntry | null;
  currentEntry: WeekIndexEntry | null;
  detail: WeekDetailPayload | null;
  loading: boolean;
  error: { status?: number; code?: string } | null;
  vanished: boolean;
  wantDetail: boolean;
  pendingBlockStep: -1 | 1 | null;
  requestCurrentDetail: (dir: -1 | 1) => void;
  clearPendingBlockStep: () => void;
  retry: () => void;
  blockSel: string | number | null;
  setBlockSel: (b: string | number | null) => void;
}

function useMilestoneNav(
  source: 'claude' | 'codex',
  index: WeekIndexEntry[],
): MilestoneNav {
  const [weekKey, setWeekKeyRaw] = useState<string | null>(null);
  const [blockSel, setBlockSel] = useState<string | number | null>(null);
  const [wantDetail, setWantDetail] = useState(false);
  const [pendingBlockStep, setPendingBlockStep] = useState<-1 | 1 | null>(null);
  const [detail, setDetail] = useState<WeekDetailPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<{ status?: number; code?: string } | null>(null);
  const reqSeq = useRef(0);

  const currentEntry = useMemo(() => index.find((e) => e.is_current) ?? null, [index]);
  const selectedEntry = useMemo(
    () => (weekKey == null ? currentEntry : index.find((e) => e.key === weekKey) ?? null),
    [weekKey, index, currentEntry],
  );
  const vanished = weekKey != null && !index.some((e) => e.key === weekKey);

  // A block-step / week-step resets the selected block back to the default.
  const setWeekKey = useCallback((k: string | null) => {
    setWeekKeyRaw(k);
    setBlockSel(null);
    setWantDetail(false);
    setPendingBlockStep(null);
    setError(null);
  }, []);
  const requestCurrentDetail = useCallback((dir: -1 | 1) => {
    setPendingBlockStep(dir);
    setWantDetail(true);
  }, []);
  const clearPendingBlockStep = useCallback(() => setPendingBlockStep(null), []);

  // Fetch policy (spec §4): historic selection always fetches; the current
  // week fetches when its entry is multi-segment (envelope is
  // active-segment-only) OR once a block-step asks for the full payload.
  const shouldFetch =
    selectedEntry != null && !vanished && (
      weekKey != null ||
      (selectedEntry.segment_count ?? 0) > 1 ||
      wantDetail
    );
  const fetchKey = shouldFetch && selectedEntry
    ? `${selectedEntry.key}|${selectedEntry.detail_stamp}`
    : null;

  const doFetch = useCallback(() => {
    if (!selectedEntry) return;
    const seq = ++reqSeq.current;
    setLoading(true);
    setError(null);
    fetchWeekDetail(source, selectedEntry)
      .then((payload) => {
        if (seq !== reqSeq.current) return; // only the latest selection resolves
        setDetail(payload);
        setLoading(false);
      })
      .catch((e: { status?: number; code?: string }) => {
        if (seq !== reqSeq.current) return;
        setError({ status: e?.status, code: e?.code });
        setLoading(false);
      });
  }, [source, selectedEntry]);

  useEffect(() => {
    if (fetchKey == null) {
      // Current week rendered purely from the envelope — clear any prior fetch.
      reqSeq.current++;
      setDetail(null);
      setLoading(false);
      setError(null);
      return;
    }
    doFetch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fetchKey]);

  const retry = useCallback(() => doFetch(), [doFetch]);

  return {
    index, weekKey, setWeekKey, selectedEntry, currentEntry, detail, loading,
    error, vanished, wantDetail, pendingBlockStep, requestCurrentDetail,
    clearPendingBlockStep, retry, blockSel, setBlockSel,
  };
}

function WeekNavChip({
  nav,
  pillText,
  accentClass,
  singleId,
}: {
  nav: MilestoneNav;
  pillText: string;
  accentClass: string;
  singleId: (v: string) => string | undefined;
}) {
  const { index, weekKey, setWeekKey } = nav;
  const olderTarget = stepWeek(index, weekKey, 1);
  const newerTarget = weekKey == null ? null : (stepWeek(index, weekKey, -1) ?? null);
  const canOlder = olderTarget != null;
  // Newer than the current week is impossible; from a historic week, the
  // "newer" step can land back on the current week (represented as null).
  const canNewer = weekKey != null;
  return (
    <div className={`m-chipstrip mcw-weeknav`} id={singleId('mcw-badges')}>
      <button
        type="button"
        className={`m-pill ${accentClass} mcw-weeknav-btn`}
        aria-label="Older week"
        disabled={!canOlder}
        onClick={() => { if (olderTarget != null) setWeekKey(olderTarget); }}
      >
        ‹
      </button>
      <span className={`m-pill ${accentClass}`} id={singleId('mcw-week-pill')}>{pillText}</span>
      <button
        type="button"
        className={`m-pill ${accentClass} mcw-weeknav-btn`}
        aria-label="Newer week"
        disabled={!canNewer}
        onClick={() => { if (weekKey != null) setWeekKey(newerTarget); }}
      >
        ›
      </button>
    </div>
  );
}

interface BlockNavWindow {
  block_start_at?: string | null;
  five_hour_resets_at?: string | null;
  crossed_seven_day_reset?: boolean;
}

function BlockNavHeaderFrame({
  blockCount,
  selectedIndex,
  block,
  onStep,
  ctx,
  singleId,
  accentClass,
}: {
  blockCount: number;
  selectedIndex: number;
  block: BlockNavWindow | null;
  onStep: (dir: -1 | 1) => void;
  ctx: FmtCtx;
  singleId: (v: string) => string | undefined;
  accentClass: string;
}) {
  const crossed = !!block?.crossed_seven_day_reset;
  const startShort = fmt.startedShort(block?.block_start_at, ctx, { noSuffix: true });
  const endShort = fmt.startedShort(block?.five_hour_resets_at, ctx, { noSuffix: true });
  return (
    <div className="mcw-mshead mcw-blocknav" id={singleId('mcw-blocknav')}>
      <button
        type="button"
        className={`m-pill ${accentClass} mcw-blocknav-btn`}
        aria-label="Older block"
        disabled={selectedIndex <= 0}
        onClick={() => onStep(-1)}
      >
        ‹
      </button>
      <span className={`m-pill ${accentClass}`}>
        {crossed ? '⚡ ' : ''}Block {selectedIndex + 1} of {blockCount}
      </span>
      <span className="mcw-ms-sub">{startShort} → {endShort}</span>
      <button
        type="button"
        className={`m-pill ${accentClass} mcw-blocknav-btn`}
        aria-label="Newer block"
        disabled={selectedIndex >= blockCount - 1}
        onClick={() => onStep(1)}
      >
        ›
      </button>
    </div>
  );
}

function BlockNavHeader({
  blocks,
  selectedIndex,
  onStep,
  ctx,
  singleId,
  accentClass,
}: {
  blocks: WeekDetailBlock[];
  selectedIndex: number;
  onStep: (dir: -1 | 1) => void;
  ctx: FmtCtx;
  singleId: (v: string) => string | undefined;
  accentClass: string;
}) {
  return (
    <BlockNavHeaderFrame
      blockCount={blocks.length}
      selectedIndex={selectedIndex}
      block={blocks[selectedIndex] ?? null}
      onStep={onStep}
      ctx={ctx}
      singleId={singleId}
      accentClass={accentClass}
    />
  );
}

function CurrentBlockNavHeader({
  blockCount,
  block,
  onStep,
  ctx,
  singleId,
  accentClass,
}: {
  blockCount: number;
  block: BlockNavWindow | null;
  onStep: (dir: -1 | 1) => void;
  ctx: FmtCtx;
  singleId: (v: string) => string | undefined;
  accentClass: string;
}) {
  return (
    <BlockNavHeaderFrame
      blockCount={blockCount}
      selectedIndex={blockCount - 1}
      block={block}
      onStep={onStep}
      ctx={ctx}
      singleId={singleId}
      accentClass={accentClass}
    />
  );
}

function fiveHoursAfter(startAt: string | null | undefined): string | null {
  const startMs = Date.parse(startAt ?? '');
  return Number.isFinite(startMs)
    ? new Date(startMs + 5 * 60 * 60 * 1000).toISOString()
    : null;
}

function VanishedState({ onBack }: { onBack: () => void }) {
  return (
    <section className="modal-current-week">
      <p className="empty-state">This cycle is no longer available.</p>
      <p className="empty-state">
        <button type="button" className="m-pill accent-green" onClick={onBack}>
          Back to current
        </button>
      </p>
    </section>
  );
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

// Flatten a Claude week-detail payload into weekly-table rows with a
// full-width credit divider between consecutive segments (spec §4, Q4).
type WeeklyRow =
  | { kind: 'ms'; m: Milestone; key: string }
  | { kind: 'divider'; effectiveAt: string; priorPercent: number | null; key: string };

function flattenClaudeWeekly(detail: WeekDetailPayload): { rows: WeeklyRow[]; count: number } {
  const rows: WeeklyRow[] = [];
  detail.segments.forEach((seg, si) => {
    if (si > 0) {
      const d = detail.dividers[si - 1];
      if (d) {
        rows.push({
          kind: 'divider', effectiveAt: d.effective_at_utc,
          priorPercent: d.prior_percent, key: `div-${si}`,
        });
      }
    }
    (seg.milestones as Milestone[]).forEach((m) => {
      rows.push({ kind: 'ms', m, key: `ms-${seg.key}-${m.percent}` });
    });
  });
  const count = detail.segments.reduce((n, s) => n + s.milestones.length, 0);
  return { rows, count };
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
  const cycleIndex: WeekIndexEntry[] = Array.isArray(codex?.quota.cycle_index)
    ? codex!.quota.cycle_index!
    : [];
  const nav = useMilestoneNav('codex', cycleIndex);
  const {
    weekKey, setWeekKey, selectedEntry, detail, loading, error, vanished,
    blockSel, setBlockSel, requestCurrentDetail, wantDetail, pendingBlockStep,
    clearPendingBlockStep,
  } = nav;
  const isHistoric = weekKey != null;

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
  const envCurrentPercent = history?.current_percent
    ?? hero?.quota.active.find((row) => row.key === history?.key)?.current_percent
    ?? 0;
  const cycleStart = cycle?.start_at ? Date.parse(cycle.start_at) : Number.NaN;
  const cycleEnd = cycle?.resets_at ? Date.parse(cycle.resets_at) : Number.NaN;
  const allMilestones = codex?.quota.milestones ?? [];
  const inCycle = (row: CodexQuotaMilestoneRow) => {
    const capturedAt = Date.parse(row.captured_at);
    return Number.isFinite(cycleStart) && Number.isFinite(cycleEnd)
      ? cycleStart <= capturedAt && capturedAt < cycleEnd
      : true;
  };
  const envWeeklyMilestones = allMilestones
    .filter((row) => row.window_minutes === 10_080
      && row.quota_key === history?.key
      && row.resets_at === cycle?.resets_at
      && inCycle(row))
    .sort((a, b) => a.percent - b.percent || a.captured_at.localeCompare(b.captured_at));
  const fiveHourHistory = codex?.quota.histories.find((row) => row.window_minutes === 300);
  const currentFiveHourBlock = codex?.quota.blocks.find((row) =>
    row.window_minutes === 300
    && row.is_active
    && !row.orphaned
    && (
      fiveHourHistory?.forecast.resets_at == null
      || row.resets_at === fiveHourHistory.forecast.resets_at
    )
  ) ?? null;
  const currentBlockPreview: BlockNavWindow | null = currentFiveHourBlock
    ? {
      block_start_at: currentFiveHourBlock.start_at,
      five_hour_resets_at: currentFiveHourBlock.end_at,
      crossed_seven_day_reset: false,
    }
    : null;
  const envFiveHourMilestones = allMilestones
    .filter((row) => row.window_minutes === 300
      && row.quota_key === fiveHourHistory?.key
      && inCycle(row));

  // Historic cycles read their milestone rows from the fetched payload's
  // single segment; the current cycle stays envelope-driven.
  const useDetail = isHistoric && detail != null;
  const detailWeekly = useDetail
    ? ((detail!.segments[0]?.milestones ?? []) as CodexQuotaMilestoneRow[])
    : [];
  const detailFiveHour: CodexQuotaMilestoneRow[] = useDetail
    ? detail!.blocks.flatMap((b) => (b.milestones as CodexQuotaMilestoneRow[]))
    : [];
  const weeklyMilestones = useDetail ? detailWeekly : envWeeklyMilestones;
  const fiveHourMilestones = useDetail ? detailFiveHour : envFiveHourMilestones;
  const currentPercent = useDetail
    ? (weeklyMilestones.length ? weeklyMilestones[weeklyMilestones.length - 1].percent : 0)
    : envCurrentPercent;

  const pct = clamp0_100(currentPercent);
  const [bigInt, bigUnit] = splitBigNum(currentPercent);
  const spent = useDetail
    ? (weeklyMilestones.length ? weeklyMilestones[weeklyMilestones.length - 1].cumulative_usd ?? null : null)
    : hero?.cost_usd ?? null;
  const dpp = spent != null && currentPercent > 0 ? spent / currentPercent : null;
  const weeklyTicks = dedupeTicks(
    weeklyMilestones.map((r) => ({ ...r, percent: r.percent })),
  );

  // Block navigator over the fetched cycle's 5h blocks. Codex carries no
  // per-block envelope stream, so the block view is fully detail-driven for
  // BOTH historic cycles and the current cycle (spec Q2-A): the current
  // cycle's first block-step lazily fetches the cycle detail, then blocks
  // render from the payload (default = last block). The herobar + weekly
  // table stay envelope-driven for the current cycle (`useDetail` is
  // historic-only), so the fetch never disturbs the live big number.
  const currentHasBlocks = !isHistoric && (selectedEntry?.block_count ?? 0) > 0;
  const blocksReady = detail != null && (isHistoric || wantDetail);
  const blocks: WeekDetailBlock[] = blocksReady ? detail!.blocks : [];
  const hasBlocks = blocks.length > 0;
  const activeBlockIndex = !isHistoric
    ? blocks.reduce((found, block, index) => (!block.is_closed ? index : found), -1)
    : -1;
  const defaultBlockIndex = activeBlockIndex >= 0
    ? activeBlockIndex
    : (hasBlocks ? blocks.length - 1 : -1);
  const selectedBlockIndex = (() => {
    if (!hasBlocks) return -1;
    if (blockSel != null) {
      const i = blocks.findIndex((b) => (b.key ?? b.five_hour_window_key) === blockSel);
      if (i >= 0) return i;
    }
    return defaultBlockIndex;
  })();
  useEffect(() => {
    if (pendingBlockStep == null || !blocksReady) return;
    if (hasBlocks) {
      const next = defaultBlockIndex + pendingBlockStep;
      if (next >= 0 && next < blocks.length) {
        const b = blocks[next];
        setBlockSel(b.key ?? b.five_hour_window_key ?? null);
      }
    }
    clearPendingBlockStep();
  }, [
    blocks, blocksReady, clearPendingBlockStep, defaultBlockIndex, hasBlocks,
    pendingBlockStep, setBlockSel,
  ]);
  const stepBlock = (dir: -1 | 1) => {
    // The current cycle's first block-step fetches the cycle payload once; the
    // effect then re-renders with the nav (spec Q2-A).
    if (!isHistoric && !blocksReady) { requestCurrentDetail(dir); return; }
    if (!hasBlocks) return;
    const next = selectedBlockIndex + dir;
    if (next < 0 || next >= blocks.length) return;
    const b = blocks[next];
    setBlockSel(b.key ?? b.five_hour_window_key ?? null);
  };

  const pill = isHistoric && selectedEntry
    ? formatCycleRange(selectedEntry, ctx)
    : (cycle
      ? `${fmt.dateShort(cycle.start_at, ctx)} → ${fmt.dateShort(cycle.resets_at, ctx)}`
      : 'Native 7-day cycle unavailable');
  const resetCell = isHistoric && selectedEntry
    ? selectedEntry.resets_at_utc ?? selectedEntry.end_at_utc
    : cycle?.resets_at;
  const singleId = (value: string) => embedded ? undefined : value;

  // Keyboard (single-provider variants only; embedded registers nothing).
  const bindings = useMemo<Binding[]>(() => {
    const isTopmost = () =>
      topmostStoreFocusLayer(getState()) === 'panel' && getState().openModal === 'current-week';
    return [
      { key: 'ArrowDown', scope: 'modal', when: isTopmost, action: () => { const t = stepWeek(cycleIndex, weekKey, 1); if (t != null) setWeekKey(t); } },
      { key: 'ArrowUp', scope: 'modal', when: isTopmost, action: () => { if (weekKey != null) setWeekKey(stepWeek(cycleIndex, weekKey, -1)); } },
      { key: 'ArrowLeft', scope: 'modal', when: isTopmost, action: () => stepBlock(-1) },
      { key: 'ArrowRight', scope: 'modal', when: isTopmost, action: () => stepBlock(1) },
    ];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cycleIndex, weekKey, selectedBlockIndex, blocks.length, blocksReady]);
  useKeymap(embedded ? EMPTY_BINDINGS : bindings);

  const showShare = weekKey == null;
  const headerExtras = showShare ? (
    <ShareIcon
      panel="current-week"
      panelLabel="Current cycle"
      triggerId="current-week-modal"
      onClick={() => dispatch(openShareModal('current-week', 'current-week-modal'))}
    />
  ) : null;

  if (vanished) {
    return (
      <CurrentWeekShell embedded={embedded} title="Cycle — per-percent milestones" accentClass="accent-orange" headerExtras={headerExtras}>
        <VanishedState onBack={() => setWeekKey(null)} />
      </CurrentWeekShell>
    );
  }

  return (
    <CurrentWeekShell
      embedded={embedded}
      title={isHistoric ? 'Cycle — per-percent milestones' : 'Current Cycle — per-percent milestones'}
      accentClass="accent-orange"
      headerExtras={headerExtras}
    >
      <section className="modal-current-week" data-source="codex">
        {cycleIndex.length > 0 ? (
          <WeekNavChip nav={nav} pillText={pill} accentClass="accent-orange" singleId={singleId} />
        ) : (
          <div className="m-chipstrip" id={singleId('mcw-badges')}>
            <span className="m-pill accent-orange" id={singleId('mcw-week-pill')}>{pill}</span>
            <span className="m-pill accent-orange">Codex · native 7-day quota</span>
          </div>
        )}

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
            <div className="s"><span className="k">spent</span><span className="v v-magenta">{fmt.usd2(spent)}</span></div>
            <div className="s"><span className="k">$ / 1%</span><span className="v v-cyan">{fmt.usd3(dpp)}</span></div>
            <div className="s"><span className="k">reset</span><span className="v">{fmt.datetimeShortZ(resetCell, ctx)}</span></div>
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
        {loading ? (
          <p className="empty-state">Loading…</p>
        ) : error ? (
          <p className="empty-state">
            Couldn’t load this cycle.{' '}
            <button type="button" className="m-pill accent-orange" onClick={() => nav.retry()}>Retry</button>
          </p>
        ) : (
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
        )}

        {!loading && !error && (
          hasBlocks ? (
            <>
              <h3 className="m-sec sec-ms sec-5h">
                <svg className="icon" aria-hidden="true"><use href="/static/icons.svg#activity" /></svg>
                5h blocks
              </h3>
              <BlockNavHeader
                blocks={blocks}
                selectedIndex={selectedBlockIndex}
                onStep={stepBlock}
                ctx={ctx}
                singleId={singleId}
                accentClass="accent-orange"
              />
            </>
          ) : currentHasBlocks ? (
            // Current cycle with retained blocks but no fetched payload yet:
            // render the same complete navigator as historic cycles from the
            // compact index + active-block envelope. The first older step
            // still lazily fetches detail before applying its direction.
            <>
              <h3 className="m-sec sec-ms sec-5h">
                <svg className="icon" aria-hidden="true"><use href="/static/icons.svg#activity" /></svg>
                5h blocks
              </h3>
              <CurrentBlockNavHeader
                blockCount={selectedEntry?.block_count ?? 0}
                block={currentBlockPreview}
                onStep={stepBlock}
                ctx={ctx}
                singleId={singleId}
                accentClass="accent-orange"
              />
            </>
          ) : (
            <p className="mcw-ms-sub">No 5h data retained for this cycle.</p>
          )
        )}
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
  const index: WeekIndexEntry[] = Array.isArray(cw?.week_index) ? cw!.week_index! : [];
  const nav = useMilestoneNav('claude', index);
  const {
    weekKey, setWeekKey, selectedEntry, detail, loading, error, vanished,
    blockSel, setBlockSel, requestCurrentDetail, wantDetail, pendingBlockStep,
    clearPendingBlockStep,
  } = nav;
  const isHistoric = weekKey != null;

  const envMs = Array.isArray(cw?.milestones) ? cw!.milestones : [];
  const fhMs: FiveHourMilestone[] = Array.isArray(cw?.five_hour_milestones)
    ? cw!.five_hour_milestones!
    : [];
  const fhCredits: FiveHourCredit[] = Array.isArray(cw?.five_hour_block?.credits)
    ? cw!.five_hour_block!.credits!
    : [];
  const fhStream = buildFhStream(fhMs, fhCredits);
  const activeWindowKey = cw?.five_hour_block?.five_hour_window_key ?? null;

  // Render from the fetched payload for a historic week, a multi-segment
  // current week, or once a block-step asked for the current week's payload.
  const useDetail = detail != null && (isHistoric || (selectedEntry?.segment_count ?? 0) > 1 || wantDetail);

  const flat = useDetail && detail ? flattenClaudeWeekly(detail) : null;
  const weeklyRows: WeeklyRow[] = flat
    ? flat.rows
    : envMs.map((m) => ({ kind: 'ms', m, key: `ms-${m.percent}` }));
  const weeklyCount = flat ? flat.count : envMs.length;

  // Hero numbers: current → envelope; historic → last segment's last
  // milestone. The override is HISTORIC-only (spec §4): the current week's
  // herobar must stay envelope-driven (live fractional `used_pct` /
  // `spent_usd` / reset) even when its tables render from a fetched detail
  // payload (a credit-split fetch-on-open, or after a block-step) — the
  // fetched payload is active-segment-only-blind and would otherwise clobber
  // the live big number with a stale last-milestone threshold.
  let heroPct: number | null | undefined = cw?.used_pct;
  let spent: number | null | undefined = cw?.spent_usd;
  let dpp: number | null | undefined = cw?.dollar_per_pct;
  let resetCell = cw?.reset_at_utc;
  if (isHistoric && detail) {
    const lastSeg = detail.segments[detail.segments.length - 1];
    const lastMs = (lastSeg?.milestones as Milestone[] | undefined)?.slice(-1)[0];
    heroPct = lastMs?.percent ?? 0;
    spent = lastMs?.cumulative_usd ?? null;
    dpp = spent != null && (heroPct ?? 0) > 0 ? spent / (heroPct as number) : null;
    resetCell = detail.end_at_utc;
  }
  const pct = clamp0_100(heroPct);
  const [bigInt, bigUnit] = splitBigNum(heroPct);
  const weekPillText = isHistoric && selectedEntry
    ? formatCycleRange(selectedEntry, ctx)
    : (cw ? formatWeekWindow(header?.week_label, cw.reset_at_utc, ctx) : '—');
  const ticks = dedupeTicks(
    weeklyRows.filter((r): r is Extract<WeeklyRow, { kind: 'ms' }> => r.kind === 'ms').map((r) => r.m),
  );
  const subText = useDetail ? null : msSub(envMs);
  const singleId = (value: string) => embedded ? undefined : value;

  // Block list: current-default → the envelope active block (rendered as the
  // live fhStream, no nav); historic / fetched → the payload's blocks.
  const payloadBlocks: WeekDetailBlock[] = useDetail && detail ? detail.blocks : [];
  const hasPayloadBlocks = payloadBlocks.length > 0;
  const defaultBlockIndex = (() => {
    if (!hasPayloadBlocks) return -1;
    if (!isHistoric && activeWindowKey != null) {
      const i = payloadBlocks.findIndex((b) => b.five_hour_window_key === activeWindowKey);
      if (i >= 0) return i;
    }
    return payloadBlocks.length - 1; // historic default: last block
  })();
  const selectedBlockIndex = (() => {
    if (!hasPayloadBlocks) return -1;
    if (blockSel != null) {
      const i = payloadBlocks.findIndex((b) => b.five_hour_window_key === blockSel);
      if (i >= 0) return i;
    }
    return defaultBlockIndex;
  })();
  useEffect(() => {
    if (pendingBlockStep == null || !useDetail) return;
    if (hasPayloadBlocks) {
      const next = defaultBlockIndex + pendingBlockStep;
      if (next >= 0 && next < payloadBlocks.length) {
        setBlockSel(payloadBlocks[next].five_hour_window_key ?? null);
      }
    }
    clearPendingBlockStep();
  }, [
    clearPendingBlockStep, defaultBlockIndex, hasPayloadBlocks,
    payloadBlocks, pendingBlockStep, setBlockSel, useDetail,
  ]);
  const selectedBlock = selectedBlockIndex >= 0 ? payloadBlocks[selectedBlockIndex] : null;
  const stepBlock = (dir: -1 | 1) => {
    // On the current week before the payload is fetched, the first block-step
    // fetches the full week payload; the effect then re-renders with the nav.
    if (!isHistoric && !useDetail) { requestCurrentDetail(dir); return; }
    if (!hasPayloadBlocks) return;
    const next = selectedBlockIndex + dir;
    if (next < 0 || next >= payloadBlocks.length) return;
    setBlockSel(payloadBlocks[next].five_hour_window_key ?? null);
  };

  // The selected block's stream: live overlay from the envelope when it IS the
  // active block, otherwise from the fetched block's rows.
  const selectedIsActive = !isHistoric && selectedBlock != null
    && selectedBlock.five_hour_window_key === activeWindowKey;
  const selectedBlockStream = selectedBlock
    ? (selectedIsActive
      ? fhStream
      : buildFhStream(
        (selectedBlock.milestones as FiveHourMilestone[]) ?? [],
        selectedBlock.credits ?? [],
      ))
    : fhStream;

  // Show the full navigator from compact index/live-block facts on the current
  // default; fetching detail remains lazy until the first step.
  const showBlockNav = hasPayloadBlocks;
  const currentBlockCount = !isHistoric && !useDetail
    ? (selectedEntry?.block_count ?? 0)
    : 0;
  const currentHasBlocks = currentBlockCount > 0;
  const currentBlockPreview: BlockNavWindow | null = cw?.five_hour_block
    ? {
      block_start_at: cw.five_hour_block.block_start_at,
      five_hour_resets_at: fiveHoursAfter(cw.five_hour_block.block_start_at),
      crossed_seven_day_reset: cw.five_hour_block.crossed_seven_day_reset,
    }
    : null;

  const bindings = useMemo<Binding[]>(() => {
    const isTopmost = () =>
      topmostStoreFocusLayer(getState()) === 'panel' && getState().openModal === 'current-week';
    return [
      { key: 'ArrowDown', scope: 'modal', when: isTopmost, action: () => { const t = stepWeek(index, weekKey, 1); if (t != null) setWeekKey(t); } },
      { key: 'ArrowUp', scope: 'modal', when: isTopmost, action: () => { if (weekKey != null) setWeekKey(stepWeek(index, weekKey, -1)); } },
      { key: 'ArrowLeft', scope: 'modal', when: isTopmost, action: () => stepBlock(-1) },
      { key: 'ArrowRight', scope: 'modal', when: isTopmost, action: () => stepBlock(1) },
    ];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [index, weekKey, selectedBlockIndex, payloadBlocks.length, useDetail]);
  useKeymap(embedded ? EMPTY_BINDINGS : bindings);

  const showShare = weekKey == null;
  const headerExtras = showShare ? (
    <ShareIcon
      panel="current-week"
      panelLabel="Current week"
      triggerId="current-week-modal"
      onClick={() => dispatch(openShareModal('current-week', 'current-week-modal'))}
    />
  ) : null;

  if (vanished) {
    return (
      <CurrentWeekShell embedded={embedded} title="Week — per-percent milestones" accentClass="accent-green" headerExtras={headerExtras}>
        <VanishedState onBack={() => setWeekKey(null)} />
      </CurrentWeekShell>
    );
  }

  return (
    <CurrentWeekShell
      embedded={embedded}
      title={isHistoric ? 'Week — per-percent milestones' : 'Current Week — per-percent milestones'}
      accentClass="accent-green"
      headerExtras={headerExtras}
    >
      <section className="modal-current-week" data-source="claude">
        {index.length > 0 ? (
          <WeekNavChip nav={nav} pillText={weekPillText} accentClass="accent-green" singleId={singleId} />
        ) : (
          <div className="m-chipstrip" id={singleId('mcw-badges')}>
            <span className="m-pill accent-green" id={singleId('mcw-week-pill')}>{weekPillText}</span>
          </div>
        )}

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
              <span className="v v-magenta" id={singleId('mcw-spent')}>{fmt.usd2(spent)}</span>
            </div>
            <div className="s">
              <span className="k">$ / 1%</span>
              <span className="v v-cyan" id={singleId('mcw-dpp')}>{fmt.usd3(dpp)}</span>
            </div>
            <div className="s">
              <span className="k">reset</span>
              <span className="v" id={singleId('mcw-reset')}>{fmt.datetimeShortZ(resetCell, ctx)}</span>
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
            {weeklyCount} crossed
          </span>
          <span className="mcw-ms-sub" id={singleId('mcw-ms-sub')} hidden={!subText}>
            {subText ?? ''}
          </span>
        </div>
        {loading ? (
          <p className="empty-state">Loading…</p>
        ) : error ? (
          <p className="empty-state" id={singleId('mcw-error')}>
            Couldn’t load this week.{' '}
            <button type="button" className="m-pill accent-green" onClick={() => nav.retry()}>Retry</button>
          </p>
        ) : weeklyCount === 0 ? (
          <p className="empty-state" id={singleId('mcw-empty')}>
            {isHistoric ? 'No milestones recorded this week' : 'No milestones yet — earliest crosses at 1 %.'}
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
              {weeklyRows.map((row) => row.kind === 'divider' ? (
                <tr key={row.key} className="mcw-5h-credit-row">
                  <td colSpan={5} className="mcw-5h-credit-cell">
                    ⚡ CREDIT&nbsp;@{' '}
                    {fmt.startedShort(row.effectiveAt, ctx, { noSuffix: true })}
                    {row.priorPercent != null ? ` · from ${row.priorPercent}%` : ''}
                  </td>
                </tr>
              ) : (
                <tr key={row.key}>
                  <td>
                    <span className="m-pill accent-purple pct-cell">
                      {row.m.percent ?? '—'}
                    </span>
                  </td>
                  <td className="d">
                    {fmt.startedShort(row.m.crossed_at_utc, ctx, { noSuffix: true })}
                  </td>
                  <td className="num">
                    {row.m.cumulative_usd != null ? '$' + row.m.cumulative_usd.toFixed(2) : '—'}
                  </td>
                  <td className="num">
                    <span className="m-marginal">
                      {row.m.marginal_usd != null ? '$' + row.m.marginal_usd.toFixed(2) : '—'}
                    </span>
                  </td>
                  <td className="num">
                    <span className="m-fh">
                      {row.m.five_hour_pct_at_cross != null
                        ? Math.round(row.m.five_hour_pct_at_cross) + '%'
                        : '—'}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {/* 5h milestone timeline. Current-default keeps the live active-block
            stream (spec §5.3); historic / fetched weeks add a block navigator
            over the fetched block list and render the selected block's stream.
            The section stays mounted whenever the effective block list has ≥1
            block (`showBlockNav`) — so stepping onto a milestone-less block (a
            cross-reset straddler) never unmounts the navigator — and equally
            for the current-default navigator (`currentHasBlocks`) or a
            non-empty live stream; only the TABLE below varies (spec §4). */}
        {!loading && !error && (showBlockNav || currentHasBlocks || selectedBlockStream.length > 0) && (
          <>
            <h3 className="m-sec sec-ms sec-5h">
              <svg className="icon" aria-hidden="true">
                <use href="/static/icons.svg#activity" />
              </svg>
              5h milestones
            </h3>
            {showBlockNav ? (
              <BlockNavHeader
                blocks={payloadBlocks}
                selectedIndex={selectedBlockIndex}
                onStep={stepBlock}
                ctx={ctx}
                singleId={singleId}
                accentClass="accent-purple"
              />
            ) : currentHasBlocks ? (
              <CurrentBlockNavHeader
                blockCount={currentBlockCount}
                block={currentBlockPreview}
                onStep={stepBlock}
                ctx={ctx}
                singleId={singleId}
                accentClass="accent-purple"
              />
            ) : (
              <div className="mcw-mshead">
                <span className="m-pill accent-purple" id={singleId('mcw-5h-count')}>
                  {fhMs.length} crossed
                </span>
              </div>
            )}
            {selectedBlockStream.length === 0 ? (
              <p className="empty-state" id={singleId('mcw-5h-empty')}>
                No integer-percent crossings in this block.
              </p>
            ) : (
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
                  {selectedBlockStream.map((ev, i) => {
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
            )}
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
