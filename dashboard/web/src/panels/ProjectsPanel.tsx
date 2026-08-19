// ProjectsPanel — ONE ranked leaderboard of the top projects, with inline
// provider attribution under All (#312 §7.3: All "presents provider
// attribution within the shared ranking language; it does not render a second
// unframed projects table"). Click a row to drill into that project; click the
// panel chrome to open the modal un-targeted. See spec §5.2 (envelope shape)
// and §4.1 (cross-nav routing).
//
// The ranking's PERIOD differs by selection, and the header says which:
//   - Claude → the current subscription week, and it still says "this week".
//   - All    → one shared absolute range, folded identically for both
//              providers and published at `sources.all.data.aggregates.range`
//              (#556 S2 §3.2). Before that range existed, this panel printed
//              "(N this week)" over a ranking whose Claude leg covered a week
//              and whose Codex leg covered roughly thirty days.
//
// The PERCENTAGE also differs by selection and is named in one legend, on
// every tab (§4.2): a share of the week's quota under Claude, a share of the
// ranked cost otherwise.
//
// States:
//   - projects envelope null   → "Projects data unavailable" panel-empty.
//   - a withheld aggregate     → its own copy, naming what is missing (§3.7).
//   - rows array empty         → "No project activity ..." panel-empty.
//
// null percentage renders as em-dash (—): the total is zero, so the share is
// undefined; mirrors the kernel's null emission.
import { useSyncExternalStore, type CSSProperties, type MouseEvent } from 'react';
import { useScopedSnapshot } from '../hooks/useScopedSnapshot';
import { dispatch, getState, subscribeStore } from '../store/store';
import { PanelGrip } from '../components/PanelGrip';
import { PanelSkeleton } from '../components/PanelSkeleton';
import { ShareIcon } from '../components/ShareIcon';
import { ExpandButton } from '../components/ExpandButton';
import { openShareModal } from '../store/shareSlice';
import { cardRegionClick } from '../lib/cardRegion';
import { fmt } from '../lib/fmt';
import { presentationProjects, presentationProviders } from '../lib/dashboardPresentation';
import { formatSpan } from '../lib/projectWindow';
import { withheldMessage } from '../lib/withheldCopy';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { warningForDomain } from '../lib/sourceGating';
import { providerIsDecorated } from '../store/accountFocus';
import { PROJECTS_MERGED_ACCOUNTS_NOTE } from '../lib/projectsColumns';
import { DegradedChip } from './sourcePanel';

const TOP_N = 5;

