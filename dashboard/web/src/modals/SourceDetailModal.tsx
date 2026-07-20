import { useCallback, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useSourceDetail } from '../hooks/useSourceDetail';
import { fmt } from '../lib/fmt';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { modelChipClass } from '../lib/model';
import { ModelCostBars } from './ModelCostBars';
import { ShareIcon } from '../components/ShareIcon';
import type { SharePanelId } from '../share/types';
import { Modal } from './Modal';
import type {
  ClaudeBlockDetailBody,
  ClaudeProjectDetailBody,
  ClaudeSessionDetailBody,
  CodexBlockDetailBody,
  CodexProjectDetailBody,
  CodexSessionDetailBody,
  SourceDetailBody,
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
  const detail = useSourceDetail<SourceDetailBody>(
    open?.source ?? 'codex',
    open?.resource ?? 'session',
    open?.key ?? null,
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
  return (
    <div className="sd-claude-session">
      <dl className="sd-tokens">
        <div><dt>Cost</dt><dd>{fmt.usd2(d.cost_total_usd)}</dd></div>
        <div><dt>Input</dt><dd>{fmt.tokens(d.input_tokens)}</dd></div>
        <div><dt>Cache write</dt><dd>{fmt.tokens(d.cache_creation_tokens)}</dd></div>
        <div><dt>Cache read</dt><dd>{fmt.tokens(d.cache_read_tokens)}</dd></div>
        <div><dt>Output</dt><dd>{fmt.tokens(d.output_tokens)}</dd></div>
        <div><dt>Duration</dt><dd>{d.duration_min == null ? '—' : `${d.duration_min}m`}</dd></div>
      </dl>
    </div>
  );
}

function ClaudeProjectDetailView({ d }: { d: ClaudeProjectDetailBody }) {
  return <div className="sd-claude-project"><p>{d.sessions_total} sessions · {fmt.usd2(d.window_cost_usd)} · {d.window_attributed_pct == null ? 'usage unavailable' : `${d.window_attributed_pct.toFixed(1)}% attributed`}</p></div>;
}

function ClaudeBlockDetailView({ d }: { d: ClaudeBlockDetailBody }) {
  return <div className="sd-claude-block"><p>{fmt.usd2(d.cost_usd)} · {fmt.tokens(d.total_tokens)} tokens · {d.is_active ? 'active' : 'complete'}</p></div>;
}

function CodexSessionDetailView({ d }: { d: CodexSessionDetailBody }) {
  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  const cacheHit = d.input_tokens > 0
    ? d.cached_input_tokens / d.input_tokens * 100
    : null;
  const tokenTiles: Array<[string, number, boolean]> = [
    ['Input', d.input_tokens, false],
    ['Output', d.output_tokens, false],
    ['Cached input', d.cached_input_tokens, false],
    ['Reasoning', d.reasoning_output_tokens, false],
    ['Cache hit %', cacheHit ?? 0, true],
  ];
  const modelRows = d.model_breakdowns.flatMap((model) => {
    const name = model.modelName?.trim();
    return name ? [{ model: name, cost_usd: model.cost ?? 0, label: name.replace(/^gpt-/i, '') }] : [];
  });
  const singleModel = modelRows.length === 1;
  return (
    <div className="sd-codex-session modal-content" data-testid="codex-session-detail">
      {d.metadata_availability === 'partial' ? <p className="sd-note">{d.metadata_reason}</p> : null}
      <div className="m-chipstrip">
        <span className="msess-badge" aria-label="Session">{d.label || 'Untitled session'}</span>
        <span className="m-pill accent-blue">Codex</span>
      </div>

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
      <div className="msess-tok-grid">
        {tokenTiles.map(([label, value, percent]) => (
          <div key={label} className={`msess-tok-tile${percent ? ' cache-hit' : ''}`}>
            <div className="lbl">{label}</div>
            <div className="n">{percent ? `${value.toFixed(1)}%` : value.toLocaleString('en-US')}</div>
            {percent ? <div className="bar"><div className="fill" style={{ width: `${Math.max(0, Math.min(100, value))}%` }} /></div> : null}
          </div>
        ))}
      </div>

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
  return (
    <div className="sd-codex-project" data-testid="codex-project-detail">
      {d.metadata_availability === 'partial' ? <p className="sd-note">{d.metadata_reason}</p> : null}
      <h3>{d.label || 'Codex project'}</h3>
      <p>{d.session_count} sessions · {fmt.usd2(d.cost_usd)} · {fmt.tokens(d.total_tokens)} tokens</p>
      <dl className="sd-context">
        <div><dt>First seen</dt><dd>{d.first_seen}</dd></div>
        <div><dt>Last seen</dt><dd>{d.last_seen}</dd></div>
        <div><dt>Range</dt><dd>{d.range_start} → {d.range_end}</dd></div>
      </dl>
      <h3>Models</h3>
      <ul className="sd-model-breakdown">
        {d.models.map((model) => (
          <li key={model.model}><span>{model.model}</span><span>{fmt.usd2(model.cost_usd)}</span></li>
        ))}
      </ul>
      <h3>Recent sessions</h3>
      <ul className="sd-recent-sessions">
        {d.sessions.map((session) => (
          <li key={`${session.label}:${session.last_activity}`}>
            <span>{session.label}</span><span>{fmt.usd2(session.cost_usd)} · {fmt.tokens(session.total_tokens)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function CodexBlockDetailView({ d }: { d: CodexBlockDetailBody }) {
  return (
    <div className="sd-codex-block" data-testid="codex-block-detail">
      <p className="sd-block-label">{d.label}</p>
      <p>{fmt.pct0(d.current_percent)} · {d.is_active ? 'active' : 'complete'} · resets {d.resets_at}</p>
      <dl className="sd-context">
        <div><dt>Window</dt><dd>{d.start_at || '—'} → {d.end_at || d.resets_at}</dd></div>
        <div><dt>Cost</dt><dd>{fmt.usd2(d.cost_usd ?? null)}</dd></div>
        <div><dt>Forecast</dt><dd>{fmt.pct1(d.forecast.projected_percent)}</dd></div>
        <div><dt>Freshness</dt><dd>{d.freshness}</dd></div>
      </dl>
      <h3>Models</h3>
      <ul className="sd-model-breakdown">
        {(d.model_breakdowns ?? []).map((model) => (
          <li key={model.modelName ?? 'unknown'}><span>{model.modelName ?? 'Unknown model'}</span><span>{fmt.usd2(model.cost ?? null)}</span></li>
        ))}
      </ul>
      <h3>Quota observations</h3>
      <p>{d.observations.length} observations · {d.milestones.length} milestones</p>
    </div>
  );
}
