import { useCallback, useState, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useSourceDetail } from '../hooks/useSourceDetail';
import { fmt } from '../lib/fmt';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { modelChipClass } from '../lib/model';
import { ModelCostBars } from './ModelCostBars';
import { ProjectDetailContent } from './ProjectsDrillPanel';
import { SessionDetailContent } from './SessionModal';
import { ShareIcon } from '../components/ShareIcon';
import type { SharePanelId } from '../share/types';
import { Modal } from './Modal';
import type {
  ClaudeBlockDetailBody,
  ClaudeProjectDetailBody,
  ClaudeSessionDetailBody,
  CodexBlockDetailBody,
  CodexModelBreakdown,
  CodexProjectDetailBody,
  CodexSessionDetailBody,
  CodexTokenTotals,
  SourceDetailBody,
  SessionDetail,
} from '../types/envelope';

// #294 S5 §5.6 — the qualified source-detail modal for Codex/All source rows.
// Fetches `/api/source/<source>/<resource>/<key>` via useSourceDetail, unwraps
// the `{source, resource, data}` envelope, and dispatches on `detail_kind`. The
// Codex session detail exposes NO conversation-reader affordance (deferred to
// S6-S8). The two stable error envelopes render as friendly non-fatal messages.

export function SourceDetailModal() {
  const open = useSyncExternalStore(subscribeStore, () => getState().openSourceDetail);
  const selection = useSyncExternalStore(
    subscribeStore,
    () => getState().openSourceDetailSelection ?? getState().activeSource,
  );
  const projectWindowWeeks = useSyncExternalStore(
    subscribeStore,
    () => getState().prefs.projectsWindowWeeks,
  );
  const detail = useSourceDetail<SourceDetailBody>(
    open?.source ?? 'codex',
    open?.resource ?? 'session',
    open?.key ?? null,
    { windowWeeks: open?.resource === 'project' ? projectWindowWeeks : undefined },
  );
  const close = useCallback(() => dispatch({ type: 'CLOSE_SOURCE_DETAIL' }), []);
  if (open == null) return null;

  let body: React.ReactNode;
  if (detail.loading && detail.data == null) {
    body = <p className="sd-loading">Loading…</p>;
  } else if (detail.error) {
    body = (
      <p className="sd-error" data-testid="source-detail-error">
        {detail.error.kind === 'not-found'
          ? 'This item is no longer available.'
          : detail.error.kind === 'capability'
            ? 'This detail is unavailable for this source.'
            : "Couldn't load this detail — try again."}
      </p>
    );
  } else if (detail.data) {
    body = <SourceDetailBodyView data={detail.data} />;
  }

  const panel = open.resource === 'session' ? 'sessions' : `${open.resource}s` as SharePanelId;
  const resourceLabel = `${open.resource[0].toUpperCase()}${open.resource.slice(1)}`;
  const title = open.resource === 'session'
    ? 'Session detail'
    : open.resource === 'project' ? 'Project detail' : 'Block detail';
  return (
    <Modal
      key={`${open.source}:${open.resource}:${open.key}`}
      title={title}
      accentClass={open.resource === 'block' ? 'accent-cyan' : 'accent-orange'}
      dataSource={open.source}
      onClose={close}
      focusLayer="source-detail"
      rootId="source-detail-root"
      titleId="source-detail-title"
      bodyId="source-detail-body"
      rootTestId="source-detail-modal"
      cardClassName="source-detail-card"
      headerExtras={(
        <ShareIcon
          panel={panel}
          panelLabel={resourceLabel}
          triggerId="source-detail-share"
          onClick={() => dispatch({
            type: 'OPEN_SHARE',
            panel,
            triggerId: 'source-detail-share',
            source: selection,
          })}
        />
      )}
    >
      {body}
    </Modal>
  );
}

function SourceDetailBodyView({ data }: { data: SourceDetailBody }) {
  switch (data.detail_kind) {
    case 'claude_session':
      return <ClaudeSessionDetailView d={data} />;
    case 'claude_project':
      return <ClaudeProjectDetailView d={data} />;
    case 'claude_block':
      return <ClaudeBlockDetailView d={data} />;
    case 'codex_session':
      return <CodexSessionDetailView d={data} />;
    case 'codex_project':
      return <CodexProjectDetailView d={data} />;
    case 'codex_block':
      return <CodexBlockDetailView d={data} />;
  }
}

