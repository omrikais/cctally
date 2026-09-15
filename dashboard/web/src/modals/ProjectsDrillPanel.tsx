// ProjectsDrillPanel — per-project drill that appears below the
// projects table when a row is selected (spec §3.5, plan Task 5 Step 5).
//
// Renders the state of GET /api/project/<key>?weeks=N. Since #834 S2 (#775)
// it does NOT fetch: `CanonicalProjectsModal` owns the one `useProjectDetail`
// call and passes its state in, so the mobile and desktop mount points are the
// same owner's output rather than two independent fetchers. Two columns on
// desktop:
//   - Models (this project): horizontal mini-bars sized to top model.
//   - Recent sessions:        clickable rows opening SessionModal (the
//                             cross-nav "replace pattern"; spec §4.2).
//
// `sessions_total > sessions.length` adds a "+N more" affordance below
// the visible list (spec §3.5).
import type { ProjectDetailState } from '../hooks/useProjectDetail';
import { useSnapshot } from '../hooks/useSnapshot';
import { exclusiveWindowSpan, formatSpan } from '../lib/projectWindow';
import { dispatch } from '../store/store';
import { fmt } from '../lib/fmt';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { modelChipClass, modelChipStyle } from '../lib/model';
import { costClass } from '../lib/cost';
import { abbreviateModel } from '../lib/modelName';
import { ModelCostBars } from './ModelCostBars';
import type { ProjectDetailModelRow } from '../types/envelope';

export interface ProjectsDrillPanelProps {
  projectKey: string;
  windowWeeks: number;
  // #834 S2 (#775) — the fetch state, OWNED by `CanonicalProjectsModal`. This
  // panel is presentation-only. It used to call `useProjectDetail` itself and
  // was mounted at two places, one inside the mobile row and one below the
  // table; crossing the 640px breakpoint unmounted one and mounted the other,
  // and because the hook's state lives in the component the newly mounted copy
  // started from `data == null`, dropped the drill to "Loading…" and refetched
  // a project detail the process already had.
  detail: ProjectDetailState;
}

export interface ProjectDetailContentData {
  label: string;
  window_weeks: number;
  window_start_at: string;
  window_end_at: string;
  window_intervals?: Array<{ start_at: string; end_at: string }>;
  window_cost_usd: number;
  models: ProjectDetailModelRow[];
  sessions: Array<{
    key: string;
    started_at: string;
    last_activity_at: string;
    primary_model: string;
    cost_usd: number;
  }>;
  sessions_total: number;
}

interface ProjectDetailContentProps {
  data: ProjectDetailContentData;
  onOpenSession: (key: string) => void;
  onShowInSessions: () => void;
  testId?: string;
  sessionTestIdPrefix?: string;
  showInSessionsTestId?: string;
}

// #556 S2 Task 0b — the drill states the span it actually reported.
//
// `_project_window_weeks_for_key` widens an All-originated drill so it reaches
// the shared range its row was ranked over, and that widening is global: one
// shared start resolves for the whole snapshot, so a row ranked over thirty
// days opens a fifty-six-day window. `window_cost_usd` is then `>=` the ranked
// figure with nothing reconciling them, which is the untruthfulness this
// session exists to remove, one level down. Naming the resolved dates labels
// each figure so neither implies the other.
//
// The response publishes its own authoritative half-open bounds. When either
// is unresolvable the header falls back to the unit count rather than stating
// a span nothing established. The client performs no window arithmetic.
// The stated end is CLAMPED to the snapshot's own `generated_at`: the window
// runs to the current week's Sunday, so on six days in seven the unclamped
// form named days no data can exist for, beside a cost figure. The anchor is
// the envelope instant, never the client clock.
function useDrillWindowSpan(
  windowStartAt: string,
  windowEndAt: string,
): string | null {
  const env = useSnapshot();
  const display = useDisplayTz();
  return formatSpan(
    exclusiveWindowSpan(windowStartAt, windowEndAt),
    { tz: display.resolvedTz, offsetLabel: display.offsetLabel },
    { clampEndTo: env?.generated_at },
  );
}

