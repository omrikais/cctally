// Spec §6.6 — gallery tile's "presets ▾" affordance.
//
// M2 surfaces saved presets for the current panel; M4.3 adds the
// "Recent shares" history group below them. Both fetched lazily on open
// so the modal mount stays cheap.
//
// Picking a preset OR a history row calls `onPick(template_id, options)`;
// the parent (ShareModal) overwrites its local recipe state. History
// rows never auto-export — spec §6.6 mandates re-confirm. The "Manage
// presets…" footer item invokes `onManage()` so the parent can hoist
// <ManagePresetsModal>.
//
// Close-on-outside-click follows the M1 modal precedent (mousedown on
// document, ignored when the click lands inside our container). We do
// NOT register an overlay-scope Esc binding — that would steal Esc
// from the share modal's own close handler. Esc closes the modal
// (which un-mounts this dropdown); for "just close the menu" the user
// re-clicks the trigger or any other element.
import { useEffect, useRef, useState } from 'react';
import {
  listPresets,
  listHistory,
  type PresetRecord,
  type HistoryRecord,
  ShareApiError,
} from './presetsApi';
import type { SharePanelId, ShareOptions } from './types';

interface Props {
  panel: SharePanelId;
  onPick: (template_id: string, options: ShareOptions) => void;
  onManage: () => void;
}

// Render the dropdown's history timestamp in the user's locale. Falls
// back to the raw ISO string when the parse fails (defensive — the
// server always emits valid ISO-8601, but a future schema change
// shouldn't blow up the menu).
function formatHistoryTimestamp(isoUtc: string): string {
  const d = new Date(isoUtc);
  if (Number.isNaN(d.getTime())) return isoUtc;
  return d.toLocaleString();
}

export function PresetDropdown({ panel, onPick, onManage }: Props) {
  const [open, setOpen] = useState(false);
  const [presets, setPresets] = useState<Record<string, PresetRecord>>({});
  const [history, setHistory] = useState<HistoryRecord[]>([]);
  const [error, setError] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

  // Lazy fetch on open. Re-fetch on panel change so switching panels
  // (rare — the modal is per-panel) doesn't show stale presets/history.
  //
  // Presets + history fetched in parallel via Promise.all so the
  // dropdown shows everything in one render — no flicker between the
  // two groups. If either request fails we still surface the error,
  // but the other's data is rendered.
  useEffect(() => {
    if (!open) return;
    const ac = new AbortController();
    setError(null);
    Promise.all([
      listPresets({ signal: ac.signal }),
      listHistory({ signal: ac.signal }),
    ])
      .then(([presResp, histResp]) => {
        setPresets(presResp.presets[panel] ?? {});
        // Filter to the current panel and reverse so newest is first
        // (server stores newest last for ring-buffer semantics; users
        // expect "most recent at top" in a recall dropdown).
        setHistory(
          histResp.history
            .filter((h) => h.panel === panel)
            .slice()
            .reverse(),
        );
      })
      .catch((err: unknown) => {
        if ((err as { name?: string })?.name === 'AbortError') return;
        const msg =
          err instanceof ShareApiError
            ? err.message ?? `HTTP ${err.status}`
            : (err as Error).message;
        setError(msg ?? 'Failed to load presets');
      });
    return () => ac.abort();
  }, [open, panel]);

  // Click-outside closes the menu. mousedown so the press registers
  // before the trigger's click handler toggles `open` back to true.
  useEffect(() => {
    if (!open) return;
    function handler(e: MouseEvent) {
      const root = containerRef.current;
      if (!root) return;
      if (!root.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  const names = Object.keys(presets).sort();

  return (
    <div className="share-presets-dropdown" ref={containerRef}>
      <button
        type="button"
        className="share-presets-trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        presets ▾
      </button>
      {open ? (
        <div className="share-presets-menu" role="menu">
          {error ? (
            <div className="share-presets-error" role="alert">{error}</div>
          ) : null}
          {names.length === 0 && !error ? (
            <div className="share-presets-empty">No saved presets yet.</div>
          ) : null}
          {names.length > 0 ? (
            <ul className="share-presets-list">
              {names.map((n) => (
                <li key={n}>
                  <button
                    type="button"
                    role="menuitem"
                    className="share-presets-item"
                    onClick={() => {
                      onPick(presets[n].template_id, presets[n].options);
                      setOpen(false);
                    }}
                  >
                    {n}
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
          {history.length > 0 ? (
            <div className="share-presets-history">
              <div
                className="share-presets-history-heading"
                role="presentation"
              >
                Recent shares
              </div>
              <ul className="share-presets-history-list">
                {history.map((h) => (
                  <li key={h.recipe_id}>
                    <button
                      type="button"
                      role="menuitem"
                      className="share-presets-history-item"
                      onClick={() => {
                        onPick(h.template_id, h.options);
                        setOpen(false);
                      }}
                      title={`${h.template_id} · ${h.format ?? 'unknown'} · ${h.exported_at}`}
                    >
                      <span className="share-presets-history-template">
                        {h.template_id}
                      </span>
                      <span className="share-presets-history-meta">
                        {' · '}{h.format ?? 'unknown'}{' · '}{formatHistoryTimestamp(h.exported_at)}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          <div className="share-presets-footer">
            <button
              type="button"
              role="menuitem"
              className="share-presets-manage"
              onClick={() => { onManage(); setOpen(false); }}
            >
              Manage presets…
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
