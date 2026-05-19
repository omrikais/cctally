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
//      Last seen / Cost / Used % / $/1%) sorted desc by window cost.
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
import { useSnapshot } from '../hooks/useSnapshot';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { dispatch, getState, subscribeStore } from '../store/store';
import { openShareModal } from '../store/shareSlice';
import { fmt } from '../lib/fmt';

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
  // attributed pct + derived $/1%. Sorted desc by window cost (spec
  // §3.4 — default sort).
  type DerivedRow = {
    key: string;
    sessions_count_12w: number;
    first_seen_at: string | null;
    last_seen_at: string | null;
    windowCost: number;
    windowPct: number | null;
    dollarsPerPct: number | null;
  };
  // Top-N active projects shown by default; rest behind an expand toggle.
  // "Active" = nonzero window cost. Spec §3.4's default sort is desc by
  // cost, so the top-N active are simply the leading rows of the sorted
  // table after filtering out zero-cost entries.
  const ACTIVE_COLLAPSE_LIMIT = 10;
  const tableRows: DerivedRow[] = (trend?.projects ?? [])
    .map((p) => {
      const weeklyCost = p.weekly_cost.slice(-windowWeeks);
      const weeklyPct = p.weekly_pct.slice(-windowWeeks);
      const windowCost = weeklyCost.reduce((s, c) => s + c, 0);
      const windowPct = weeklyPct.reduce<number | null>(
        (s, c) => (c == null ? s : (s ?? 0) + c),
        null,
      );
      const dpp =
        windowPct != null && windowPct > 0 ? windowCost / windowPct : null;
      return {
        key: p.key,
        sessions_count_12w: p.sessions_count_12w,
        first_seen_at: p.first_seen_at,
        last_seen_at: p.last_seen_at,
        windowCost,
        windowPct,
        dollarsPerPct: dpp,
      };
    })
    .sort((a, b) => b.windowCost - a.windowCost);

  const activeRows = tableRows.filter((r) => r.windowCost > 0);
  const collapsedRows = activeRows.slice(0, ACTIVE_COLLAPSE_LIMIT);
  const hiddenWhenCollapsed = tableRows.length - collapsedRows.length;
  const visibleRows = tableExpanded ? tableRows : collapsedRows;
  const canExpand = hiddenWhenCollapsed > 0;

  return (
    <Modal
      title={`Projects · last ${windowWeeks}w`}
      accentClass="accent-magenta"
      headerExtras={
        <span data-testid="share-icon-projects-modal">
          <ShareIcon
            panel="projects"
            panelLabel="Projects"
            triggerId="projects-modal"
            onClick={() =>
              dispatch(openShareModal('projects', 'projects-modal', { windowWeeks }))
            }
          />
        </span>
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
          <thead>
            <tr>
              <th>Project</th>
              {/*
                I2 (cross-branch review): these three columns read
                window-unaware envelope fields (`sessions_count_12w`,
                `first_seen_at`, `last_seen_at`) — they do NOT change
                when the 1w / 4w / 8w / 12w pill flips. Spec §3.4 wants
                them window-scoped, but that requires per-week sessions
                counts + first/last-seen in the envelope shape, too
                large for this commit. Widen the labels until the
                envelope-shape follow-up lands (tracking: cctally-dev
                issue).
              */}
              <th>Sessions (12w)</th>
              <th>First seen (all-time)</th>
              <th>Last seen (all-time)</th>
              <th>Cost ▼</th>
              <th>Used %</th>
              <th>$/1%</th>
            </tr>
          </thead>
          <tbody>
            {visibleRows.map((r) => (
              <tr
                key={r.key}
                data-testid="projects-table-row"
                data-cost={r.windowCost}
                aria-expanded={selectedKey === r.key}
                className={selectedKey === r.key ? 'selected' : ''}
                onClick={() => onRowClick(r.key)}
              >
                <td>{r.key}</td>
                <td>{r.sessions_count_12w}</td>
                <td>{fmt.dateShort(r.first_seen_at, ctx) ?? '—'}</td>
                <td>{fmt.dateShort(r.last_seen_at, ctx) ?? '—'}</td>
                <td>{fmt.usd2(r.windowCost)}</td>
                <td>{r.windowPct == null ? '—' : fmt.pct0(r.windowPct)}</td>
                <td>{r.dollarsPerPct == null ? '—' : fmt.usd2(r.dollarsPerPct)}</td>
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
      </div>
    </Modal>
  );
}
