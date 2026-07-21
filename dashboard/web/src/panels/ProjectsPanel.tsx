// ProjectsPanel — top-5 horizontal-bar leaderboard for the current
// subscription week. Click a row to open the Projects modal pre-expanded
// on that project; click the panel chrome to open un-targeted. See spec
// §5.2 (envelope shape) and §4.1 (cross-nav routing).
//
// Empty states (spec §4.3):
//   - projects envelope null   → "Projects data unavailable" panel-empty.
//   - rows array empty         → "No project activity yet this week".
//
// null `attributed_pct` renders as em-dash (— ) — the week's total cost
// is zero so attribution is undefined; mirrors the kernel's null
// emission.
import { useSyncExternalStore, type CSSProperties, type MouseEvent } from 'react';
import { useSnapshot } from '../hooks/useSnapshot';
import { dispatch, getState, subscribeStore } from '../store/store';
import { PanelGrip } from '../components/PanelGrip';
import { PanelSkeleton } from '../components/PanelSkeleton';
import { ShareIcon } from '../components/ShareIcon';
import { ExpandButton } from '../components/ExpandButton';
import { openShareModal } from '../store/shareSlice';
import { cardRegionClick } from '../lib/cardRegion';
import { fmt } from '../lib/fmt';
import { presentationProjects, presentationProviders } from '../lib/dashboardPresentation';
import { warningForDomain } from '../lib/sourceGating';
import { DegradedChip } from './sourcePanel';

const TOP_N = 5;

