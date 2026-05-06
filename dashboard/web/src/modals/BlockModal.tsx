import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { getState, subscribeStore } from '../store/store';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { Modal } from './Modal';
import { BlockTimeline } from './BlockTimeline';
import { fmt, type FmtCtx } from '../lib/fmt';
import { modelChipClass } from '../lib/model';
import type { BlockDetail } from '../types/envelope';

// SSE-driven live updates: same lifecycle as SessionModal.tsx — refetch
// /api/block/:start_at on each new snapshot.generated_at, with stale-
// while-revalidate semantics, 404-grace, bound-id stability, and the
// `data == null` interrupt-and-retry guard. See SessionModal for the
// canonical comments — this file repeats only the load-bearing ones.

function useGeneratedAt(): string {
  return useSyncExternalStore(
    subscribeStore,
    () => getState().snapshot?.generated_at ?? '',
  );
}

function fmtElapsed(detail: BlockDetail, nowIso: string): string {
  const startMs = Date.parse(detail.start_at);
  const endRef = detail.is_active
    ? (Date.parse(nowIso) || Date.now())
    : (detail.actual_end_at
       ? Date.parse(detail.actual_end_at)
       : Date.parse(detail.end_at));
  const minutes = Math.max(0, Math.floor((endRef - startMs) / 60000));
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return `${h}h ${String(m).padStart(2, '0')}m`;
}

function fmtWindow(start: string, end: string, ctx: FmtCtx): string {
  // F1: was `toISOString().slice(11,16) + ' UTC'` — replaced with the
  // tz-aware `fmt.timeHHmm`, which appends the suffix from
  // ctx.offsetLabel. Both ends carry the suffix so the rendered text
  // remains readable when copied without surrounding context.
  return `${fmt.timeHHmm(start, ctx)} → ${fmt.timeHHmm(end, ctx)}`;
}

export function BlockModal() {
  const startAt = useSyncExternalStore(subscribeStore,
                                        () => getState().openBlockStartAt);
  const generatedAt = useGeneratedAt();
  const display = useDisplayTz();
  const ctx: FmtCtx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  const [data, setData] = useState<BlockDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const lastResolvedIdRef = useRef<string | null>(null);
  const consecutive404Ref = useRef(false);
  const resolvedIdRef = useRef<string | null>(null);

  useEffect(() => {
    resolvedIdRef.current = startAt ?? null;
    consecutive404Ref.current = false;
  }, [startAt]);

  useEffect(() => {
    const id = resolvedIdRef.current;
    if (!id) {
      setLoading(false);
      setError('No block bound.');
      setData(null);
      lastResolvedIdRef.current = null;
      return;
    }
    // Same data == null clause as SessionModal — see the canonical comment
    // there for why this is load-bearing.
    const isInitialFetch = lastResolvedIdRef.current !== id || data == null;
    lastResolvedIdRef.current = id;
    if (isInitialFetch) {
      setLoading(true);
      setError(null);
      setData(null);
    }
    const ctl = new AbortController();
    fetch(`/api/block/${encodeURIComponent(id)}`, { signal: ctl.signal })
      .then(async (r) => {
        if (r.status === 404) {
          if (isInitialFetch || consecutive404Ref.current) {
            setLoading(false);
            setError('Block not found (the cache may have rolled forward).');
            setData(null);
          }
          consecutive404Ref.current = true;
          return;
        }
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const json = (await r.json()) as BlockDetail;
        consecutive404Ref.current = false;
        setData(json);
        setError(null);
        setLoading(false);
      })
      .catch((err) => {
        if ((err as DOMException).name === 'AbortError') return;
        consecutive404Ref.current = false;
        if (isInitialFetch) {
          setError('Failed to load: ' + (err as Error).message);
          setLoading(false);
        }
      });
    return () => ctl.abort();
  }, [startAt, generatedAt]);

  const title = data
    ? `${data.anchor === 'heuristic' ? '~ ' : ''}Block · ${data.label}`
    : 'Block';

  return (
    <Modal title={title} accentClass="accent-blue">
      <section className="modal-block">
        {loading ? (
          <div className="modal-loading">Loading block detail…</div>
        ) : null}
        {error ? <div className="modal-error">{error}</div> : null}
        {!loading && !error && data
          ? <BlockContent detail={data} generatedAt={generatedAt} ctx={ctx} />
          : null}
      </section>
    </Modal>
  );
}