export function ProjectsDrillPanel(
  { projectKey, windowWeeks, detail }: ProjectsDrillPanelProps,
) {
  const { data, loading, error } = detail;

  // Stale-on-switch guard: while `useProjectDetail` is fetching for the
  // newly selected project — OR for the same project under a different
  // window (e.g. 12w → 4w) — the SWR pattern keeps prior `data` mounted.
  // Without a window check the drill keeps rendering the prior window's
  // cost/models/sessions under the new `{windowWeeks} cycles` heading until
  // /api/project resolves; on large projects that fetch can take seconds
  // so the modal would show numbers that disagree with the visible
  // header. Render Loading… until the new fetch resolves so the drill
  // never lies about which (project, window) it represents.
  const isStaleForCurrentKey =
    data != null &&
    (data.key !== projectKey || data.window_weeks !== windowWeeks);

  if ((loading && !data) || isStaleForCurrentKey)
    return <div className="panel-empty">Loading…</div>;
  if (error && !data) return <div className="panel-empty">{error}</div>;
  if (!data) return null;

  return (
    <ProjectDetailContent
      data={{
        ...data,
        label: data.key,
        sessions: data.sessions.map((session) => ({
          ...session,
          key: session.session_id,
        })),
      }}
      onOpenSession={(sessionId) => dispatch({
        type: 'OPEN_MODAL',
        kind: 'session',
        sessionId,
      })}
      onShowInSessions={() => {
        dispatch({ type: 'SET_FILTER', text: data.key });
        dispatch({ type: 'CLOSE_MODAL' });
      }}
    />
  );
}

export function ProjectDetailContent({
  data,
  onOpenSession,
  onShowInSessions,
  testId = 'projects-drill',
  sessionTestIdPrefix = 'drill-session',
  showInSessionsTestId = 'drill-show-in-sessions',
}: ProjectDetailContentProps) {
  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  const remaining = Math.max(0, data.sessions_total - data.sessions.length);
  const windowSpan = useDrillWindowSpan(data.window_start_at, data.window_end_at);
  const resetGapCount = (data.window_intervals ?? []).reduce(
    (count, interval, index, intervals) => (
      index > 0 && intervals[index - 1]!.end_at < interval.start_at
        ? count + 1
        : count
    ),
    0,
  );

  return (
    <div className="projects-drill" data-testid={testId} aria-live="polite">
      <div className="projects-drill-head">
        <span className="title">
          ▾ {data.label} · {data.sessions_total} session{data.sessions_total === 1 ? '' : 's'}
          {' · '}
          {fmt.usd2(data.window_cost_usd)}
          {/* #750 S4 D2 / acceptance 11: the drill header names cycles on
              BOTH branches. `window_weeks` is a frozen wire name; only the
              rendered noun changes. */}
          {windowSpan == null
            ? ` (${data.window_weeks} ${data.window_weeks === 1 ? 'cycle' : 'cycles'})`
            : ` · ${data.window_weeks} ${data.window_weeks === 1 ? 'cycle' : 'cycles'} · ${windowSpan}`}
          {resetGapCount > 0
            ? ` · ${resetGapCount} reset gap${resetGapCount === 1 ? '' : 's'}`
            : ''}
        </span>
      </div>
      <div className="projects-drill-grid">
        <div>
          <div className="section-label">Models (this project)</div>
          {data.models.length === 0 ? (
            <div className="muted">No model data for this window.</div>
          ) : (
            // #263 — the drill's `data.models` rows carry only the canonical
            // `model` id (no server `display` field, unlike History/Block's
            // `ModelCostRow`), so a dated id like `claude-opus-4-6-20251101`
            // wrapped to two lines in the shared 110px chip column. Derive the
            // friendly short chip label client-side via `abbreviateModel`,
            // exactly as SessionModal does for its display-field-less
            // `cost_per_model` rows — one-line chips, parity with the other
            // three ModelCostBars surfaces, no server/envelope change.
            <ModelCostBars
              rows={data.models.map((m) => ({
                model: m.model,
                cost_usd: m.cost_usd,
                label: abbreviateModel(m.model),
              }))}
            />
          )}
        </div>
        <div>
          <div className="section-label">Recent sessions →</div>
          {data.sessions.length === 0 ? (
            <div className="muted">No sessions for this window.</div>
          ) : (
            data.sessions.map((s, i) => (
              <button
                key={s.key}
                data-testid={`${sessionTestIdPrefix}-${i}`}
                className="drill-session-row"
                onClick={() => onOpenSession(s.key)}
              >
                <span className="started">{fmt.datetimeShort(s.last_activity_at, ctx)}</span>
                <span
                  className={`chip ${modelChipClass(s.primary_model)}`}
                  style={modelChipStyle(s.primary_model)}
                >
                  {s.primary_model}
                </span>
                <span className={`cost ${costClass(s.cost_usd)}`}>{fmt.usd2(s.cost_usd)}</span>
              </button>
            ))
          )}
          <div className="drill-session-footer">
            {remaining > 0 && (
              <span className="muted">+{remaining} more</span>
            )}
            <button
              type="button"
              data-testid={showInSessionsTestId}
              className="drill-show-in-sessions"
              onClick={onShowInSessions}
            >
              Show in Sessions →
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