function ClaudeSessionDetailView({ d }: { d: ClaudeSessionDetailBody }) {
  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  const detail: SessionDetail = {
    session_id: null,
    started_utc: d.started_utc,
    last_activity_utc: d.last_activity_utc,
    duration_min: d.duration_min,
    cost_total_usd: d.cost_total_usd,
    project_label: d.project_label,
    project_path: null,
    input_tokens: d.input_tokens,
    output_tokens: d.output_tokens,
    cache_creation_tokens: d.cache_creation_tokens,
    cache_read_tokens: d.cache_read_tokens,
    cache_hit_pct: d.cache_hit_pct,
    models: d.models,
    cost_per_model: d.cost_per_model,
    source_paths: [],
  };
  return (
    <SessionDetailContent
      detail={detail}
      ctx={ctx}
      identityLabel={d.label}
      projectAction={d.project_key && d.project_label ? {
        label: d.project_label,
        onOpen: () => dispatch({
          type: 'OPEN_SOURCE_DETAIL',
          source: 'claude',
          resource: 'project',
          key: d.project_key!,
        }),
      } : undefined}
      privacyNote={d.privacy_note}
      showCacheRebuilds={false}
      testId="claude-session-detail"
    />
  );
}

function ClaudeProjectDetailView({ d }: { d: ClaudeProjectDetailBody }) {
  return (
    <div className="sd-claude-project" data-testid="claude-project-detail">
      <ProjectDetailContent
        data={d}
        testId="qualified-project-drill"
        sessionTestIdPrefix="qualified-project-session"
        showInSessionsTestId="qualified-project-show-in-sessions"
        onOpenSession={(key) => dispatch({
          type: 'OPEN_SOURCE_DETAIL',
          source: 'claude',
          resource: 'session',
          key,
        })}
        onShowInSessions={() => {
          dispatch({ type: 'SET_FILTER', text: d.label });
          dispatch({ type: 'CLOSE_SOURCE_DETAIL' });
        }}
      />
    </div>
  );
}

function ClaudeBlockDetailView({ d }: { d: ClaudeBlockDetailBody }) {
  return <div className="sd-claude-block"><p>{fmt.usd2(d.cost_usd)} · {fmt.tokens(d.total_tokens)} tokens · {d.is_active ? 'active' : 'complete'}</p></div>;
}

function BoundedCollection<T>({
  items,
  initial,
  noun,
  empty,
  rowTestId,
  itemKey,
  renderItem,
}: {
  items: readonly T[];
  initial: number;
  noun: string;
  empty: string;
  rowTestId: string;
  itemKey: (item: T, index: number) => string;
  renderItem: (item: T, index: number) => React.ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);
  if (items.length === 0) return <p className="sd-empty-note">{empty}</p>;
  const visible = expanded ? items : items.slice(0, initial);
  const hidden = items.length - Math.min(items.length, initial);
  return (
    <div className={`sd-bounded-collection${expanded ? ' is-expanded' : ''}`}>
      <ul className="sd-collection-list">
        {visible.map((item, index) => (
          <li key={itemKey(item, index)} data-testid={rowTestId}>
            {renderItem(item, index)}
          </li>
        ))}
      </ul>
      {hidden > 0 ? (
        <button
          type="button"
          className="sd-collection-toggle"
          aria-expanded={expanded}
          aria-label={expanded ? `Show first ${initial} ${noun}` : `Show all ${items.length} ${noun}`}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? 'Show less' : `+${hidden} more`}
        </button>
      ) : null}
    </div>
  );
}

function CodexTokenGrid({ totals }: { totals: CodexTokenTotals }) {
  const cacheHit = totals.input_tokens > 0
    ? totals.cached_input_tokens / totals.input_tokens * 100
    : null;
  const tokenTiles: Array<[string, number, boolean]> = [
    ['Input', totals.input_tokens, false],
    ['Output', totals.output_tokens, false],
    ['Cached input', totals.cached_input_tokens, false],
    ['Reasoning', totals.reasoning_output_tokens, false],
    ['Cache hit %', cacheHit ?? 0, true],
  ];
  return (
    <div className="msess-tok-grid">
      {tokenTiles.map(([label, value, percent]) => (
        <div key={label} className={`msess-tok-tile${percent ? ' cache-hit' : ''}`}>
          <div className="lbl">{label}</div>
          <div className="n">{percent ? `${value.toFixed(1)}%` : value.toLocaleString('en-US')}</div>
          {percent ? <div className="bar"><div className="fill" style={{ width: `${Math.max(0, Math.min(100, value))}%` }} /></div> : null}
        </div>
      ))}
    </div>
  );
}

