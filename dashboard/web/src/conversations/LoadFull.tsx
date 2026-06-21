import { useEffect } from 'react';
import { useFullPayload } from '../hooks/useFullPayload';
import { useSessionId } from './TranscriptContext';
import { SpinnerIcon } from './ConvIcons';
import type { FullPayload } from '../types/conversation';

// Shared "load full result/input" affordance for the diff + terminal cards
// (#178, spec §4.4). Renders only when the card already knows its payload was
// truncated. States: idle → a button ("showing X of Y · <label>") → a
// reduced-motion-safe spinner while fetching → on success it calls onLoaded
// (the card swaps in the full payload and the affordance collapses to nothing)
// or a friendly error note (403/410/network). The fetch + per-(toolUseId,which)
// cache + 410 handling all live in useFullPayload; this is the thin view.
export function LoadFull({
  toolUseId,
  which,
  fullLength,
  label,
  onLoaded,
}: {
  toolUseId: string;
  which: 'result' | 'input';
  fullLength: number | null;
  label: string;
  onLoaded: (payload: FullPayload) => void;
}) {
  const sessionId = useSessionId();
  const state = useFullPayload(sessionId, toolUseId, which);

  // Hand the loaded payload up exactly once when the fetch resolves. The hook
  // caches `done`, so this effect fires a single time per successful load.
  useEffect(() => {
    if (state.status === 'done') onLoaded(state.data);
    // onLoaded is a stable card callback; depend on the resolved payload only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.status, state.status === 'done' ? state.data : null]);

  // Once the card has swapped in the full payload, the affordance disappears.
  if (state.status === 'done') return null;

  if (state.status === 'loading') {
    return (
      <div className="conv-loadfull conv-loadfull--loading">
        <span className="conv-loadfull-spinner" aria-hidden="true">
          <SpinnerIcon />
        </span>
        <span className="conv-loadfull-label">loading…</span>
      </div>
    );
  }

  if (state.status === 'error') {
    return (
      <div className="conv-loadfull">
        <span className="conv-loadfull-err">{state.error}</span>
      </div>
    );
  }

  const showing = fullLength != null ? `showing capped view of ${fullLength} chars · ` : '';
  // #217 S3 E10#4 — a11y/disabled affordance. Double-fetch is already prevented
  // by useFullPayload (inFlightRef/doneRef). The remaining gap is that the idle
  // button looked actionable even when load() would no-op (no open session id),
  // so disable it in that case rather than leaving a dead-click button.
  return (
    <div className="conv-loadfull">
      <button
        type="button"
        className="conv-loadfull-btn"
        disabled={!sessionId}
        onClick={() => state.load()}
      >
        {showing}
        {label}
      </button>
    </div>
  );
}
