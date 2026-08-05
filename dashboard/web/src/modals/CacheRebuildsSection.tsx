import { useMemo, useState, useSyncExternalStore } from 'react';
import { dispatch, getState, selectMarkersEnabled, subscribeStore } from '../store/store';
import { useConversationOutline } from '../hooks/useConversationOutline';
import { useSnapshot } from '../hooks/useSnapshot';
import { ctxFromEnvelope } from '../store/selectors';
import { fmt } from '../lib/fmt';
import {
  buildOutlineTargets, outlineTurnVisible, resolveTurnIndex,
} from '../conversations/outlineNavigation';
import { buildConversationJump, legacyClaudeConversationRef } from '../types/conversation';

const CAP = 3;

// Session-modal cache-rebuilds (2026-06-16 spec). Self-contained: fetches the
// outline via the shared hook (stale-drop + failure handled there), gates on the
// client-side markers selector, and dispatches the compound OPEN_CONVERSATION
// (which switches view + dismisses the modal) to jump to each rebuild turn.
export function CacheRebuildsSection({ sessionId }: { sessionId: string | null }) {
  const markersEnabled = useSyncExternalStore(
    subscribeStore, () => selectMarkersEnabled(getState()));
  // Only fetch when markers are on AND we have a session.
  const { outline } = useConversationOutline(markersEnabled ? sessionId : null);
  const env = useSnapshot();
  const ctx = useMemo(() => ctxFromEnvelope(env),
    [env?.display?.resolved_tz, env?.display?.offset_label]);
  const [expanded, setExpanded] = useState(false);

  // The shared hook returns whatever JSON the server sent; a malformed or
  // wrong-shape response (no `stats`) degrades to nothing, never a crash.
  if (!markersEnabled || !outline || !outline.stats || !sessionId) return null;

  const stats = outline.stats;
  const cf = stats.cache_failures;
  const count = cf?.count ?? 0;
  const saved = stats.cache_saved_usd ?? 0;
  const rebuilds = cf?.rebuilds ?? [];
  const shown = expanded ? rebuilds : rebuilds.slice(0, CAP);
  const sid = sessionId;

  // #463 S4 remediation round 3 (F8) — the SAME jump every outline rail row
  // performs, on the sibling that lives outside the rail. A same-session
  // OPEN_CONVERSATION preserves `convFocusMode` by design (the caller is the
  // authority), so without this reset a click here while the viewer held that
  // session under `prompts` or `errors` loaded a turn the filter had unmounted.
  // The mode is read at click time from the store rather than subscribed to,
  // because nothing in this section's rendering depends on it.
  const jumpTo = (uuid: string) => {
    const turns = outline.turns ?? [];
    const ownerIndex = resolveTurnIndex(
      buildOutlineTargets(turns, undefined, outline.landmarks), uuid);
    const turn = ownerIndex !== undefined ? turns[ownerIndex] : undefined;
    const mode = getState().convFocusMode;
    if (turn && mode !== 'all' && !outlineTurnVisible(turn, mode)) {
      dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'all' });
    }
    dispatch({
      type: 'OPEN_CONVERSATION',
      sessionId: sid,
      // The bare Claude session id this modal is given is the unqualified form,
      // so the payload is `{session_id, uuid}` — byte-identical to the literal
      // this replaced, now built by the one shared builder.
      jump: buildConversationJump(legacyClaudeConversationRef(sid), uuid, false),
    });
  };

  return (
    <>
      <h3 className="m-sec sec-rebuild">
        <svg className="icon" aria-hidden="true">
          <use href="/static/icons.svg#refresh" />
        </svg>
        Cache rebuilds
      </h3>

      {count === 0 ? (
        <div className="msess-rebuild-none" id="msess-rebuild-none">
          No cache rebuilds ✓
        </div>
      ) : (
        <>
          <div className="msess-tok-grid msess-rebuild-tiles" id="msess-rebuild-tiles">
            <div className="msess-tok-tile">
              <div className="lbl">Rebuilds</div>
              <div className="n">{count}</div>
            </div>
            <div className="msess-tok-tile rebuild-wasted">
              <div className="lbl">Wasted</div>
              <div className="n">{fmt.usd2(cf!.est_wasted_usd)}</div>
            </div>
            <div className="msess-tok-tile">
              <div className="lbl">Re-created</div>
              <div className="n">{cf!.tokens_recreated.toLocaleString('en-US')}</div>
            </div>
          </div>

          <ul className="msess-rebuild-list" id="msess-rebuild-list">
            {shown.map((r) => (
              <li key={r.uuid} className="rebuild-row">
                <button
                  type="button"
                  className="rebuild-jump"
                  onClick={() => jumpTo(r.uuid)}
                >
                  <span className="rb-cost">{fmt.usd2(r.est_wasted_usd)}</span>
                  <span className="rb-tok">
                    {r.tokens_recreated.toLocaleString('en-US')} tok
                  </span>
                  <span className="rb-time">
                    {r.ts ? fmt.timeHHmm(r.ts, ctx, { noSuffix: true }) : ''}
                  </span>
                  {r.subagent_key ? <span className="rb-sub">subagent</span> : null}
                  <span className="rb-go">→ Jump</span>
                </button>
              </li>
            ))}
          </ul>

          {rebuilds.length > CAP && !expanded ? (
            <button
              type="button"
              className="msess-rebuild-more"
              onClick={() => setExpanded(true)}
            >
              +{rebuilds.length - CAP} more
            </button>
          ) : null}
        </>
      )}

      {saved > 0 ? (
        <div className="msess-rebuild-saved" id="msess-rebuild-saved">
          Cache saved this session {fmt.usd2(saved)} ✓
        </div>
      ) : null}
    </>
  );
}
