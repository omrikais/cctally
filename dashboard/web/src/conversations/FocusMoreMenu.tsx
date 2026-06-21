import { useCallback, useRef, useState } from 'react';
import type { FocusMode } from './applyFocusMode';

// #217 S5 E4 — the focus "▾ More" menu beside the All/Chat/Prompts/Errors
// segmented control. Adds the Edits / Bash / per-Subagent focus modes as
// additional single-select values on the SAME focus axis (picking one clears the
// four primary modes). The Subagent submenu lists only the TOP-LEVEL subagent
// keys present in the loaded items/groups (Codex P1-3/P1-4); labels come from
// `detail.subagent_meta` with a key fallback when meta is empty. Hidden when
// there are no subagents.
//
// Popover invariants (dashboard-gotchas, mirroring ExportMenu): container-level
// Escape (onKeyDown on the role="menu" div, NOT per-item), focus captured at
// open + restored to the trigger on close, ≥44px touch targets (CSS),
// reduced-motion via CSS only. Presentational — `onSelect` is the dispatch
// boundary so the reader owns the store write (and the unit tests stay pure).

export interface FocusSubagentOption {
  key: string;
  label: string; // resolved label (subagent_meta kind/description) — may be ''
}

// Label shown for the trigger when a More-mode is active, '' otherwise (the
// four primary modes show their own segmented button as selected).
function activeLabel(mode: FocusMode, subagents: FocusSubagentOption[]): string {
  if (mode === 'edits') return 'Edits';
  if (mode === 'bash') return 'Bash';
  if (mode.startsWith('subagent:')) {
    const key = mode.slice('subagent:'.length);
    const opt = subagents.find((s) => s.key === key);
    return opt ? subagentLabel(opt) : key.slice(0, 8);
  }
  return '';
}

// A subagent's display label: its resolved meta label, or a truncated key when
// the meta is empty (buckets exist even without subagent_meta — Codex P1-4).
function subagentLabel(opt: FocusSubagentOption): string {
  return opt.label.trim() || opt.key.slice(0, 8);
}

export function FocusMoreMenu({
  focusMode,
  subagents,
  onSelect,
}: {
  focusMode: FocusMode;
  subagents: FocusSubagentOption[];
  onSelect: (mode: FocusMode) => void;
}) {
  const [open, setOpen] = useState(false);
  const [subOpen, setSubOpen] = useState(false);
  const restoreRef = useRef<Element | null>(null);

  const close = useCallback(() => {
    setOpen(false);
    setSubOpen(false);
    const el = restoreRef.current;
    if (el instanceof HTMLElement) el.focus();
  }, []);

  const toggle = useCallback(() => {
    setOpen((prev) => {
      const next = !prev;
      if (next) restoreRef.current = document.activeElement;
      else setSubOpen(false);
      return next;
    });
  }, []);

  // Close the menu after a selection lands (the reader will reset the segmented
  // control's selected state via the new focus mode).
  const pick = useCallback(
    (mode: FocusMode) => {
      onSelect(mode);
      close();
    },
    [onSelect, close],
  );

  const label = activeLabel(focusMode, subagents);
  const isActive = label !== '';

  return (
    <div
      className="conv-focus-more"
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
          setOpen(false);
          setSubOpen(false);
        }
      }}
    >
      <button
        type="button"
        className={['conv-focus-more-toggle', isActive ? 'conv-focus-more-toggle--on' : ''].filter(Boolean).join(' ')}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="More focus filters"
        onClick={toggle}
      >
        {isActive ? `${label} ▾` : 'More ▾'}
      </button>
      {open && (
        <div
          className="conv-focus-more-menu"
          role="menu"
          aria-label="More focus filters"
          tabIndex={-1}
          onKeyDown={(e) => {
            if (e.key === 'Escape') {
              e.stopPropagation();
              close();
            }
          }}
        >
          <button
            type="button"
            role="menuitem"
            className={['conv-focus-more-item', focusMode === 'edits' ? 'conv-focus-more-item--on' : ''].filter(Boolean).join(' ')}
            onClick={() => pick('edits')}
          >
            Edits
          </button>
          <button
            type="button"
            role="menuitem"
            className={['conv-focus-more-item', focusMode === 'bash' ? 'conv-focus-more-item--on' : ''].filter(Boolean).join(' ')}
            onClick={() => pick('bash')}
          >
            Bash
          </button>
          {subagents.length > 0 && (
            <>
              <button
                type="button"
                role="menuitem"
                className="conv-focus-more-item conv-focus-more-sub"
                aria-haspopup="menu"
                aria-expanded={subOpen}
                onClick={() => setSubOpen((v) => !v)}
              >
                Subagent {subOpen ? '▾' : '▸'}
              </button>
              {subOpen && (
                <div className="conv-focus-more-submenu" role="menu" aria-label="Focus a subagent">
                  {subagents.map((opt) => {
                    const mode = `subagent:${opt.key}` as FocusMode;
                    return (
                      <button
                        key={opt.key}
                        type="button"
                        role="menuitem"
                        className={['conv-focus-more-item', focusMode === mode ? 'conv-focus-more-item--on' : ''].filter(Boolean).join(' ')}
                        onClick={() => pick(mode)}
                      >
                        {subagentLabel(opt)}
                      </button>
                    );
                  })}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
