import { useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useSourceDetail } from '../hooks/useSourceDetail';
import { fmt } from '../lib/fmt';
import type {
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
  const detail = useSourceDetail<SourceDetailBody>(
    open?.source ?? 'codex',
    open?.resource ?? 'session',
    open?.key ?? null,
  );
  if (open == null) return null;

  const close = () => dispatch({ type: 'CLOSE_SOURCE_DETAIL' });

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

  return (
    <div
      className="source-detail-backdrop"
      onClick={close}
      role="presentation"
      data-testid="source-detail-modal"
    >
      <div
        className="source-detail-card"
        role="dialog"
        aria-modal="true"
        aria-label="Source detail"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === 'Escape') {
            e.stopPropagation();
            close();
          }
        }}
      >
        <button type="button" className="source-detail-close" aria-label="Close" onClick={close}>
          ×
        </button>
        {body}
      </div>
    </div>
  );
}

function SourceDetailBodyView({ data }: { data: SourceDetailBody }) {
  switch (data.detail_kind) {
    case 'codex_session':
      return <CodexSessionDetailView d={data} />;
    case 'codex_project':
      return <CodexProjectDetailView d={data} />;
    case 'codex_block':
      return <CodexBlockDetailView d={data} />;
    default:
      // Claude bodies are handled by the legacy modals in S5; the qualified
      // path here is exercised by the Codex/All source rows.
      return <p className="sd-generic">{data.detail_kind}</p>;
  }
}

function CodexSessionDetailView({ d }: { d: CodexSessionDetailBody }) {
  return (
    <div className="sd-codex-session" data-testid="codex-session-detail">
      <h2>Codex session</h2>
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
      <h2>Codex project</h2>
      <p>{d.session_count} sessions · {fmt.usd2(d.cost_usd)} · {fmt.tokens(d.total_tokens)} tokens</p>
    </div>
  );
}

function CodexBlockDetailView({ d }: { d: CodexBlockDetailBody }) {
  return (
    <div className="sd-codex-block" data-testid="codex-block-detail">
      <h2>{d.label}</h2>
      <p>{fmt.pct0(d.current_percent)} · resets {d.resets_at}</p>
    </div>
  );
}