function CodexModelCollection({
  rows,
  rowTestId,
  initial = 6,
}: {
  rows: readonly CodexModelBreakdown[];
  rowTestId: string;
  initial?: number;
}) {
  const named = rows.filter((row) => row.modelName?.trim());
  return (
    <BoundedCollection
      items={named}
      initial={initial}
      noun="models"
      empty="No model breakdown is available."
      rowTestId={rowTestId}
      itemKey={(row, index) => `${row.modelName}:${index}`}
      renderItem={(row) => (
        <>
          <span className="sd-collection-primary">
            <span className={`sw ${modelChipClass(row.modelName!)}`} aria-hidden="true" />
            {row.modelName}
          </span>
          <span>{fmt.usd2(row.cost ?? null)}</span>
        </>
      )}
    />
  );
}

function CodexSessionDetailView({ d }: { d: CodexSessionDetailBody }) {
  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  const cacheHit = d.input_tokens > 0
    ? d.cached_input_tokens / d.input_tokens * 100
    : null;
  const label = d.label?.trim() || 'Untitled session';
  const modelRows = d.model_breakdowns.flatMap((model) => {
    const name = model.modelName?.trim();
    return name ? [{ model: name, cost_usd: model.cost ?? 0, label: name.replace(/^gpt-/i, '') }] : [];
  });
  const singleModel = modelRows.length === 1;
  return (
    <div className="sd-codex-session modal-content" data-testid="codex-session-detail">
      {d.metadata_availability === 'partial' ? <p className="sd-note">{d.metadata_reason}</p> : null}
      <div className="m-chipstrip sd-session-chipstrip">
        <span className="msess-badge sd-prompt-clamp" aria-label="Session prompt" title={label}>{label}</span>
        <span className="m-pill accent-blue">Codex</span>
      </div>
      {label.length > 96 ? (
        <details className="sd-prompt-disclosure">
          <summary>Show full prompt</summary>
          <div className="sd-prompt-full" tabIndex={0}>{label}</div>
        </details>
      ) : null}

      <div className="m-hero cols-3">
        <div className="m-kv kv-cost"><svg className="icon" aria-hidden="true"><use href="/static/icons.svg#dollar" /></svg><div><div className="v">{fmt.usd2(d.cost_usd)}</div><div className="lbl">Total cost</div></div></div>
        <div className="m-kv kv-dur"><svg className="icon" aria-hidden="true"><use href="/static/icons.svg#clock" /></svg><div><div className="v">{d.duration_min == null ? '—' : `${d.duration_min} min`}</div><div className="lbl">Duration</div></div></div>
        <div className="m-kv kv-proj"><svg className="icon" aria-hidden="true"><use href="/static/icons.svg#folder" /></svg><div><div className="v" title={d.project || ''} aria-label="Project">{d.project || '—'}</div><div className="lbl">Project</div></div></div>
      </div>

      <div className="msess-ts">
        <svg className="icon" aria-hidden="true"><use href="/static/icons.svg#calendar" /></svg>
        <div><span className="k">started</span><span className="v">{fmt.datetimeShort(d.started_at ?? null, ctx)}</span></div>
        <div><span className="k">last activity</span><span className="v">{fmt.datetimeShort(d.last_activity, ctx)}</span></div>
      </div>

      <h3 className="m-sec sec-tok"><svg className="icon" aria-hidden="true"><use href="/static/icons.svg#hash" /></svg>Tokens</h3>
      <CodexTokenGrid totals={d} />

      <h3 className="m-sec sec-rebuild"><svg className="icon" aria-hidden="true"><use href="/static/icons.svg#refresh-cw" /></svg>Cache reuse</h3>
      <div className="msess-rebuild-saved">
        {cacheHit == null ? 'No input tokens recorded' : `${cacheHit.toFixed(1)}% of input reused from cache ✓`}
      </div>

      {singleModel ? (
        <div className="msess-model-caption">
          <span className={`sw ${modelChipClass(modelRows[0].model)}`} aria-hidden="true" />
          <span className="k">Model</span><span className="name">{modelRows[0].model}</span>
          <span className="dot" aria-hidden="true">·</span><span className="v">{fmt.usd2(modelRows[0].cost_usd)}</span>
        </div>
      ) : modelRows.length > 0 ? (
        <>
          <h3 className="m-sec sec-costm"><svg className="icon" aria-hidden="true"><use href="/static/icons.svg#pie-chart" /></svg>Cost by model</h3>
          <div className="sd-model-cost-bars"><ModelCostBars rows={modelRows} /></div>
        </>
      ) : null}
      <div className="sd-total-tokens">Total tokens <strong>{fmt.tokens(d.total_tokens)}</strong></div>
    </div>
  );
}

