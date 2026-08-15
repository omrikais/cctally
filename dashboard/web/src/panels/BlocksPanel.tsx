import { useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react';
import { scopeProviderFor, useAccountScope, useScopedSnapshot } from '../hooks/useScopedSnapshot';
import { PanelGrip } from '../components/PanelGrip';
import { PanelSkeleton } from '../components/PanelSkeleton';
import { ShareIcon } from '../components/ShareIcon';
import { ExpandButton } from '../components/ExpandButton';
import { ModelLegend } from '../components/ModelLegend';
import { fmt } from '../lib/fmt';
import { modelChipStyle } from '../lib/model';
import { dispatch, getState, subscribeStore } from '../store/store';
import { sourceAccounts } from '../store/accountFocus';
import { resolveSourceView } from '../store/sourceView';
import { openShareModal } from '../store/shareSlice';
import {
  blocksDisplayedSpan,
  blocksFooterLegs,
  presentationBlocks,
  presentationProviders,
  type BlockPresentationRow,
} from '../lib/dashboardPresentation';
import { formatSpan } from '../lib/projectWindow';
import { useDisplayTz } from '../hooks/useDisplayTz';
import type { CodexSourceData } from '../types/envelope';

function openBlockDetail(r: BlockPresentationRow): void {
  if (r.source === 'claude') {
    dispatch({ type: 'OPEN_MODAL', kind: 'block', blockStartAt: r.start_at });
  } else {
    dispatch({ type: 'OPEN_SOURCE_DETAIL', source: r.source, resource: 'block', key: r.key });
  }
}

function Row({
  r,
  maxCost,
  isFirstMount,
  showSource,
  accountLabel,
}: {
  r: BlockPresentationRow;
  maxCost: number;
  isFirstMount: boolean;
  showSource: boolean;
  accountLabel: string | null;
}) {
  const fillPct = maxCost > 0 ? (r.value / maxCost) * 100 : 0;
  const open = () => openBlockDetail(r);
  return (
    <div
      className="blocks-row"
      role="button"
      tabIndex={0}
      aria-label={
        accountLabel == null
          ? `Open detail for block starting ${r.label}`
          : `Open detail for ${accountLabel} block starting ${r.label}`
      }
      onClick={open}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          open();
        }
      }}
    >
      <div className="meta">
        <span className="label">
          {showSource && (
            <span className={`source-chip source-chip--${r.source}`}>{r.source === 'claude' ? 'Claude' : 'Codex'}</span>
          )}
          {/* #416 QA P1-A — the merged list now carries every account's
              windows, so two otherwise identical rows can belong to different
              accounts. Under focus (or below two real accounts) there is
              exactly one owner and no chip is rendered. */}
          {accountLabel != null && (
            <span
              className="blocks-account-chip"
              data-testid="block-account-chip"
              title={`5-hour window owned by ${accountLabel}`}
            >
              {accountLabel}
            </span>
          )}
          {r.anchor === 'heuristic' && (
            <span className="anchor-marker" aria-label="approximate start">~</span>
          )}
          {r.label}
          {r.is_active && <span className="pill-active">Active</span>}
        </span>
        <span className="cost">{r.valueLabel}</span>
      </div>
      <div className="gauge-track">
        <div
          className="gauge-fill"
          // First paint of a row animates from 0 → target width;
          // subsequent SSE updates render straight to target.
          style={{ width: isFirstMount ? '0%' : `${fillPct}%` }}
        >
          {r.models.map((m) => (
            <span
              key={m.model}
              className={`seg-${m.chip}`}
              style={{ ...modelChipStyle(m.model), width: `${m.cost_pct}%` }}
              title={`${m.display} ${fmt.usd2(m.cost_usd)} (${m.cost_pct.toFixed(0)}%)`}
            />
          ))}
        </div>
      </div>
      <ModelLegend models={r.models} />
    </div>
  );
}

