import { useCallback, useEffect, useRef, useState } from 'react';
import type { FocusMode } from './applyFocusMode';
import { nextRovingIndex } from './menuKeyboard';
import { useOutsideDismiss } from './useOutsideDismiss';

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
//
// APG menu keyboard pattern (#224): role="menuitem" items with a single roving
// tabindex; opening moves focus to the first item, Arrow Up/Down cycle (wrapping)
// and Home/End jump within the active level, Escape closes. The Subagent item is
// a submenu parent — Arrow Right (or Enter/click) opens it and moves focus to the
// first subitem; Arrow Left collapses it and returns focus to the parent. Index
// math is the pure `nextRovingIndex` helper; `inSub` tracks which level has focus.

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
// Exported so FocusCompactMenu (#228 S3 C2) reuses the SAME rule rather than
// duplicating it (#230 P3 — one source of truth for the label derivation).
export function subagentLabel(opt: FocusSubagentOption): string {
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
  // #238 R3 — pointerdown-outside dismiss. Silent (mirrors the existing onBlur:
  // setOpen(false)+setSubOpen(false)), NOT the focus-restoring close() — a
  // pointerdown fires before the clicked-outside control is focused, so close()'s
  // trigger.focus() would yank focus back. The submenu lives inside the same
  // container, so an outside pointerdown closing both is correct.
  const rootRef = useRef<HTMLDivElement>(null);
  useOutsideDismiss(rootRef, open, useCallback(() => { setOpen(false); setSubOpen(false); }, []));

  // Roving-focus state. The main level lists Edits, Bash, and (when present) the
  // Subagent parent; `inSub` flips focus into the subagent submenu. Mirroring
  // refs let the focus-on-open effects read the latest values without depending
  // on them.
  const hasSub = subagents.length > 0;
  const mainCount = 2 + (hasSub ? 1 : 0);
  const subagentParentIndex = 2; // valid only when hasSub
  const mainRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const subRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const [mainIndex, setMainIndexState] = useState(0);
  const mainIndexRef = useRef(0);
  const setMainIndex = useCallback((i: number) => {
    mainIndexRef.current = i;
    setMainIndexState(i);
  }, []);
  const [subIndex, setSubIndexState] = useState(0);
  const subIndexRef = useRef(0);
  const setSubIndex = useCallback((i: number) => {
    subIndexRef.current = i;
    setSubIndexState(i);
  }, []);
  const [inSub, setInSubState] = useState(false);
  const inSubRef = useRef(false);
  const setInSub = useCallback((v: boolean) => {
    inSubRef.current = v;
    setInSubState(v);
  }, []);

  const close = useCallback(() => {
    setOpen(false);
    setSubOpen(false);
    setInSub(false);
    const el = restoreRef.current;
    if (el instanceof HTMLElement) el.focus();
  }, [setInSub]);

  // Open with a chosen initial main item (0 for click / ArrowDown, last for
  // ArrowUp). The focus-on-open effect moves focus there once the menu mounts.
  const openAt = useCallback(
    (index: number) => {
      restoreRef.current = document.activeElement;
      setMainIndex(index);
      setInSub(false);
      setSubOpen(false);
      setOpen(true);
    },
    [setMainIndex, setInSub],
  );

  const toggle = useCallback(() => {
    if (open) {
      setOpen(false);
      setSubOpen(false);
      setInSub(false);
    } else {
      openAt(0);
    }
  }, [open, openAt, setInSub]);

  // Open the Subagent submenu and move focus to its first item; close it (back to
  // the parent) keeps the main menu open. Shared by Arrow Right/Left and click.
  const openSub = useCallback(() => {
    setSubIndex(0);
    setInSub(true);
    setSubOpen(true);
  }, [setSubIndex, setInSub]);
  const closeSubToParent = useCallback(() => {
    setSubOpen(false);
    setInSub(false);
    mainRefs.current[subagentParentIndex]?.focus();
  }, [setInSub]);

  // Close the menu after a selection lands (the reader will reset the segmented
  // control's selected state via the new focus mode).
  const pick = useCallback(
    (mode: FocusMode) => {
      onSelect(mode);
      close();
    },
    [onSelect, close],
  );

  // On open, move focus to the active main item. On submenu open, move focus to
  // its first item. Both read the index via ref so deps stay minimal.
  useEffect(() => {
    if (open) mainRefs.current[mainIndexRef.current]?.focus();
  }, [open]);
  useEffect(() => {
    if (subOpen && inSubRef.current) subRefs.current[subIndexRef.current]?.focus();
  }, [subOpen]);

  const onMenuKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        close();
        return;
      }
      if (!inSubRef.current) {
        // Main level. Arrow Right opens the submenu when on the Subagent parent.
        if (e.key === 'ArrowRight' && hasSub && mainIndexRef.current === subagentParentIndex) {
          e.preventDefault();
          openSub();
          return;
        }
        const ni = nextRovingIndex(e.key, mainIndexRef.current, mainCount);
        if (ni !== null) {
          e.preventDefault();
          setMainIndex(ni);
          mainRefs.current[ni]?.focus();
        }
        return;
      }
      // Submenu level. Arrow Left collapses back to the parent.
      if (e.key === 'ArrowLeft') {
        e.preventDefault();
        closeSubToParent();
        return;
      }
      const ni = nextRovingIndex(e.key, subIndexRef.current, subagents.length);
      if (ni !== null) {
        e.preventDefault();
        setSubIndex(ni);
        subRefs.current[ni]?.focus();
      }
    },
    [close, hasSub, mainCount, openSub, closeSubToParent, setMainIndex, setSubIndex, subagents.length],
  );

  const onTriggerKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        openAt(0);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        openAt(mainCount - 1);
      }
    },
    [openAt, mainCount],
  );

  const label = activeLabel(focusMode, subagents);
  const isActive = label !== '';

  return (
    <div
      ref={rootRef}
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
        onKeyDown={onTriggerKeyDown}
      >
        {isActive ? `${label} ▾` : 'More ▾'}
      </button>
      {open && (
        <div
          className="conv-focus-more-menu"
          role="menu"
          aria-label="More focus filters"
          tabIndex={-1}
          onKeyDown={onMenuKeyDown}
        >
          <button
            type="button"
            role="menuitem"
            tabIndex={!inSub && mainIndex === 0 ? 0 : -1}
            ref={(el) => {
              mainRefs.current[0] = el;
            }}
            className={['conv-focus-more-item', focusMode === 'edits' ? 'conv-focus-more-item--on' : ''].filter(Boolean).join(' ')}
            onClick={() => pick('edits')}
          >
            Edits
          </button>
          <button
            type="button"
            role="menuitem"
            tabIndex={!inSub && mainIndex === 1 ? 0 : -1}
            ref={(el) => {
              mainRefs.current[1] = el;
            }}
            className={['conv-focus-more-item', focusMode === 'bash' ? 'conv-focus-more-item--on' : ''].filter(Boolean).join(' ')}
            onClick={() => pick('bash')}
          >
            Bash
          </button>
          {hasSub && (
            <>
              <button
                type="button"
                role="menuitem"
                tabIndex={!inSub && mainIndex === subagentParentIndex ? 0 : -1}
                ref={(el) => {
                  mainRefs.current[subagentParentIndex] = el;
                }}
                className="conv-focus-more-item conv-focus-more-sub"
                aria-haspopup="menu"
                aria-expanded={subOpen}
                onClick={() => (subOpen ? closeSubToParent() : openSub())}
              >
                Subagent {subOpen ? '▾' : '▸'}
              </button>
              {subOpen && (
                <div className="conv-focus-more-submenu" role="menu" aria-label="Focus a subagent">
                  {subagents.map((opt, i) => {
                    const mode = `subagent:${opt.key}` as FocusMode;
                    return (
                      <button
                        key={opt.key}
                        type="button"
                        role="menuitem"
                        tabIndex={inSub && subIndex === i ? 0 : -1}
                        ref={(el) => {
                          subRefs.current[i] = el;
                        }}
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