function CodexProjectDetailView({ d }: { d: CodexProjectDetailBody }) {
  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  const models: CodexModelBreakdown[] = d.models.map((model) => ({
    modelName: model.model,
    cost: model.cost_usd,
    inputTokens: model.input_tokens,
    cachedInputTokens: model.cached_input_tokens,
    outputTokens: model.output_tokens,
    reasoningOutputTokens: model.reasoning_output_tokens,
    totalTokens: model.total_tokens,
  }));
  return (
    <div className="sd-codex-project modal-content" data-testid="codex-project-detail">
      {d.metadata_availability === 'partial' ? <p className="sd-note">{d.metadata_reason}</p> : null}
      <div className="m-chipstrip">
        <span className="msess-badge sd-project-label" title={d.label || 'Codex project'}>{d.label || 'Codex project'}</span>
        <span className="m-pill accent-blue">Codex</span>
      </div>
      <div className="m-hero cols-3">
        <div className="m-kv kv-cost"><svg className="icon" aria-hidden="true"><use href="/static/icons.svg#dollar" /></svg><div><div className="v">{fmt.usd2(d.cost_usd)}</div><div className="lbl">Total cost</div></div></div>
        <div className="m-kv kv-dur"><svg className="icon" aria-hidden="true"><use href="/static/icons.svg#clock" /></svg><div><div className="v">{d.session_count}</div><div className="lbl">Sessions</div></div></div>
        <div className="m-kv kv-proj"><svg className="icon" aria-hidden="true"><use href="/static/icons.svg#hash" /></svg><div><div className="v">{fmt.tokens(d.total_tokens)}</div><div className="lbl">Total tokens</div></div></div>
      </div>
      <div className="msess-ts sd-project-ts">
        <svg className="icon" aria-hidden="true"><use href="/static/icons.svg#calendar" /></svg>
        <div><span className="k">first seen</span><span className="v">{fmt.datetimeShort(d.first_seen, ctx)}</span></div>
        <div><span className="k">last activity</span><span className="v">{fmt.datetimeShort(d.last_seen, ctx)}</span></div>
        <div><span className="k">range</span><span className="v">{fmt.datetimeShort(d.range_start, ctx)} → {fmt.datetimeShort(d.range_end, ctx)}</span></div>
      </div>
      <h3 className="m-sec sec-tok"><svg className="icon" aria-hidden="true"><use href="/static/icons.svg#hash" /></svg>Native token totals</h3>
      <CodexTokenGrid totals={d} />
      <h3 className="m-sec sec-costm"><svg className="icon" aria-hidden="true"><use href="/static/icons.svg#pie-chart" /></svg>Models</h3>
      <CodexModelCollection rows={models} rowTestId="codex-project-model-row" />
      <h3 className="m-sec sec-src"><svg className="icon" aria-hidden="true"><use href="/static/icons.svg#clock" /></svg>Recent sessions</h3>
      <BoundedCollection
        items={d.sessions}
        initial={6}
        noun="sessions"
        empty="No retained sessions are available."
        rowTestId="codex-project-session-row"
        itemKey={(session, index) => `${session.label}:${session.last_activity}:${index}`}
        renderItem={(session) => (
          <>
            <span className="sd-collection-primary sd-session-summary">
              <span>{session.label}</span>
              <span className="sd-collection-meta">{fmt.datetimeShort(session.last_activity, ctx)}</span>
            </span>
            <span>{fmt.usd2(session.cost_usd)} · {fmt.tokens(session.total_tokens)}</span>
          </>
        )}
      />
    </div>
  );
}

