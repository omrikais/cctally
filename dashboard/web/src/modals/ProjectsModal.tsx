// ProjectsModal — the full Projects panel modal (spec §3, plan Task 5
// Step 6). Layout (top → bottom):
//   1. Header — title "Projects · last Nw" + ShareIcon (passes
//      `{ windowWeeks }` to the share modal state per spec §7.3).
//   2. Window pills (1w / 4w / 8w / 12w) + Y-axis radios (share % vs.
//      $ absolute).
//   3. Optional "Showing N weeks" notice when the snapshot has less
//      history than the requested window.
//   4. Stacked-area trend chart (top-5 + (other) bucket).
//   5. Full 7-column projects table (Project / Sessions / First seen /
//      Last seen / Cost / Used % / % of week) sorted desc by window cost.
//   6. Per-project drill panel — appears below the selected row.
//
// Cross-nav (spec §4.1): when the modal opens with a `projectKey` set
// (e.g. from clicking a row in ProjectsPanel or a project cell in
// SessionsPanel), that row is pre-selected. Otherwise the leader
// (top-1 by current-week cost) is pre-selected. The user can toggle the
// selection on/off by clicking the same row twice.
//
// Drill session row click → `OPEN_MODAL { kind: 'session', sessionId }`
// replaces the Projects modal (no modal stack); same behavior as the
// existing per-panel modals.
import { useEffect, useState, useSyncExternalStore } from 'react';
import { Modal } from './Modal';
import { ProjectsTrendChart } from './ProjectsTrendChart';
import { ProjectsDrillPanel } from './ProjectsDrillPanel';
import { ShareIcon } from '../components/ShareIcon';
import { SortableHeader } from '../components/SortableHeader';
import { SyncChip } from '../components/SyncChip';
import { useSnapshot } from '../hooks/useSnapshot';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { useKeymap } from '../hooks/useKeymap';
import { dispatch, getState, subscribeStore } from '../store/store';
import { openShareModal } from '../store/shareSlice';
import { fmt } from '../lib/fmt';
import { costClass } from '../lib/cost';
import { applyTableSort } from '../lib/tableSort';
import { PROJECTS_COLUMNS, type ProjectsTableRow } from '../lib/projectsColumns';

type WindowPill = 1 | 4 | 8 | 12;
const WINDOW_PILLS: readonly WindowPill[] = [1, 4, 8, 12];

