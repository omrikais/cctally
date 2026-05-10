import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { refreshUpdateState } from '../store/update';
import { UpdateStream } from './UpdateStream';

// SSE-driven running modal. Subscribes to /api/update/stream/<runId>
// and pushes each event into the store via APPEND_UPDATE_STREAM. The
// terminal events (`exit` rc=0 → success, `exit` rc!=0 / `error_event`
// → failed, `execvp` → success+restart) drive SET_UPDATE_STATUS.
//
// Restrictive-proxy fallback (spec §6.5): if the EventSource handshake
// hasn't fired *any* event within 3 s of subscribe, fall back to
// polling /api/update/status at 1 Hz. The poll loop watches the
// `current_run_id` field — when it goes null AND state.current_version
// matches state.latest_version we infer success; otherwise we infer
// failed and surface a generic "stream unavailable" message.
//
// Server uses event name `error_event` (not `error`) to avoid clashing
// with EventSource's connection-error semantics (`onerror` fires
// continuously during reconnect attempts; we'd misclassify a transient
// disconnect as a worker error).

const SSE_HANDSHAKE_TIMEOUT_MS = 3000;
const POLLING_INTERVAL_MS = 1000;

function _formatElapsed(startedAt: number | null): string {
  if (startedAt == null) return '0s';
  const s = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}m ${r}s`;
}

export function UpdateRunningModal() {
  const update = useSyncExternalStore(subscribeStore, () => getState().update);
  const runId = update.runId;
  const startedAt = update.startedAt;
  const cmd = update.state?.update_command ?? '';
  const latest = update.state?.latest_version ?? '';

  // Re-render once per second so the elapsed timer stays accurate.
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, []);

  // SSE consumer + polling fallback. The handshake-timeout watchdog
  // arms on subscribe and clears on first event; if it fires we
  // close the EventSource and start polling. The polling loop reads
  // /api/update/status and watches for a null `current_run_id` to
  // detect terminal state.
  const handshakeArmedRef = useRef(false);
  useEffect(() => {
    if (!runId) return;

    let es: EventSource | null = null;
    let pollTimer: number | null = null;
    let handshakeTimer: number | null = null;
    let cancelled = false;
    handshakeArmedRef.current = true;

    function clearHandshake() {
      if (handshakeTimer != null) {
        window.clearTimeout(handshakeTimer);
        handshakeTimer = null;
      }
      handshakeArmedRef.current = false;
    }

    function pushEvent(ev: Event, type: string) {
      clearHandshake();
      // MessageEvent carries the JSON body in `.data`. The Python
      // server writes `data: {...}` per SSE frame; values like rc, ts,
      // step are flattened into that payload (see
      // bin/cctally:_handle_get_update_stream).
      let payload: Record<string, unknown> = {};
      try {
        if (ev instanceof MessageEvent && typeof ev.data === 'string') {
          payload = JSON.parse(ev.data);
        }
      } catch {
        /* malformed event; treat as empty payload */
      }
      dispatch({
        type: 'APPEND_UPDATE_STREAM',
        event: {
          type: type as 'stdout' | 'stderr' | 'step' | 'exit' | 'execvp' | 'error_event' | 'done' | 'heartbeat',
          data: typeof payload.data === 'string' ? payload.data : undefined,
          name: typeof payload.name === 'string' ? payload.name : undefined,
          rc: typeof payload.rc === 'number' ? payload.rc : undefined,
          step: typeof payload.step === 'string' ? payload.step : undefined,
          message: typeof payload.message === 'string' ? payload.message : undefined,
          ts: typeof payload.ts === 'number' ? payload.ts : undefined,
        },
      });
      // Terminal events drive the status machine.
      //
      // `exit` is a STEP boundary, not a terminal worker signal. Brew's
      // two-step flow (`brew update` → `brew upgrade cctally`) emits an
      // `exit rc=0 step="brew update"` BEFORE step 2 starts, and an
      // earlier version of this branch flipped to success on that first
      // zero exit — masking failures from `brew upgrade cctally`. The
      // canonical success transition is `execvp` (handled below); only
      // a non-zero exit short-circuits to `failed`.
      if (type === 'exit') {
        const rc = typeof payload.rc === 'number' ? payload.rc : -1;
        if (rc !== 0) {
          dispatch({
            type: 'SET_UPDATE_STATUS',
            status: 'failed',
            errorMessage: `subprocess exited with rc=${rc}`,
          });
        }
        // rc===0 is a step boundary; success transition awaits execvp.
      } else if (type === 'execvp') {
        // Server is about to replace itself in place. Flip to success
        // and let the existing /api/events reconnect logic re-establish
        // against the new server. refreshUpdateState polling will
        // close the modal once current_version === latest_version.
        dispatch({ type: 'SET_UPDATE_STATUS', status: 'success' });
      } else if (type === 'error_event') {
        const msg =
          typeof payload.message === 'string' ? payload.message : 'update worker error';
        dispatch({
          type: 'SET_UPDATE_STATUS',
          status: 'failed',
          errorMessage: msg,
        });
      } else if (type === 'done') {
        // Worker finished cleanly — generator closes. Status is
        // already success via prior `exit` rc=0.
        if (es) {
          es.close();
          es = null;
        }
      }
    }

    function startPolling() {
      // Fall through to /api/update/status polling. The dashboard's
      // own /api/data SSE may still be flowing fine — only the update
      // stream endpoint is gated through the proxy (some corp proxies
      // buffer text/event-stream into 4 KB chunks and effectively kill
      // SSE; status polling is HTTP/1.1-friendly and works through
      // anything that proxies /api/data).
      if (pollTimer != null) return;
      pollTimer = window.setInterval(async () => {
        if (cancelled) return;
        try {
          const r = await fetch('/api/update/status');
          if (!r.ok) return;
          const body = await r.json();
          const currentRunId = body?.current_run_id ?? null;
          const stateRaw = body?.state ?? {};
          if (currentRunId === null) {
            // Worker terminated. Decide success vs failed by comparing
            // current_version against latest_version (post-execvp the
            // dashboard has reloaded with the new binary).
            const cur = stateRaw?.current_version;
            const lat = stateRaw?.latest_version;
            const sliceNow = getState().update;
            if (typeof cur === 'string' && typeof lat === 'string' && cur === lat) {
              dispatch({ type: 'SET_UPDATE_STATUS', status: 'success' });
            } else if (sliceNow.status === 'running') {
              dispatch({
                type: 'SET_UPDATE_STATUS',
                status: 'failed',
                errorMessage: 'update worker exited (stream unavailable)',
              });
            }
            // Push a synthetic terminal marker so the stream viewer
            // shows *something*. The render-no-events branch otherwise
            // shows "Waiting for output…" forever.
            if (sliceNow.stream.length === 0) {
              dispatch({
                type: 'APPEND_UPDATE_STREAM',
                event: {
                  type: 'stdout',
                  data:
                    '(SSE stream unavailable; ' +
                    'install completed in background — see ~/.local/share/cctally/update.log for full output)',
                },
              });
            }
            if (pollTimer != null) {
              window.clearInterval(pollTimer);
              pollTimer = null;
            }
          }
        } catch {
          /* polling blip — try again next tick */
        }
      }, POLLING_INTERVAL_MS);
    }

    handshakeTimer = window.setTimeout(() => {
      if (cancelled) return;
      // No event ever arrived. Drop the SSE and start polling.
      if (handshakeArmedRef.current) {
        handshakeArmedRef.current = false;
        if (es) {
          es.close();
          es = null;
        }
        startPolling();
      }
    }, SSE_HANDSHAKE_TIMEOUT_MS);

    try {
      es = new EventSource(`/api/update/stream/${encodeURIComponent(runId)}`);
      // Generic message-without-event-name (defensive — server always
      // names events but a proxy strip would otherwise lose them).
      es.addEventListener('message', (ev) => pushEvent(ev, 'stdout'));
      ['stdout', 'stderr', 'step', 'exit', 'execvp', 'error_event', 'done', 'heartbeat'].forEach(
        (name) => {
          es!.addEventListener(name, (ev) => pushEvent(ev as MessageEvent, name));
        },
      );
      es.onerror = () => {
        // EventSource auto-reconnects on its own; only fall back to
        // polling if we never saw an event in the first place. Once
        // the handshake is past, treat onerror as a reconnect and
        // ride it out — terminal events have already been delivered.
        if (handshakeArmedRef.current) {
          clearHandshake();
          if (es) {
            es.close();
            es = null;
          }
          startPolling();
        }
      };
    } catch {
      clearHandshake();
      startPolling();
    }

    // Always re-fetch state on close so the success modal's auto-close
    // can fire when the post-execvp current_version matches latest.
    return () => {
      cancelled = true;
      if (handshakeTimer != null) {
        window.clearTimeout(handshakeTimer);
        handshakeTimer = null;
      }
      if (pollTimer != null) {
        window.clearInterval(pollTimer);
        pollTimer = null;
      }
      if (es) {
        es.close();
        es = null;
      }
      handshakeArmedRef.current = false;
      // Best-effort refresh after disconnect so the auto-close
      // condition (current === latest) re-evaluates.
      refreshUpdateState();
    };
  }, [runId]);

  return (
    <div className="update-modal-body">
      <div className="update-running-row">
        <span className="update-spinner" aria-hidden="true">
          ⟳
        </span>
        <code className="update-cmd update-cmd-inline">{cmd || 'updating cctally'}</code>
        <span className="update-elapsed">{_formatElapsed(startedAt)} elapsed</span>
      </div>
      <UpdateStream events={update.stream} />
      <p className="update-stream-cap">
        Output streams over SSE · press Esc to hide (install continues to {latest || 'latest'})
      </p>
    </div>
  );
}