function CodexBlockDetailView({ d }: { d: CodexBlockDetailBody }) {
  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  const models = d.model_breakdowns ?? [];
  const observations = d.observations ?? [];
  const milestones = d.milestones ?? [];
  const freshness = d.freshness ?? 'unavailable';
  return (
    <div className="sd-codex-block modal-content" data-testid="codex-block-detail">
      <div className="m-chipstrip">
        <span className="msess-badge sd-project-label" title={d.label}>{d.label}</span>
        <span className="m-pill accent-blue">Codex</span>
        <span className={`m-pill ${d.is_active ? 'accent-green' : 'm-unavailable'}`}>{d.is_active ? 'Active' : 'Complete'}</span>
        <span className="m-pill accent-cyan">{freshness}</span>
      </div>
      <div className="m-hero cols-3">
        <div className="m-kv kv-dur"><svg className="icon" aria-hidden="true"><use href="/static/icons.svg#activity" /></svg><div><div className="v">{fmt.pct0(d.current_percent)}</div><div className="lbl">Current usage</div></div></div>
        <div className="m-kv kv-cost"><svg className="icon" aria-hidden="true"><use href="/static/icons.svg#dollar" /></svg><div><div className="v">{fmt.usd2(d.cost_usd ?? null)}</div><div className="lbl">Retained cost</div></div></div>
        <div className="m-kv kv-proj"><svg className="icon" aria-hidden="true"><use href="/static/icons.svg#trending-up" /></svg><div><div className="v">{fmt.pct1(d.forecast.projected_percent)}</div><div className="lbl">Projected at reset</div><div className="sub">{d.forecast.status}</div></div></div>
      </div>
      <div className="msess-ts sd-block-ts">
        <svg className="icon" aria-hidden="true"><use href="/static/icons.svg#calendar" /></svg>
        <div><span className="k">window</span><span className="v">{fmt.datetimeShort(d.start_at ?? null, ctx)} → {fmt.datetimeShort(d.end_at ?? d.resets_at, ctx)}</span></div>
        <div><span className="k">resets</span><span className="v">{fmt.datetimeShort(d.resets_at, ctx)}</span></div>
      </div>
      <h3 className="m-sec sec-costm"><svg className="icon" aria-hidden="true"><use href="/static/icons.svg#pie-chart" /></svg>Models</h3>
      <CodexModelCollection rows={models} rowTestId="codex-block-model-row" />
      <h3 className="m-sec sec-prog"><svg className="icon" aria-hidden="true"><use href="/static/icons.svg#activity" /></svg>Quota progression</h3>
      <p className="sd-progression-summary">{observations.length} observations · {milestones.length} milestones</p>
      <h4 className="sd-subsection-title">Observations</h4>
      <BoundedCollection
        items={observations}
        initial={8}
        noun="observations"
        empty="No retained quota observations are available."
        rowTestId="codex-block-observation-row"
        itemKey={(observation, index) => `${observation.captured_at}:${index}`}
        renderItem={(observation) => (
          <>
            <span className="sd-collection-primary sd-observation-time">{fmt.datetimeShort(observation.captured_at, ctx)}</span>
            <span className="sd-observation-value">{fmt.pct1(observation.used_percent)}</span>
            <span className="sd-observation-track" aria-hidden="true"><span style={{ width: `${Math.max(0, Math.min(100, observation.used_percent))}%` }} /></span>
          </>
        )}
      />
      <h4 className="sd-subsection-title">Milestones</h4>
      <BoundedCollection
        items={milestones}
        initial={8}
        noun="milestones"
        empty="No quota milestones were crossed in this window."
        rowTestId="codex-block-milestone-row"
        itemKey={(milestone, index) => `${milestone.percent}:${milestone.captured_at}:${index}`}
        renderItem={(milestone) => (
          <>
            <span className="sd-collection-primary">Crossed {fmt.pct0(milestone.percent)}</span>
            <span>{fmt.datetimeShort(milestone.captured_at, ctx)}</span>
          </>
        )}
      />
    </div>
  );
}
