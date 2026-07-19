import { useCallback, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useSourceDetail } from '../hooks/useSourceDetail';
import { fmt } from '../lib/fmt';
import { ShareIcon } from '../components/ShareIcon';
import { SELECTION_LABEL, type SharePanelId } from '../share/types';
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
  return (
    <Modal
      title={`${SELECTION_LABEL[open.source]} ${open.resource}`}
      accentClass={open.resource === 'project' ? 'accent-orange' : 'accent-cyan'}
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
  return (
    <div className="sd-codex-session" data-testid="codex-session-detail">
      {d.metadata_availability === 'partial' ? <p className="sd-note">{d.metadata_reason}</p> : null}
      <dl className="sd-tokens">
        <div><dt>Cost</dt><dd>{fmt.usd2(d.cost_usd)}</dd></div>
        <div><dt>Input</dt><dd>{fmt.tokens(d.input_tokens)}</dd></div>
        <div><dt>Cached input</dt><dd>{fmt.tokens(d.cached_input_tokens)}</dd></div>
        <div><dt>Output</dt><dd>{fmt.tokens(d.output_tokens)}</dd></div>
        <div><dt>Reasoning</dt><dd>{fmt.tokens(d.reasoning_output_tokens)}</dd></div>
        <div><dt>Total</dt><dd>{fmt.tokens(d.total_tokens)}</dd></div>
      </dl>
      <p className="sd-models">Models: {d.models.join(', ') || '—'}</p>
    </div>
  );
}

function CodexProjectDetailView({ d }: { d: CodexProjectDetailBody }) {
  return (
    <div className="sd-codex-project" data-testid="codex-project-detail">
      {d.metadata_availability === 'partial' ? <p className="sd-note">{d.metadata_reason}</p> : null}
      <p>{d.session_count} sessions · {fmt.usd2(d.cost_usd)} · {fmt.tokens(d.total_tokens)} tokens</p>
    </div>
  );
}

function CodexBlockDetailView({ d }: { d: CodexBlockDetailBody }) {
  return (
    <div className="sd-codex-block" data-testid="codex-block-detail">
      <p className="sd-block-label">{d.label}</p>
      <p>{fmt.pct0(d.current_percent)} · resets {d.resets_at}</p>
    </div>
  );
}