// #294 S5 — source-aware wrapper. Claude = legacy leaderboard (unchanged);
// Codex = native qualified-attribution table; All = provider sections
// (identical labels across providers stay distinct rows — different keys).
export function ProjectsPanel() {
  const env = useSnapshot();
  const activeSource = useSyncExternalStore(subscribeStore, () => getState().activeSource);
  const projectedRows = presentationProjects(env, activeSource);
  const rows = projectedRows ?? [];
  const isUnavailable = projectedRows == null;
  // #278 §1.4: during the cheap first-paint seed the projects envelope is
  // still null / empty; show a loading skeleton instead of the "restart the
  // dashboard" / "no activity" copy, which would wrongly imply a broken instance.
  const hydrating = presentationProviders(env, activeSource).hydrating;
  const projectWarning = warningForDomain(
    presentationProviders(env, activeSource).warnings,
    'projects',
  );
  // #278 Theme A (ui-qa P3): mirror CacheReportPanel's header — while hydrating
  // with no data yet, the sub-label reads "(loading)" instead of the misleading
  // final-state "(unavailable)" (which re-implies a broken instance) or
  // "(0 this week)". Flips off automatically once the panel hydrates.
  const showLoadingSub = hydrating && rows.length === 0;

  // ShareIcon + PanelGrip render in BOTH the populated and the
  // unavailable-envelope branches per spec §2.6 ("ShareIcon still
  // visible"). The header parity also restores the folder icon and
  // row-count sub-span across both branches; only the panel-body
  // content varies.
  const header = (
    <div className="panel-header" style={{ justifyContent: 'space-between' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
        <svg className="icon" aria-hidden="true">
          <use href="/static/icons.svg#folder" />
        </svg>
        <h2>
          Projects{' '}
          <span className="sub">
            {showLoadingSub
              ? '(loading)'
              : isUnavailable
                ? '(unavailable)'
                : `(${rows.length} this week)`}
          </span>
        </h2>
        {projectWarning && <DegradedChip gate={{ mode: 'degraded', warning: projectWarning, noSuccessYet: false }} />}
      </div>
      <div className="panel-header-actions">
        <ShareIcon
          panel="projects"
          panelLabel="Projects"
          triggerId="projects-panel"
          onClick={() =>
            dispatch(openShareModal('projects', 'projects-panel'))
          }
        />
        <ExpandButton
          label="Projects"
          onOpen={() => dispatch({ type: 'OPEN_MODAL', kind: 'projects' })}
        />
        <PanelGrip />
      </div>
    </div>
  );

  if (isUnavailable) {
    return (
      <section
        className="panel accent-magenta"
        id="panel-projects"
        data-panel-kind="projects"
        role="region"
        aria-label="Projects panel"
      >
        {header}
        <div className="panel-body projects-body">
          {hydrating ? (
            <PanelSkeleton />
          ) : (
            <div className="panel-empty">
              Projects data unavailable — restart the dashboard.
            </div>
          )}
        </div>
      </section>
    );
  }

  const top = rows.slice(0, TOP_N);
  const tail = rows.slice(TOP_N);
  const tailCost = tail.reduce((s, r) => s + r.cost, 0);
  // tailPctRaw treats null attributed_pct as 0 — fine for "+N more"
  // rollup semantics where the sum represents the visible share of
  // attributed_pct (null rows by definition contribute no attribution).
  const tailPctRaw = tail.reduce<number>((s, r) => s + (r.pct ?? 0), 0);
  // div-by-zero guard: when the top row's cost is 0 the bar widths
  // collapse to 0% (visually empty); never divide by 0 directly.
  const leaderCost = top[0]?.cost || 1;

  const onPanelClick = () => {
    dispatch({ type: 'OPEN_MODAL', kind: 'projects' });
  };

  const openRow = (source: 'claude' | 'codex', key: string) => {
    if (source === 'claude' && activeSource === 'claude') {
      dispatch({ type: 'OPEN_MODAL', kind: 'projects', projectKey: key });
    } else {
      dispatch({ type: 'OPEN_SOURCE_DETAIL', source, resource: 'project', key });
    }
  };
  const onRowClick = (source: 'claude' | 'codex', key: string) => (e: MouseEvent) => {
    e.stopPropagation();
    openRow(source, key);
  };

  return (
    <section
      className="panel accent-magenta"
      id="panel-projects"
      data-panel-kind="projects"
      data-source={activeSource}
      role="region"
      aria-label="Projects panel"
      onClick={cardRegionClick(onPanelClick)}
    >
      {header}
      <div className="panel-body projects-body">
        {rows.length === 0 ? (
          hydrating ? (
            <PanelSkeleton />
          ) : (
            <div className="panel-empty">
              No project activity yet this week.
            </div>
          )
        ) : (
          <>
            {top.map((r) => {
              const widthPct = (r.cost / leaderCost) * 100;
              const barStyle = { '--w': `${widthPct}%` } as CSSProperties;
              return (
                <div
                  key={r.key}
                  className="projects-row"
                  role="button"
                  tabIndex={0}
                  aria-label={activeSource === 'claude'
                    ? `Open Projects modal for ${r.key}`
                    : `Open ${r.source} project details: ${r.label}`}
                  onClick={onRowClick(r.source, r.key)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      // Re-dispatch directly; the e cast in the upstream
                      // plan is unnecessary now that we accept React's
                      // KeyboardEvent here.
                      e.stopPropagation();
                      openRow(r.source, r.key);
                    }
                  }}
                  title={r.label}
                >
                  <span className="name">{activeSource === 'all' ? `${r.source === 'claude' ? 'Claude' : 'Codex'} · ${r.label}` : r.label}</span>
                  {/* A5 — decorative cost-relative bar. The enclosing
                      role="button" row already names the project, cost,
                      and %, so the bar conveys nothing new (and its width
                      is cost-vs-leader, NOT the project's usage %, so a
                      progressbar valuenow would mislead). */}
                  <div className="lb-bar" style={barStyle} aria-hidden="true" />
                  <span className="cost">{fmt.usd2(r.cost)}</span>
                  <span className="pct">
                    {r.pct == null ? '—' : fmt.pct0(r.pct)}
                  </span>
                </div>
              );
            })}
            {tail.length > 0 && (
              <div
                className="projects-row tail"
                aria-label={`${tail.length} more projects`}
              >
                <span className="name muted">+{tail.length} more</span>
                <div
                  className="lb-bar muted"
                  style={{ '--w': `${(tailCost / leaderCost) * 100}%` } as CSSProperties}
                  aria-hidden="true"
                />
                <span className="cost muted">{fmt.usd2(tailCost)}</span>
                <span className="pct muted">
                  {tailPctRaw === 0 ? '—' : fmt.pct0(tailPctRaw)}
                </span>
              </div>
            )}
          </>
        )}
      </div>
    </section>
  );
}
