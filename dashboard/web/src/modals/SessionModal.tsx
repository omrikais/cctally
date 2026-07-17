import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { openShareModal } from '../store/shareSlice';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { Modal } from './Modal';
import { CacheRebuildsSection } from './CacheRebuildsSection';
import { ShareIcon } from '../components/ShareIcon';
import { fmt, type FmtCtx } from '../lib/fmt';
import { revalToken } from '../lib/revalToken';
import { modelChipClass } from '../lib/model';
import { abbreviateModel } from '../lib/modelName';
import { ModelCostBars } from './ModelCostBars';
import { isSingleModel } from '../lib/sessionModel';
import type { SessionDetail } from '../types/envelope';

// SSE-driven live updates: the modal subscribes to the snapshot's change
// signal (#300 — `revalToken`: the all-inputs `data_version`, falling back to
// `generated_at`) and refetches /api/session/:id when it changes — i.e. on a
// genuine data change, NOT on every 5s heartbeat tick, so a finished session
// fetches once while open. Refetches keep the prior
// `data` mounted (stale-while-revalidate); the spinner only renders when no
// content is showing (data == null). Refetch network errors are silently
// swallowed so a transient blip does not yank good content. The
// `|| data == null` clause in isInitialFetch is load-bearing: it covers the
// case where a tick aborts an in-flight initial fetch before content has
// rendered, so the retry is correctly classified as initial (404 evicts,
// network error surfaces) rather than as a refetch (which would silently
// keep non-existent stale content and leave the spinner stuck).
//
// 404-grace policy: a single 404 on a refetch keeps stale content (transient
// blip). Two consecutive refetch 404s evict to the "Session not found …"
// error. A successful refetch (or non-404 error) clears the arm.
//
// Bound-id stability: the resolved session id (explicit openSessionId or
// the fallback newest-row at open time) is captured into resolvedIdRef
// and stays stable across SSE ticks. Only an openSessionId change (or
// close→reopen) re-binds. A snapshot whose newest-row changes between
// ticks will NOT silently swap which session the modal is showing.
//
// Do NOT re-introduce setData(null) into the refetch path — that defeats the
// stale-while-revalidate guarantee (the original CLAUDE.md gotcha).

const SUBAGENT_RE = /(^|\/)(subagents\/|agent-)/;

function useRevalToken(): string {
  // #300 — the change-signal token (data_version, falling back to generated_at).
  // A stable string per state, so useSyncExternalStore's Object.is check does not
  // loop; it only changes when a real data change advances data_version (or, in
  // the fallback, on each generated_at heartbeat).
  return useSyncExternalStore(
    subscribeStore,
    () => revalToken(getState().snapshot),
  );
}

export function SessionModal() {
  const sessionId = useSyncExternalStore(subscribeStore, () => getState().openSessionId);
  const revalTokenValue = useRevalToken();
  const display = useDisplayTz();
  const ctx: FmtCtx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  const [data, setData] = useState<SessionDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Tracks the last id we actually fetched against. A change means "new
  // session" → treat as initial (clear stale data, show spinner). Equal means
  // "same session, new tick" → stale-while-revalidate refetch.
  const lastResolvedIdRef = useRef<string | null>(null);
  // True iff the most recent refetch returned 404. A second consecutive 404
  // evicts content; a successful refetch (or non-404 error) clears this.
  const consecutive404Ref = useRef(false);
  // Bound session id: set once when openSessionId changes (or on mount).
  // Stays stable across SSE ticks so a changing newest-row in the snapshot
  // does NOT silently swap which session the modal is showing.
  const resolvedIdRef = useRef<string | null>(null);

  useEffect(() => {
    // Re-resolve the bound id when openSessionId changes (or on mount).
    // Reads the snapshot ONCE here; the fetch effect below uses this ref
    // and does not re-resolve on subsequent ticks.
    resolvedIdRef.current =
      sessionId ?? getState().snapshot?.sessions?.rows?.[0]?.session_id ?? null;
    // Reset the 404 arm so a new session does not inherit the prior
    // session's armed state.
    consecutive404Ref.current = false;
  }, [sessionId]);

  useEffect(() => {
    const id = resolvedIdRef.current;
    if (!id) {
      setLoading(false);
      setError('No session available.');
      setData(null);
      lastResolvedIdRef.current = null;
      return;
    }

    // `data == null` covers the interrupt-and-retry case: if a tick aborts an
    // in-flight initial fetch before it resolves, lastResolvedIdRef is already
    // set to id but no content has rendered. Without this guard the retry
    // would be classified as a refetch — a 404 would take the keep-stale path
    // (without clearing loading) and a network error would be silently
    // swallowed, leaving the modal stuck on the spinner.
    const isInitialFetch = lastResolvedIdRef.current !== id || data == null;
    lastResolvedIdRef.current = id;

    if (isInitialFetch) {
      // New session → clear stale data + show spinner.
      setLoading(true);
      setError(null);
      setData(null);
    }
    // else: refetch tick — keep `data` and `loading` exactly as they are.

    const ctl = new AbortController();
    fetch(`/api/session/${encodeURIComponent(id)}`, { signal: ctl.signal })
      .then(async (r) => {
        if (r.status === 404) {
          if (isInitialFetch || consecutive404Ref.current) {
            // Either: (a) initial fetch 404 — evict immediately, or
            // (b) second consecutive refetch 404 — likely permanent, evict.
            setLoading(false);
            setError('Session not found (the cache may have rolled forward).');
            setData(null);
          }
          // First refetch 404 (non-consecutive): keep stale, arm for next.
          consecutive404Ref.current = true;
          return;
        }
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const json = (await r.json()) as SessionDetail;
        consecutive404Ref.current = false;  // success clears the arm
        setData(json);
        setError(null);
        setLoading(false);
      })
      .catch((err) => {
        if ((err as DOMException).name === 'AbortError') return;
        consecutive404Ref.current = false;  // non-404 failure clears the arm
        if (isInitialFetch) {
          setError('Failed to load: ' + (err as Error).message);
          setLoading(false);
        }
        // Refetch failures (non-404) → silently keep stale data.
      });
    return () => ctl.abort();
  }, [sessionId, revalTokenValue]);

  return (
    <Modal
      title="Session detail"
      accentClass="accent-orange"
      headerExtras={
        <ShareIcon
          panel="sessions"
          panelLabel="Session"
          triggerId="session-modal"
          onClick={() => dispatch(openShareModal('sessions', 'session-modal'))}
        />
      }
    >
      <section className="modal-sessions">
        {loading ? (
          <div className="modal-loading" id="msess-loading">
            Loading session detail…
          </div>
        ) : null}

        {error ? (
          <div className="modal-error" id="msess-error">
            {error}
          </div>
        ) : null}

        {!loading && !error && data ? <SessionContent detail={data} ctx={ctx} /> : null}
      </section>
    </Modal>
  );
}