function BlockContent({
  detail, generatedAt, ctx,
}: {
  detail: BlockDetail; generatedAt: string; ctx: FmtCtx;
}) {
  const totalCost = detail.cost_usd ?? 0;
  return (
    <div className="modal-content">
      <div className="m-chipstrip">
        {detail.is_active ? (
          <span className="m-pill accent-green">● Active</span>
        ) : null}
        {detail.anchor === 'heuristic' ? (
          <span className="m-pill accent-amber">~ heuristic anchor</span>
        ) : null}
        <span className="m-pill accent-blue">
          {fmtWindow(detail.start_at, detail.end_at, ctx)}
        </span>
        <span className="m-pill">
          {detail.entries_count.toLocaleString('en-US')}{' '}
          {detail.entries_count === 1 ? 'entry' : 'entries'}
        </span>
      </div>

      <div className={'m-hero ' + (detail.is_active ? 'cols-4' : 'cols-3')}>
        <div className="m-kv kv-cost">
          <svg className="icon" aria-hidden="true">
            <use href="/static/icons.svg#dollar" />
          </svg>
          <div>
            <div className="v">{fmt.usd2(detail.cost_usd)}</div>
            <div className="lbl">Total cost</div>
          </div>
        </div>
        <div className="m-kv kv-dur">
          <svg className="icon" aria-hidden="true">
            <use href="/static/icons.svg#clock" />
          </svg>
          <div>
            <div className="v">{fmtElapsed(detail, generatedAt)}</div>
            <div className="lbl">Elapsed</div>
          </div>
        </div>
        <div className="m-kv">
          <svg className="icon" aria-hidden="true">
            <use href="/static/icons.svg#hash" />
          </svg>
          <div>
            <div className="v">{detail.total_tokens.toLocaleString('en-US')}</div>
            <div className="lbl">Total tokens</div>
          </div>
        </div>
        {detail.is_active ? (
          <div className="m-kv kv-proj">
            <svg className="icon" aria-hidden="true">
              <use href="/static/icons.svg#crystal-ball" />
            </svg>
            <div>
              <div className="v">
                {detail.projection
                  ? fmt.usd2(detail.projection.total_cost_usd)
                  : '—'}
              </div>
              <div className="lbl">
                Projected
                {detail.projection
                  ? ` · ${detail.projection.remaining_minutes}m left`
                  : ''}
              </div>
            </div>
          </div>
        ) : null}
      </div>

      <BlockTimeline detail={detail} />

      <h3 className="m-sec sec-tok">
        <svg className="icon" aria-hidden="true">
          <use href="/static/icons.svg#hash" />
        </svg>
        Tokens
      </h3>
      <div className="msess-tok-grid mblock-tok-grid">
        <div className="msess-tok-tile">
          <div className="lbl">Input</div>
          <div className="n">{detail.input_tokens.toLocaleString('en-US')}</div>
        </div>
        <div className="msess-tok-tile">
          <div className="lbl">Output</div>
          <div className="n">{detail.output_tokens.toLocaleString('en-US')}</div>
        </div>
        <div className="msess-tok-tile">
          <div className="lbl">Cache create</div>
          <div className="n">{detail.cache_creation_tokens.toLocaleString('en-US')}</div>
        </div>
        <div className="msess-tok-tile">
          <div className="lbl">Cache read</div>
          <div className="n">{detail.cache_read_tokens.toLocaleString('en-US')}</div>
        </div>
        {detail.cache_hit_pct != null ? (
          <div className="msess-tok-tile cache-hit">
            <div className="lbl">Cache hit %</div>
            <div className="n">{detail.cache_hit_pct.toFixed(1)}%</div>
            <div className="bar">
              <div className="fill"
                   style={{ width: Math.min(100, Math.max(0, detail.cache_hit_pct)) + '%' }} />
            </div>
          </div>
        ) : null}
      </div>

      {detail.models.length > 0 ? (
        <>
          <h3 className="m-sec sec-costm">
            <svg className="icon" aria-hidden="true">
              <use href="/static/icons.svg#pie-chart" />
            </svg>
            Cost by model
          </h3>
          <div className="msess-costm">
            <div className="bar">
              {detail.models.map((m) => {
                const pct = totalCost > 0 ? (m.cost_usd / totalCost) * 100 : 0;
                return (
                  <div key={m.model}
                       className={'seg ' + modelChipClass(m.model)}
                       style={{ width: pct + '%' }} />
                );
              })}
            </div>
            <div className="legend">
              {detail.models.map((m) => {
                const pct = totalCost > 0 ? (m.cost_usd / totalCost) * 100 : 0;
                return (
                  <div key={m.model} className="lg">
                    <span className={'sw ' + modelChipClass(m.model)} />
                    <span className="name">{m.display}</span>
                    <span className="v">${m.cost_usd.toFixed(2)}</span>
                    <span className="pct">{Math.round(pct)}%</span>
                  </div>
                );
              })}
            </div>
          </div>
        </>
      ) : null}

      {detail.is_active && detail.burn_rate && detail.projection ? (
        <>
          <h3 className="m-sec sec-burn">
            <svg className="icon" aria-hidden="true">
              <use href="/static/icons.svg#crystal-ball" />
            </svg>
            Burn rate &amp; projection
          </h3>
          <div className="mblock-burn">
            <div className="row">
              <span className="k">Burn rate</span>
              <span className="v amber">{fmt.usd2(detail.burn_rate.cost_per_hour)}</span>
              <span className="dim">/ hr</span>
              <span className="sep">·</span>
              <span className="v cyan">
                {Math.round(detail.burn_rate.tokens_per_minute).toLocaleString('en-US')}
              </span>
              <span className="dim">tok / min</span>
            </div>
            <div className="row">
              <span className="k">Projection</span>
              <span className="v amber">{fmt.usd2(detail.projection.total_cost_usd)}</span>
              <span className="dim">total</span>
              <span className="sep">·</span>
              <span className="v cyan">
                {detail.projection.total_tokens.toLocaleString('en-US')}
              </span>
              <span className="dim">tok</span>
              <span className="sep">·</span>
              <span className="v">{detail.projection.remaining_minutes} min</span>
              <span className="dim">remaining</span>
            </div>
          </div>
        </>
      ) : null}
    </div>
  );
}