// #294 S5 — source-aware wrapper. Both providers render real 5h activity
// blocks; Codex boundaries come from its durable native 300-minute windows.
export function BlocksPanel() {
  const env = useScopedSnapshot();
  const activeSource = useSyncExternalStore(subscribeStore, () => getState().activeSource);
  const collapsed = useSyncExternalStore(
    subscribeStore,
    () => getState().prefs.blocksCollapsed,
  );
  // #264 S4 (A2): render ALL blocks; the bento card scrolls internally (A1) so
  // every block is reachable (the old #248 slice(0,3) summary-cap hid blocks
  // 4..N with no view for them). `maxCost` still spans the full week so every
  // bar keeps its true scale vs the week's peak; the footer count + total
  // already summarize the whole set (each row still opens its own Block modal).
  const allRows = presentationBlocks(env, activeSource);
  const rows = allRows;
  const codexEntry = resolveSourceView(env, 'codex').entry;
  const codex = codexEntry?.data as CodexSourceData | undefined;
  // #373: a foreign quota pool's 5h window is not the account's, so it must
  // not claim the account has a 5-hour limit at all.
  const codexHasFiveHourWindow = codex?.quota.histories.some(
    (row) => row.window_minutes === 300 && !row.model_scoped,
  ) ?? false;
  // #416 QA P1-A — key -> label for the merged Codex rows. Built only when the
  // provider is decorated AND no chip is focused; a focused view has exactly
  // one owner and an undecorated envelope ships no `accounts[]` at all, so both
  // keep today's unlabelled rows (R8).
  // #556 S5 §5.4 — under All the focus that can narrow these rows is CODEX's,
  // so the stated rule (decorated AND no chip focused) now holds on that tab
  // too: a focused view has exactly one owner and needs no per-row label.
  const scope = useAccountScope(activeSource, scopeProviderFor(activeSource));
  const showCodexAccountLabels = activeSource === 'all'
    ? sourceAccounts(codexEntry) != null && scope.accountKey == null
    : scope.scopesSupported && scope.accountKey == null;
  const codexAccountLabels = showCodexAccountLabels
    ? new Map((codex?.accounts ?? []).map((card) => [card.accountKey, card.label]))
    : null;
  const accountLabelFor = (row: BlockPresentationRow): string | null => (
    row.source === 'codex' && row.accountKey != null && codexAccountLabels != null
      ? codexAccountLabels.get(row.accountKey) ?? null
      : null
  );
  // #556 S2 QA — the string is unchanged; it renders somewhere else. Inside
  // the h2 this was the worst truncation on the board: at 390px clientWidth
  // 126 against scrollWidth 267, 47% visible, so `Blocks (5h · current
  // provider cycles)` rendered as "Blocks (5h · curren…" and the composition
  // was exactly what was hidden. It now renders on `.panel-range-note`, the
  // wrapping full-width sub-line, and the h2 carries the panel name alone.
  // Splitting it instead — unit in the h2, cycle on the note — was measured
  // and rejected: `Blocks (optional 5h)`, the Codex-without-a-native-window
  // form, still needed 161px against the 126px the actions cluster leaves,
  // so the unit itself would clip at 78%.
  const blocksScope = activeSource === 'codex'
    ? `${codexHasFiveHourWindow ? '5h' : 'optional 5h'} · current cycle`
    : activeSource === 'all'
      ? '5h · current provider cycles'
      : '5h · current week';
  const maxCost = allRows.length > 0 ? Math.max(...allRows.map((r) => r.value), 0) : 0;
  // Compatible provider costs can be combined once in All mode. These rows
  // are already source-qualified, so summing their displayed values preserves
  // the no-double-count invariant while keeping Codex-only totals truthful.
  const total = allRows.reduce((sum, row) => sum + row.value, 0);
  const hasHeuristic = rows.some((r) => r.anchor === 'heuristic');
  const display = useDisplayTz();
  // #556 S2 §6.4 — the summed total stays (the blocks contract requires the
  // footer to equal the sum of the displayed rows) and gains the attribution
  // and the coverage it never stated. Coverage is the interval the DISPLAYED
  // rows span, never a claim of continuous coverage and never a shared cycle:
  // the two providers run independent five-hour clocks.
  const footerLegs = activeSource === 'all' ? blocksFooterLegs(allRows) : null;
  // Clamped to the snapshot instant: an ACTIVE block's `end_at` is its
  // projected reset, so the coverage line would otherwise name a future hour
  // as an interval windows were shown for.
  const displayedSpan = activeSource === 'all'
    ? formatSpan(
        blocksDisplayedSpan(allRows),
        { tz: display.resolvedTz, offsetLabel: display.offsetLabel },
        { clampEndTo: env?.generated_at },
      )
    : null;

  // First-mount animation: paint .gauge-fill width:0, then rAF flips to
  // target width so the CSS transition interpolates. Spec §2.5.
  const seenStarts = useRef<Set<string>>(new Set());
  const [, forceRender] = useState(0);
  useLayoutEffect(() => {
    const newRows = rows.filter((r) => !seenStarts.current.has(r.start_at));
    if (newRows.length === 0) return;
    newRows.forEach((r) => seenStarts.current.add(r.start_at));
    const id = requestAnimationFrame(() => forceRender((n) => n + 1));
    return () => cancelAnimationFrame(id);
  }, [rows]);

  return (
    <section
      className={'panel accent-blue' + (collapsed ? ' blocks-collapsed' : '')}
      id="panel-blocks"
      role="region"
      aria-label="Blocks panel"
      data-panel-kind="blocks"
      data-source={activeSource}
    >
      <div className="panel-header" style={{ justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <svg className="icon" aria-hidden="true">
            <use href="/static/icons.svg#layers" />
          </svg>
          <h2>Blocks</h2>
        </div>
        <div className="panel-header-actions">
          <ShareIcon
            panel="blocks"
            panelLabel="5-hour blocks"
            triggerId="blocks-panel"
            onClick={() => dispatch(openShareModal('blocks', 'blocks-panel'))}
          />
          <ExpandButton
            label="Blocks"
            onOpen={() => {
              const row = allRows.find((item) => item.is_active) ?? allRows[0];
              if (!row) return;
              openBlockDetail(row);
            }}
            disabled={allRows.length === 0}
          />
          <button
            type="button"
            className="panel-collapse-toggle"
            aria-expanded={!collapsed}
            aria-controls="panel-blocks-body"
            aria-label={collapsed ? 'Expand Blocks' : 'Collapse Blocks'}
            title={collapsed ? 'Expand' : 'Collapse'}
            onClick={(e) => {
              e.stopPropagation();
              dispatch({
                type: 'SAVE_PREFS',
                patch: { blocksCollapsed: !collapsed },
              });
            }}
          >
            <svg className="icon" aria-hidden="true">
              <use href={`/static/icons.svg#${collapsed ? 'chevron-down' : 'chevron-up'}`} />
            </svg>
          </button>
          <PanelGrip />
        </div>
      </div>
      {/* The window unit and the cycle the displayed rows come from. It
          renders on EVERY tab, not only under All: the statement exists on
          every tab and the h2 truncated on every tab, so one panel keeps one
          pattern. It stays outside the collapsible body, because it describes
          the title rather than the rows. */}
      <div className="panel-range-note">{blocksScope}</div>
      <div className="panel-body" id="panel-blocks-body">
        {rows.length === 0 ? (
            presentationProviders(env, activeSource).hydrating ? (
            <PanelSkeleton />
          ) : (
            <div className="panel-empty">
              {activeSource === 'codex'
                ? codexHasFiveHourWindow
                  ? 'No 5-hour activity blocks in the current Codex cycle.'
                  : 'No native 5-hour window is currently reported; the 7-day Codex cycle remains available.'
                : activeSource === 'all'
                  ? codexHasFiveHourWindow
                    ? 'No 5-hour activity blocks in the current provider cycles.'
                    : 'No Claude 5-hour activity blocks; Codex is not currently reporting an optional 5-hour window.'
                  : 'No activity blocks this week yet.'}
            </div>
          )
        ) : (
          rows.map((r) => (
            <Row
              key={r.key}
              r={r}
              maxCost={maxCost}
              isFirstMount={!seenStarts.current.has(r.start_at)}
              showSource={activeSource === 'all'}
              accountLabel={accountLabelFor(r)}
            />
          ))
        )}
      </div>
      {allRows.length > 0 && (
        <div className="panel-foot">
          <span>
            {allRows.length} blocks
            <span className="sep" aria-hidden="true"> · </span>
            <span className="total">{fmt.usd2(total)}</span>
            {hasHeuristic && (
              <>
                <span className="sep" aria-hidden="true"> · </span>
                <span className="legend-anchor">~ = approximate start</span>
              </>
            )}
          </span>
          {footerLegs && (
            <span className="blocks-foot-attribution">
              {displayedSpan && (
                <>
                  <span className="blocks-foot-span">
                    windows shown span {displayedSpan}
                  </span>
                  <span className="sep" aria-hidden="true"> · </span>
                </>
              )}
              {footerLegs.map((leg, index) => (
                <span key={leg.source} className="blocks-foot-leg">
                  {index > 0 && <span className="sep" aria-hidden="true"> · </span>}
                  {leg.label} {leg.count} {leg.count === 1 ? 'block' : 'blocks'}{' '}
                  {fmt.usd2(leg.cost)}
                </span>
              ))}
            </span>
          )}
        </div>
      )}
    </section>
  );
}