export function ProjectsModal() {
  const env = useSnapshot();
  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  const projectKey = useSyncExternalStore(subscribeStore, () => getState().openProjectKey);
  const windowWeeks = useSyncExternalStore(
    subscribeStore,
    () => getState().prefs.projectsWindowWeeks,
  );
  const yMode = useSyncExternalStore(
    subscribeStore,
    () => getState().prefs.projectsTrendYMode,
  );
  const sortOverride = useSyncExternalStore(
    subscribeStore,
    () => getState().prefs.projectsSortOverride,
  );
  const [selectedKey, setSelectedKey] = useState<string | null>(projectKey ?? null);
  // Collapse the projects table to the top-N active projects by default;
  // a real cache can carry 30+ historical projects, most of which are
  // $0.00 in any given window — dumping all of them into the modal makes
  // it tall and noisy. Expand reveals the rest (inactive + tail).
  const [tableExpanded, setTableExpanded] = useState(false);

  // Re-bind selected key when the modal opens with a different
  // `openProjectKey` (cross-nav from panel/sessions) or when the
  // snapshot's leader changes and no cross-nav target is set.
  useEffect(() => {
    if (projectKey) {
      setSelectedKey(projectKey);
      return;
    }
    if (selectedKey) return;
    const leader = env?.projects?.current_week?.rows?.[0]?.key ?? null;
    setSelectedKey(leader);
    // We intentionally depend on the rows reference rather than
    // selectedKey: re-running on every selectedKey change would clobber
    // the user's manual click-to-toggle interaction.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectKey, env?.projects?.current_week?.rows]);

  const trend = env?.projects?.trend ?? null;
  const requested = windowWeeks;
  const actual = trend?.window_weeks ?? 0;

  const savePref = <K extends 'projectsWindowWeeks' | 'projectsTrendYMode'>(
    key: K,
    value: K extends 'projectsWindowWeeks' ? WindowPill : 'share' | 'absolute',
  ): void => {
    dispatch({ type: 'SAVE_PREFS', patch: { [key]: value } });
  };

  const onRowClick = (key: string) => {
    setSelectedKey((prev) => (prev === key ? null : key));
  };

  // Window-aware project rows: window-summed cost + window-summed
  // attributed pct + window-summed sessions + window min/max of
  // first/last seen + derived % of week. Default sort is desc by window
  // cost (spec §3.4); a click on any `SortableHeader` cell persists an
  // override at `prefs.projectsSortOverride` and routes through
  // `applyTableSort`. % of week = `windowCost / sum(windowCost)` over
  // the active window — the per-row differential signal that replaces
  // the degenerate v1 `$/1%` column (issue #72).
  // Top-N active projects shown by default; rest behind an expand toggle.
  // "Active" = nonzero window cost. The default cost-desc sort means the
  // top-N active are simply the leading rows after filtering out
  // zero-cost entries; under an override the user-chosen order is
  // applied AFTER the active-filter so they still only see the top
  // ACTIVE_COLLAPSE_LIMIT rows when collapsed.
  const ACTIVE_COLLAPSE_LIMIT = 10;
  const baseRows: ProjectsTableRow[] = (trend?.projects ?? [])
    .map((p) => {
      const weeklyCost = p.weekly_cost.slice(-windowWeeks);
      const weeklyPct = p.weekly_pct.slice(-windowWeeks);
      const weeklySessions = p.sessions_per_week.slice(-windowWeeks);
      const weeklyFirstSeen = p.first_seen_per_week.slice(-windowWeeks);
      const weeklyLastSeen = p.last_seen_per_week.slice(-windowWeeks);
      const windowCost = weeklyCost.reduce((s, c) => s + c, 0);
      const windowPct = weeklyPct.reduce<number | null>(
        (s, c) => (c == null ? s : (s ?? 0) + c),
        null,
      );
      // Per-week counts double-count cross-week sessions (rare in
      // practice — a single Claude session typically stays inside one
      // ISO Monday boundary). Matches the share-flow's window sum at
      // `_build_share_projects_envelope` in bin/_cctally_dashboard.py.
      const sessionsCount = weeklySessions.reduce((s, c) => s + c, 0);
      const firstSeenAt = weeklyFirstSeen.reduce<string | null>(
        (acc, ts) => (ts == null ? acc : acc == null || ts < acc ? ts : acc),
        null,
      );
      const lastSeenAt = weeklyLastSeen.reduce<string | null>(
        (acc, ts) => (ts == null ? acc : acc == null || ts > acc ? ts : acc),
        null,
      );
      return {
        key: p.key,
        sessionsCount,
        firstSeenAt,
        lastSeenAt,
        windowCost,
        windowPct,
        // shareOfWindow filled in a second pass below — needs the
        // total across all rows first.
        shareOfWindow: null as number | null,
      };
    });
  // Second pass: fill shareOfWindow now that totalWindowCost is known.
  // Stored as 0–100 so `fmt.pct0` renders directly (matches `windowPct`).
  const totalWindowCost = baseRows.reduce((s, r) => s + r.windowCost, 0);
  for (const r of baseRows) {
    r.shareOfWindow =
      totalWindowCost > 0 ? (r.windowCost / totalWindowCost) * 100 : null;
  }

  // Apply override when set; otherwise fall back to cost-desc (spec §3.4
  // "Default sort: cost desc"). `applyTableSort` does NOT mutate its
  // input — slice() inside.
  const tableRows: ProjectsTableRow[] = sortOverride
    ? applyTableSort(baseRows, PROJECTS_COLUMNS, sortOverride)
    : applyTableSort(baseRows, PROJECTS_COLUMNS, {
        column: 'cost',
        direction: 'desc',
      });

  const activeRows = tableRows.filter((r) => r.windowCost > 0);
  const collapsedRows = activeRows.slice(0, ACTIVE_COLLAPSE_LIMIT);
  const hiddenWhenCollapsed = tableRows.length - collapsedRows.length;
  const visibleRows = tableExpanded ? tableRows : collapsedRows;
  const canExpand = hiddenWhenCollapsed > 0;

  // Spec §3.7 keyboard bindings. Bindings re-register each render so the
  // closures capture the latest selectedKey / visibleRows / windowWeeks
  // / yMode — useKeymap accepts the re-registration cost in exchange
  // for not needing refs for every captured value (its docstring
  // explicitly allows this trade-off). `0` maps to 12w by convention
  // (`0` reads as "max").
  useKeymap([
    { key: '1', scope: 'modal', action: () => savePref('projectsWindowWeeks', 1) },
    { key: '4', scope: 'modal', action: () => savePref('projectsWindowWeeks', 4) },
    { key: '8', scope: 'modal', action: () => savePref('projectsWindowWeeks', 8) },
    { key: '0', scope: 'modal', action: () => savePref('projectsWindowWeeks', 12) },
    {
      key: 's',
      scope: 'modal',
      action: () =>
        savePref('projectsTrendYMode', yMode === 'share' ? 'absolute' : 'share'),
    },
    {
      key: 'ArrowUp',
      scope: 'modal',
      action: () => {
        if (visibleRows.length === 0) return;
        const idx = visibleRows.findIndex((r) => r.key === selectedKey);
        const next = idx <= 0 ? visibleRows.length - 1 : idx - 1;
        setSelectedKey(visibleRows[next].key);
      },
    },
    {
      key: 'ArrowDown',
      scope: 'modal',
      action: () => {
        if (visibleRows.length === 0) return;
        const idx = visibleRows.findIndex((r) => r.key === selectedKey);
        const next = idx === -1 || idx === visibleRows.length - 1 ? 0 : idx + 1;
        setSelectedKey(visibleRows[next].key);
      },
    },
    {
      key: 'Enter',
      scope: 'modal',
      action: () => {
        if (selectedKey) {
          setSelectedKey(null);
        } else if (visibleRows.length > 0) {
          setSelectedKey(visibleRows[0].key);
        }
      },
    },
  ]);

  return (
    <Modal
      title={`Projects · last ${windowWeeks}w`}
      accentClass="accent-magenta"
      headerExtras={
        <ShareIcon
          panel="projects"
          panelLabel="Projects"
          triggerId="projects-modal"
          dataTestId="share-icon-projects-modal"
          onClick={() =>
            dispatch(openShareModal('projects', 'projects-modal', { windowWeeks }))
          }
        />
      }
    >
      <div className="projects-modal-body">
        <div className="projects-controls" role="radiogroup" aria-label="Window">
          {WINDOW_PILLS.map((w) => (
            <button
              key={`window-${w}`}
              type="button"
              role="radio"
              aria-checked={windowWeeks === w}
              className={`pill ${windowWeeks === w ? 'on' : ''}`}
              onClick={() => savePref('projectsWindowWeeks', w)}
            >
              {w}w
            </button>
          ))}
          <span className="sep" aria-hidden="true">|</span>
          <button
            type="button"
            role="radio"
            aria-checked={yMode === 'share'}
            className={`pill ${yMode === 'share' ? 'on' : ''}`}
            onClick={() => savePref('projectsTrendYMode', 'share')}
          >
            share %
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={yMode === 'absolute'}
            className={`pill ${yMode === 'absolute' ? 'on' : ''}`}
            onClick={() => savePref('projectsTrendYMode', 'absolute')}
          >
            $ absolute
          </button>
        </div>

        {actual > 0 && actual < requested && (
          <div className="projects-notice">
            Showing {actual} week{actual === 1 ? '' : 's'} (need more history for the full window).
          </div>
        )}

        {trend ? (
          <ProjectsTrendChart
            trend={trend}
            yMode={yMode}
            windowWeeks={windowWeeks}
            onProjectSelect={(key) => setSelectedKey(key)}
          />
        ) : (
          <div className="panel-empty">Projects trend unavailable.</div>
        )}

        <table className="projects-table">
          <SortableHeader
            columns={PROJECTS_COLUMNS}
            override={sortOverride}
            onChange={(next) =>
              dispatch({ type: 'SET_TABLE_SORT', table: 'projects', override: next })
            }
            accentVar="--accent-magenta"
          />
          <tbody>
            {visibleRows.map((r) => (
              <tr
                key={r.key}
                data-testid="projects-table-row"
                data-cost={r.windowCost}
                data-sessions={r.sessionsCount}
                aria-expanded={selectedKey === r.key}
                className={selectedKey === r.key ? 'selected' : ''}
                onClick={() => onRowClick(r.key)}
              >
                <td className="project">{r.key}</td>
                <td>{r.sessionsCount}</td>
                <td className="started">{fmt.dateShort(r.firstSeenAt, ctx) ?? '—'}</td>
                <td className="started">{fmt.dateShort(r.lastSeenAt, ctx) ?? '—'}</td>
                <td className={costClass(r.windowCost)}>{fmt.usd2(r.windowCost)}</td>
                <td>{r.windowPct == null ? '—' : fmt.pct0(r.windowPct)}</td>
                <td>{r.shareOfWindow == null ? '—' : fmt.pct0(r.shareOfWindow)}</td>
              </tr>
            ))}
          </tbody>
        </table>

        {canExpand && (
          <button
            type="button"
            className="projects-table-toggle"
            data-testid="projects-table-toggle"
            aria-expanded={tableExpanded}
            onClick={() => setTableExpanded((v) => !v)}
          >
            {tableExpanded
              ? `Show top ${ACTIVE_COLLAPSE_LIMIT} active`
              : `Show all ${tableRows.length} projects (+${hiddenWhenCollapsed})`}
          </button>
        )}

        {selectedKey && (
          <ProjectsDrillPanel projectKey={selectedKey} windowWeeks={windowWeeks} />
        )}

        <div
          className="projects-modal-footer-hint"
          data-testid="projects-modal-footer-hint"
          aria-live="off"
        >
          <span><kbd>1</kbd>/<kbd>4</kbd>/<kbd>8</kbd>/<kbd>0</kbd> window</span>
          <span className="sep" aria-hidden="true">·</span>
          <span><kbd>↑↓</kbd> row</span>
          <span className="sep" aria-hidden="true">·</span>
          <span><kbd>Enter</kbd> drill</span>
          <span className="sep" aria-hidden="true">·</span>
          <span><kbd>s</kbd> share/$</span>
          <span className="sep" aria-hidden="true">·</span>
          <span><kbd>Esc</kbd> close</span>
          <span className="sep" aria-hidden="true">·</span>
          <SyncChip />
        </div>
      </div>
    </Modal>
  );
}