// #294 S5 — source-aware wrapper. Claude = legacy leaderboard (unchanged);
// Codex = native qualified-attribution table; All = provider sections
// (identical labels across providers stay distinct rows — different keys).
export function ProjectsPanel() {
  const env = useScopedSnapshot();
  const activeSource = useSyncExternalStore(subscribeStore, () => getState().activeSource);
  const projected = presentationProjects(env, activeSource);
  const rows = projected.state === 'available' ? projected.rows : [];
  const isUnavailable = projected.state === 'unavailable';
  // §3.7 — a withheld aggregate is its OWN state, distinct from both "no
  // activity" and "restart the dashboard". Rendering it as either would report
  // a range problem as emptiness or as a broken instance.
  const withheld = projected.state === 'withheld' ? projected : null;
  const display = useDisplayTz();
  const range = projected.state === 'available' ? projected.range : null;
  // §4.4 — the resolved dates, or null when no range was published. A null
  // span falls back to a period-free phrase rather than naming one nothing
  // established.
  const rangeSpan = formatSpan(
    range == null ? null : { startAt: range.start_at, endAt: range.end_at },
    { tz: display.resolvedTz, offsetLabel: display.offsetLabel },
    // The published `end_at` is already `now_utc` and legitimately TRAILS the
    // clock on an idle tick (§3.6), so the clamp is a no-op here in practice.
    // It is passed anyway so every span-stating surface goes through the same
    // rule and none can drift into naming a future day.
    { clampEndTo: env?.generated_at },
  );
  // §4.2 — one legend for the percentage slot, on EVERY tab. The
  // recomputation was never All-specific: the Codex tab already showed a
  // share of cost in the slot the Claude tab uses for a share of quota.
  const metricName = activeSource === 'claude' ? 'share of quota' : 'share of cost';
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
        {/* The h2 carries the COUNT and nothing else under a shared range.
            Measured at 390px, with the range and the composition inside it:
            clientWidth 174px against scrollWidth 385px, under the mobile
            `white-space: nowrap` / `text-overflow: ellipsis` rule — so it
            rendered "Projects (41 projects…" and cut away the resolved range,
            which is the entire point of this change. The range and the
            composition moved to `.panel-range-note` below, a full-width
            line that wraps. Neither was shortened; the h2 was. Weekly,
            Monthly, Blocks and Daily now use the SAME class for the same
            reason, which is why it is named for panels and not for this
            panel. */}
        <h2>
          Projects{' '}
          <span className="sub">
            {showLoadingSub
              ? '(loading)'
              : withheld
                ? '(withheld)'
                : isUnavailable
                  ? '(unavailable)'
                  : activeSource === 'claude'
                    ? `(${rows.length} this week)`
                    : `(${rows.length} projects)`}
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

  if (isUnavailable || withheld) {
    return (
      <section
        className="panel accent-magenta"
        id="panel-projects"
        data-panel-kind="projects"
        data-source={activeSource}
        role="region"
        aria-label="Projects panel"
      >
        {header}
        <div className="panel-body projects-body">
          {/* HYDRATING IS TESTED FIRST, and the order is the whole point. The
              store's initial snapshot is null while `activeSource` is
              persisted, so a user whose last selection was All renders once
              before the bootstrap response arrives — and
              `presentationProjects(null, 'all')` synthesizes `rows_absent` for
              that null envelope. Testing `withheld` first therefore opened a
              cold load with "This page is talking to a server that does not
              publish the combined ranking", naming a server-version problem
              that does not exist and prescribing an action. #278 §1.4 added
              this branch precisely so a first paint shows a skeleton rather
              than copy implying a broken instance. */}
          {hydrating ? (
            <PanelSkeleton />
          ) : withheld ? (
            <div className="panel-empty panel-withheld" data-withheld-code={withheld.code}>
              {withheldMessage(withheld, 'ranking')}
            </div>
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

  // The header's sub-line: the resolved range, and — under All only — the
  // composition. `by provider` describes nothing on a single-provider tab, so
  // the Codex tab states its range alone; the Claude tab states neither,
  // because its ranking is the subscription week its h2 already names.
  const noteParts = [
    rangeSpan,
    activeSource === 'all' ? 'by provider' : null,
  ].filter((part): part is string => part != null);
  const rangeNote = activeSource === 'claude' || noteParts.length === 0
    ? null
    : <div className="panel-range-note">{noteParts.join(' · ')}</div>;

  // #620 S1 D1 — where this panel remains a single merged fold across a
  // provider's accounts, it says so. The dashboard publishes `account_scopes`
  // for Codex only, so a Claude install with more than one real account has no
  // way to narrow this ranking at all, and the rule was true, enforced in the
  // server arithmetic, and stated nowhere a reader could see it. The gate is
  // the R8 decoration signal: below two real accounts nothing is merged, and
  // the sentence would describe a fold that is not happening.
  //
  // The SELECTION gates it too. The sentence names a Claude fold, and the
  // Codex tab ranks Codex projects — so on that tab it would describe a
  // population the figures beneath it are not computed over. Under All the
  // ranking does fold every Claude account into one row set, so it stays.
  const mergedNote = activeSource !== 'codex' && providerIsDecorated(env, 'claude')
    ? (
      <div className="panel-range-note projects-merged-note">
        {PROJECTS_MERGED_ACCOUNTS_NOTE}
      </div>
    )
    : null;

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

  // §4.5 — the accessible name carries the cost and the NAMED percentage. The
  // comment on the cost bar used to assert the row already named them; it did
  // not, and the bar is the only other thing in the row that could have.
  const rowAccessibleName = (r: (typeof rows)[number]): string => {
    const figures = `${fmt.usd2(r.cost)}, ${
      r.pct == null ? `no ${metricName}` : `${fmt.pct0(r.pct)} ${metricName}`
    }`;
    return activeSource === 'claude'
      ? `Open Projects modal for ${r.key}: ${figures}`
      : `Open ${r.source} project details: ${r.label}: ${figures}`;
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
      {rangeNote}
      {mergedNote}
      <div className="panel-body projects-body">
        {rows.length === 0 ? (
          hydrating ? (
            <PanelSkeleton />
          ) : (
            <div className="panel-empty">
              {activeSource === 'claude'
                ? 'No project activity yet this week.'
                : rangeSpan == null
                  ? 'No project activity in this window.'
                  : `No project activity in ${rangeSpan}.`}
            </div>
          )
        ) : (
          <>
            <div className="projects-legend">% = {metricName}</div>
            {top.map((r) => {
              const widthPct = (r.cost / leaderCost) * 100;
              const barStyle = { '--w': `${widthPct}%` } as CSSProperties;
              const body = (
                <>
                  <span className="name">{activeSource === 'all' ? `${r.source === 'claude' ? 'Claude' : 'Codex'} · ${r.label}` : r.label}</span>
                  {/* A5 — decorative cost-relative bar, deliberately hidden.
                      The row's accessible name carries the project, the cost
                      and the NAMED percentage (built above), so the bar
                      conveys nothing new — and its width is cost-vs-leader,
                      NOT the project's usage %, so a progressbar valuenow
                      would mislead. */}
                  <div className="lb-bar" style={barStyle} aria-hidden="true" />
                  <span className="cost">{fmt.usd2(r.cost)}</span>
                  <span className="pct">
                    <span className="pct-value">
                      {r.pct == null ? '—' : fmt.pct0(r.pct)}
                    </span>
                    {/* The metric name reached only the DRILLABLE rows, via
                        their `aria-label`. A non-drillable row has no label
                        (deliberately — it is not a control), so its percentage
                        was announced as a bare number with no unit, and a null
                        one as a bare em-dash. The legend names the metric once
                        for sighted readers; this names it per row for everyone
                        else, in the same words `rowAccessibleName` uses. */}
                    <span className="sr-only">
                      {r.pct == null ? ` no ${metricName}` : ` ${metricName}`}
                    </span>
                  </span>
                </>
              );
              // §3.8a — a row the drill-down route cannot resolve stays in the
              // ranking at its real cost and loses only the interaction. No
              // click handler, no keyboard handler, no `role="button"` and no
              // pointer cursor, so assistive technology is not told there is a
              // control here and nobody is offered a modal that 404s.
              if (!r.drillable) {
                return (
                  <div
                    key={r.key}
                    className="projects-row is-static"
                    data-project-key={r.key}
                    data-drillable="false"
                    title={`${r.label} — no detail view is available for this project in this range`}
                  >
                    {body}
                    {/* WHY there is no detail, as text rather than only as a
                        `title`. A title is not a touch disclosure (recorded in
                        this repository already) and a screen reader does not
                        read one on a non-focusable element, so the reason
                        reached nobody who could not hover. This element is a
                        real second grid line, so it is visible on touch and
                        announced in the row. */}
                    <span className="projects-row-nodrill" data-nodrill-reason>
                      no detail view for this range
                    </span>
                  </div>
                );
              }
              return (
                <div
                  key={r.key}
                  className="projects-row"
                  data-project-key={r.key}
                  data-drillable="true"
                  role="button"
                  tabIndex={0}
                  aria-label={rowAccessibleName(r)}
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
                  {body}
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
