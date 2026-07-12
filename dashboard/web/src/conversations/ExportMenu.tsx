import { useCallback, useEffect, useRef, useState } from 'react';
import { useCopy } from './useCopy';
import { nextRovingIndex } from './menuKeyboard';
import { useOutsideDismiss } from './useOutsideDismiss';

// #217 S5 §4 (F1/F5) — the reader-header "Export ▾" menu. Lists the four
// Markdown export scopes, each with a Copy (clipboard) and a Download (.md
// file) action; both fetch the new server route once. The endpoint is the SOLE
// whole-session-correct path (the reader is windowed, so a client-side export
// over the loaded window would be silently incomplete — spec §1, Q1).
//
// Popover invariants (dashboard-gotchas): container-level Escape (onKeyDown on
// the role="menu" div, NOT per-button — the S4 Esc-teardown gotcha), focus
// captured at open and restored to the trigger on close, ≥44px touch targets,
// reduced-motion via CSS only. Local component state (no store slot) per the
// plan, with its own outside-click + Escape close.
//
// APG menu keyboard pattern (#224): the eight action buttons are role="menuitem"
// with a single roving tabindex (only the active item is Tab-reachable). Opening
// moves focus into the menu; Arrow Up/Down cycle (wrapping), Home/End jump to the
// ends, Escape closes and restores focus to the trigger. Index math is the pure
// `nextRovingIndex` helper; this component owns the imperative `.focus()`.

type Scope = 'all' | 'prompts' | 'chat' | 'recipe';

const SCOPES: { scope: Scope; label: string }[] = [
  { scope: 'all', label: 'Whole transcript' },
  { scope: 'prompts', label: 'Prompts only' },
  { scope: 'chat', label: 'Chat only' },
  { scope: 'recipe', label: 'Replay recipe' },
];

