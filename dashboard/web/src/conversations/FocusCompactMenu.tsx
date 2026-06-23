import { useCallback, useEffect, useRef, useState } from 'react';
import type { FocusMode } from './applyFocusMode';
import { subagentLabel, type FocusSubagentOption } from './FocusMoreMenu';
import { nextRovingIndex } from './menuKeyboard';

// #228 S3 C2 — the mobile (≤640px) compact focus picker: a single
// "Focus: <active> ▾" dropdown that REPLACES the desktop 4-button segment AND
// absorbs the FocusMoreMenu sub-options (Edits / Bash / per-Subagent) into one
// flat list, so Row 2 of the two-row mobile header stays slim. Desktop keeps the
// segment + the separate "▾ More" menu untouched.
//
// Same menu primitive as ExportMenu / FocusMoreMenu (dashboard-gotchas):
// container-level Escape (onKeyDown on the role="menu" div), focus captured at
// open + restored to the trigger on close, ≥44px touch targets (CSS),
// reduced-motion via CSS only. Flat role="menuitem" list with a single roving
// tabindex; ArrowUp/Down wrap, Home/End jump, Escape closes. Index math is the
// pure `nextRovingIndex` helper. Presentational — `onSelect` is the dispatch
// boundary so the reader owns the store write and the unit test stays pure.

const PRIMARY: { mode: FocusMode; label: string }[] = [
  { mode: 'all', label: 'All' },
  { mode: 'chat', label: 'Chat' },
  { mode: 'prompts', label: 'Prompts' },
  { mode: 'errors', label: 'Errors' },
];

const EXTRA: { mode: FocusMode; label: string }[] = [
  { mode: 'edits', label: 'Edits' },
  { mode: 'bash', label: 'Bash' },
];

// `subagentLabel` is shared with FocusMoreMenu (#230 P3 — one source of truth).

// The active mode's display label for the trigger. Mirrors the desktop segment +
// FocusMoreMenu's `activeLabel`, but covers the four primary modes too (the
// compact picker subsumes the whole axis).
function focusLabel(mode: FocusMode, subagents: FocusSubagentOption[]): string {
  const primary = PRIMARY.find((p) => p.mode === mode);
  if (primary) return primary.label;
  if (mode === 'edits') return 'Edits';
  if (mode === 'bash') return 'Bash';
  if (mode.startsWith('subagent:')) {
    const key = mode.slice('subagent:'.length);
    const opt = subagents.find((s) => s.key === key);
    return opt ? subagentLabel(opt) : key.slice(0, 8);
  }
  return 'All';
}

export function FocusCompactMenu({
  focusMode,
  subagents,
  onSelect,
  errorCount = 0,
}: {
  focusMode: FocusMode;
  subagents: FocusSubagentOption[];
  onSelect: (mode: FocusMode) => void;
  errorCount?: number;
}) {
  const [open, setOpen] = useState(false);
  const restoreRef = useRef<Element | null>(null);

  // The flat option list: the four primary modes, then Edits/Bash, then one
  // entry per top-level subagent. Built each render (cheap; the list is short).
  const options: { mode: FocusMode; label: string; badge?: number }[] = [
    ...PRIMARY.map((p) => (p.mode === 'errors' && errorCount > 0 ? { ...p, badge: errorCount } : p)),
    ...EXTRA,
    ...subagents.map((s) => ({ mode: `subagent:${s.key}` as FocusMode, label: subagentLabel(s) })),
  ];
  const itemCount = options.length;
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const [activeIndex, setActiveIndex] = useState(0);
  const activeIndexRef = useRef(0);
  const setActive = useCallback((i: number) => {
    activeIndexRef.current = i;
    setActiveIndex(i);
  }, []);

  const close = useCallback(() => {
    setOpen(false);
    const el = restoreRef.current;
    if (el instanceof HTMLElement) el.focus();
  }, []);

  const openAt = useCallback(
    (index: number) => {
      restoreRef.current = document.activeElement;
      setActive(index);
      setOpen(true);
    },
    [setActive],
  );

  const toggle = useCallback(() => {
    if (open) setOpen(false);
    else {
      // Open with the CURRENT mode pre-focused so the picker lands on what's
      // active (falls back to 0 when the active mode isn't in the list).
      const cur = options.findIndex((o) => o.mode === focusMode);
      openAt(cur >= 0 ? cur : 0);
    }
  }, [open, openAt, options, focusMode]);

  useEffect(() => {
    if (open) itemRefs.current[activeIndexRef.current]?.focus();
  }, [open]);

  const onMenuKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        close();
        return;
      }
      const ni = nextRovingIndex(e.key, activeIndexRef.current, itemCount);
      if (ni !== null) {
        e.preventDefault();
        setActive(ni);
        itemRefs.current[ni]?.focus();
      }
    },
    [close, itemCount, setActive],
  );

  const onTriggerKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        openAt(0);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        openAt(itemCount - 1);
      }
    },
    [openAt, itemCount],
  );

  const pick = useCallback(
    (mode: FocusMode) => {
      onSelect(mode);
      close();
    },
    [onSelect, close],
  );

  const label = focusLabel(focusMode, subagents);

  return (
    <div
      className="conv-focus-compact"
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setOpen(false);
      }}
    >
      <button
        type="button"
        className="conv-focus-compact-toggle"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Focus: ${label}`}
        onClick={toggle}
        onKeyDown={onTriggerKeyDown}
      >
        Focus: {label} ▾
      </button>
      {open && (
        <div
          className="conv-focus-compact-menu"
          role="menu"
          aria-label="Focus mode"
          tabIndex={-1}
          onKeyDown={onMenuKeyDown}
        >
          {options.map((o, i) => (
            <button
              key={o.mode}
              type="button"
              role="menuitemradio"
              aria-checked={focusMode === o.mode}
              tabIndex={i === activeIndex ? 0 : -1}
              ref={(el) => {
                itemRefs.current[i] = el;
              }}
              className={['conv-focus-compact-item', focusMode === o.mode ? 'conv-focus-compact-item--on' : ''].filter(Boolean).join(' ')}
              onClick={() => pick(o.mode)}
            >
              {o.label}
              {o.badge != null && <span className="conv-focus-compact-badge">{o.badge}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
