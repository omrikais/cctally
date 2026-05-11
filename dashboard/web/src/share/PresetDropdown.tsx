// Spec §6.6 — gallery tile's "presets ▾" affordance.
//
// M2 surfaces saved presets for the current panel; the "Recent shares"
// history group lands in M4.3 as a second segmented group inside this
// same dropdown. Lazy fetch on open so the modal mount stays cheap.
//
// Picking a preset calls `onPick(template_id, options)`; the parent
// (ShareModal) overwrites its local recipe state. The "Manage
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
import { listPresets, type PresetRecord, ShareApiError } from './presetsApi';
import type { SharePanelId, ShareOptions } from './types';

interface Props {
  panel: SharePanelId;
  onPick: (template_id: string, options: ShareOptions) => void;
  onManage: () => void;
}

export function PresetDropdown({ panel, onPick, onManage }: Props) {
  const [open, setOpen] = useState(false);
  const [presets, setPresets] = useState<Record<string, PresetRecord>>({});
  const [error, setError] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

  // Lazy fetch on open. Re-fetch on panel change so switching panels
  // (rare — the modal is per-panel) doesn't show stale presets.
  useEffect(() => {
    if (!open) return;
    const ac = new AbortController();
    setError(null);
    listPresets({ signal: ac.signal })
      .then((r) => setPresets(r.presets[panel] ?? {}))
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