// Slugify the session title for a download filename (Codex P2-2): strip
// path/control/non-ASCII, collapse to dashes, cap length; fall back to a
// session-id prefix when the slug is empty. Mirrors the share ActionBar slug.
export function slugifyTitle(title: string | undefined, sessionId: string): string {
  const s = (title ?? '')
    .normalize('NFKD')
    .replace(/[^\x20-\x7E]/g, '')
    .replace(/[^a-zA-Z0-9._-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60);
  return s || sessionId.slice(0, 12);
}

// #281 S4 — when anon mode is ON the 8 copy/download items fetch the
// server-scrubbed body (`&anonymize=1`); OFF is byte-identical to today's URL.
function exportUrl(sessionId: string, scope: Scope, anon: boolean): string {
  const base = `/api/conversation/${encodeURIComponent(sessionId)}/export?scope=${scope}`;
  return anon ? `${base}&anonymize=1` : base;
}

async function fetchExport(sessionId: string, scope: Scope, anon: boolean): Promise<string> {
  const res = await fetch(exportUrl(sessionId, scope, anon));
  if (!res.ok) throw new Error(`export failed: ${res.status}`);
  return res.text();
}

// Download filename gains an `-anon` suffix in anon mode; OFF is unchanged.
function exportFilename(title: string | undefined, sessionId: string, scope: Scope, anon: boolean): string {
  const slug = slugifyTitle(title, sessionId);
  return anon ? `${slug}-${scope}-anon.md` : `${slug}-${scope}.md`;
}

function triggerDownload(filename: string, text: string): void {
  const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

export function ExportMenu({
  sessionId,
  title,
  anonMode = false,
}: {
  sessionId: string;
  title?: string;
  // #281 S4 — when true, exports fetch `&anonymize=1` and filenames gain `-anon`.
  // Defaults false so provider-less tests keep asserting today's OFF-path bytes.
  anonMode?: boolean;
}) {
  const [open, setOpen] = useState(false);
  // The action currently fetching, encoded `${scope}:${kind}`, so its row shows
  // a disabled/loading state without freezing the others.
  const [busy, setBusy] = useState<string | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const restoreRef = useRef<Element | null>(null);
  // #238 R3 — pointerdown-outside dismiss. Silent (setOpen(false)), NOT the
  // focus-restoring close() (which would yank focus back to the trigger before
  // the clicked-outside control receives it).
  const rootRef = useRef<HTMLDivElement>(null);
  useOutsideDismiss(rootRef, open, useCallback(() => setOpen(false), []));
  // Roving-focus state for the menuitems (flat index over the scope×{copy,download}
  // grid). A mirroring ref lets the focus-on-open effect read the latest value
  // without re-subscribing to it.
  const itemCount = SCOPES.length * 2;
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const [activeIndex, setActiveIndex] = useState(0);
  const activeIndexRef = useRef(0);
  const setActive = useCallback((i: number) => {
    activeIndexRef.current = i;
    setActiveIndex(i);
  }, []);
  // Belt-and-suspenders (mirrors useCopy): doCopy/doDownload setBusy in a
  // `finally` after an awaited fetch; if the reader unmounts mid-fetch, skip the
  // post-await setState to avoid a setState-on-unmounted-component.
  const mountedRef = useRef(true);
  useEffect(
    () => () => {
      mountedRef.current = false;
    },
    [],
  );
  const { copy } = useCopy();

  // The reader is not keyed by session, so an open menu would otherwise persist
  // across a session switch; close it when the conversation changes.
  useEffect(() => {
    setOpen(false);
  }, [sessionId]);

  const close = useCallback(() => {
    setOpen(false);
    // Restore focus to whatever was focused at open (the trigger, normally).
    const el = restoreRef.current;
    if (el instanceof HTMLElement) el.focus();
  }, []);

  // Open with a chosen initial active item (0 for click / ArrowDown, last for
  // ArrowUp). The focus-on-open effect moves focus there once the menu mounts.
  const openAt = useCallback(
    (index: number) => {
      restoreRef.current = document.activeElement;
      setActive(index);
      setOpen(true);
    },
    [setActive],
  );

  const toggle = useCallback(() => {
    if (open) {
      setOpen(false);
    } else {
      openAt(0);
    }
  }, [open, openAt]);

  // On open, move focus into the menu (the active menuitem). Reads the index via
  // ref so the effect depends only on `open`.
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

  const doCopy = useCallback(
    async (scope: Scope) => {
      const key = `${scope}:copy`;
      setBusy(key);
      try {
        const text = await fetchExport(sessionId, scope, anonMode);
        copy(text);
      } catch {
        /* swallow — a failed export leaves the clipboard untouched */
      } finally {
        if (mountedRef.current) setBusy((b) => (b === key ? null : b));
      }
    },
    [sessionId, copy, anonMode],
  );

  const doDownload = useCallback(
    async (scope: Scope) => {
      const key = `${scope}:download`;
      setBusy(key);
      try {
        const text = await fetchExport(sessionId, scope, anonMode);
        triggerDownload(exportFilename(title, sessionId, scope, anonMode), text);
      } catch {
        /* swallow */
      } finally {
        if (mountedRef.current) setBusy((b) => (b === key ? null : b));
      }
    },
    [sessionId, title, anonMode],
  );

  return (
    <div
      ref={rootRef}
      className="conv-export"
      onBlur={(e) => {
        // Outside-click / focus-out close: if focus leaves the container, close.
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setOpen(false);
      }}
    >
      <button
        ref={triggerRef}
        type="button"
        className="conv-export-toggle"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Export transcript"
        onClick={toggle}
        onKeyDown={onTriggerKeyDown}
      >
        Export ▾
      </button>
      {open && (
        <div
          className="conv-export-menu"
          role="menu"
          aria-label="Export transcript"
          tabIndex={-1}
          onKeyDown={onMenuKeyDown}
        >
          {anonMode && (
            <div className="conv-export-anon-note" role="none">
              Anonymized — project paths, home, username &amp; known secrets redacted
            </div>
          )}
          {SCOPES.map(({ scope, label }, rowIdx) => {
            const copyIdx = rowIdx * 2;
            const downloadIdx = copyIdx + 1;
            return (
              <div key={scope} className="conv-export-row" role="none">
                <span className="conv-export-row-label">{label}</span>
                <button
                  type="button"
                  role="menuitem"
                  tabIndex={copyIdx === activeIndex ? 0 : -1}
                  ref={(el) => {
                    itemRefs.current[copyIdx] = el;
                  }}
                  className="conv-export-action"
                  aria-label={`${label} — Copy`}
                  disabled={busy === `${scope}:copy`}
                  onClick={() => void doCopy(scope)}
                >
                  {busy === `${scope}:copy` ? '…' : 'Copy'}
                </button>
                <button
                  type="button"
                  role="menuitem"
                  tabIndex={downloadIdx === activeIndex ? 0 : -1}
                  ref={(el) => {
                    itemRefs.current[downloadIdx] = el;
                  }}
                  className="conv-export-action"
                  aria-label={`${label} — Download`}
                  disabled={busy === `${scope}:download`}
                  onClick={() => void doDownload(scope)}
                >
                  {busy === `${scope}:download` ? '…' : 'Download'}
                </button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