function SessionContent({ detail, ctx }: { detail: SessionDetail; ctx: FmtCtx }) {
  const paths = Array.isArray(detail.source_paths) ? detail.source_paths : [];
  const primary: string[] = [];
  const subagents: string[] = [];
  paths.forEach((p) => {
    if (SUBAGENT_RE.test(p)) subagents.push(p);
    else primary.push(p);
  });

  const tokenTiles: Array<[string, number, string | null]> = (
    [
      ['Input', detail.input_tokens, null],
      ['Output', detail.output_tokens, null],
      ['Cache creation', detail.cache_creation_tokens, null],
      ['Cache read', detail.cache_read_tokens, null],
      ['Cache hit %', detail.cache_hit_pct, 'cache-hit'],
    ] as Array<[string, number | null | undefined, string | null]>
  ).filter((t): t is [string, number, string | null] => t[1] != null);

  const emptyCost = !detail.cost_per_model || detail.cost_per_model.length === 0;

  // SE-2 — a single-model session's "Models" + "Cost by model" sections are
  // both degenerate (one chip; a 100% bar + one legend row). Collapse them into
  // one caption line. Multi-model (or empty) keeps the two sections as today.
  const oneModel = isSingleModel(detail);
  const soleModelName =
    detail.models?.[0]?.name ?? detail.cost_per_model?.[0]?.model ?? '—';
  const soleModelCost =
    detail.cost_per_model?.[0]?.cost_usd ?? detail.cost_total_usd ?? null;

  return (
    <div className="modal-content" id="msess-content">
      <div className="m-chipstrip">
        <span className="msess-badge" id="msess-id" aria-label="Session ID">
          {detail.session_id ?? '—'}
        </span>
      </div>

      <div className="m-hero cols-3">
        <div className="m-kv kv-cost">
          <svg className="icon" aria-hidden="true">
            <use href="/static/icons.svg#dollar" />
          </svg>
          <div>
            <div className="v" id="msess-cost">
              {detail.cost_total_usd != null
                ? '$' + detail.cost_total_usd.toFixed(2)
                : '—'}
            </div>
            <div className="lbl">Total cost</div>
          </div>
        </div>
        <div className="m-kv kv-dur">
          <svg className="icon" aria-hidden="true">
            <use href="/static/icons.svg#clock" />
          </svg>
          <div>
            <div className="v" id="msess-dur">
              {detail.duration_min != null ? detail.duration_min + ' min' : '—'}
            </div>
            <div className="lbl">Duration</div>
          </div>
        </div>
        <div className="m-kv kv-proj">
          <svg className="icon" aria-hidden="true">
            <use href="/static/icons.svg#folder" />
          </svg>
          <div>
            <div
              className="v"
              id="msess-project"
              title={detail.project_path ?? ''}
              aria-label="Project"
            >
              {detail.project_label ?? detail.project_path ?? '—'}
            </div>
            <div className="lbl">Project</div>
          </div>
        </div>
      </div>

      <div className="msess-ts">
        <svg className="icon" aria-hidden="true">
          <use href="/static/icons.svg#calendar" />
        </svg>
        <div>
          <span className="k">started</span>
          <span className="v" id="msess-started">
            {fmt.datetimeShort(detail.started_utc, ctx)}
          </span>
        </div>
        <div>
          <span className="k">last activity</span>
          <span className="v" id="msess-last">
            {fmt.datetimeShort(detail.last_activity_utc, ctx)}
          </span>
        </div>
      </div>

      <h3 className="m-sec sec-tok">
        <svg className="icon" aria-hidden="true">
          <use href="/static/icons.svg#hash" />
        </svg>
        Tokens
      </h3>
      <div className="msess-tok-grid" id="msess-tokens">
        {tokenTiles.map(([label, value, flavor]) => (
          <div
            key={label}
            className={'msess-tok-tile' + (flavor ? ' ' + flavor : '')}
          >
            <div className="lbl">{label}</div>
            <div className="n">
              {flavor === 'cache-hit' ? value.toFixed(1) + '%' : value.toLocaleString('en-US')}
            </div>
            {flavor === 'cache-hit' ? (
              <div className="bar">
                <div
                  className="fill"
                  style={{ width: Math.max(0, Math.min(value, 100)) + '%' }}
                />
              </div>
            ) : null}
          </div>
        ))}
      </div>

      <CacheRebuildsSection
        key={detail.session_id ?? 'no-session'}
        sessionId={detail.session_id ?? null}
      />

      {oneModel ? (
        <div className="msess-model-caption" id="msess-model-caption">
          <span className={'sw ' + modelChipClass(soleModelName)} aria-hidden="true" />
          <span className="k">Model</span>
          <span className="name">{soleModelName}</span>
          <span className="dot" aria-hidden="true">·</span>
          <span className="v">
            {soleModelCost != null ? '$' + soleModelCost.toFixed(2) : '—'}
          </span>
        </div>
      ) : (
        // #260 — multi-model sessions render the shared `ModelCostBars` under a
        // single "Cost by model" section (History-card / PeriodDetailCard
        // parity), replacing the former bespoke segmented-bar + legend AND the
        // now-redundant standalone "Models" chip strip (ModelCostBars already
        // renders a `modelChipClass` colour chip per model). Session
        // `cost_per_model` carries only the canonical id, so the friendly short
        // chip label comes from `abbreviateModel` (matching History's
        // server-provided `display`); a null cost coerces to 0 for the
        // relative-to-top bar. Rows already arrive cost-descending from the
        // server's `model_breakdowns`, satisfying ModelCostBars' rows[0]=max.
        !emptyCost ? (
          <>
            <h3 className="m-sec sec-costm">
              <svg className="icon" aria-hidden="true">
                <use href="/static/icons.svg#pie-chart" />
              </svg>
              Cost by model
            </h3>
            <ModelCostBars
              rows={(detail.cost_per_model || []).map((c) => ({
                model: c.model,
                cost_usd: c.cost_usd ?? 0,
                label: abbreviateModel(c.model),
              }))}
            />
          </>
        ) : null
      )}

      {paths.length > 0 ? (
        <>
          <h3 className="m-sec sec-src">
            <svg className="icon" aria-hidden="true">
              <use href="/static/icons.svg#file-text" />
            </svg>
            Source files
          </h3>
          <div className="msess-src" id="msess-src">
            <div className="src-head">
              <span className="count-pill">
                {paths.length} file{paths.length === 1 ? '' : 's'}
              </span>
              <span className="sub">
                {primary.length} primary
                {subagents.length > 0 ? (
                  <>
                    {' '}
                    <span className="dot">·</span>{' '}
                    <span className="subcount">
                      {subagents.length} subagent{subagents.length === 1 ? '' : 's'}
                    </span>
                  </>
                ) : null}
              </span>
            </div>
            {primary.length > 0
              ? primary.map((p) => (
                  <div key={p} className="primary-path">
                    {p}
                  </div>
                ))
              : subagents.length > 0 ? (
                  <div className="src-empty-primary">
                    No primary path resolved · {subagents.length} subagent path
                    {subagents.length === 1 ? '' : 's'} below
                  </div>
                )
              : null}
            {subagents.length > 0 ? (
              <details className="subagents" open={primary.length === 0}>
                <summary>
                  Show {subagents.length} subagent path
                  {subagents.length === 1 ? '' : 's'}
                </summary>
                <ul className="paths">
                  {subagents.map((p) => (
                    <li key={p}>{p}</li>
                  ))}
                </ul>
              </details>
            ) : null}
          </div>
        </>
      ) : null}
    </div>
  );
}
