import { useCallback, useRef, useState } from 'react';
import { useCopy } from './useCopy';

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

function exportUrl(sessionId: string, scope: Scope): string {
  return `/api/conversation/${encodeURIComponent(sessionId)}/export?scope=${scope}`;
}

async function fetchExport(sessionId: string, scope: Scope): Promise<string> {
  const res = await fetch(exportUrl(sessionId, scope));
  if (!res.ok) throw new Error(`export failed: ${res.status}`);
  return res.text();
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

export function ExportMenu({ sessionId, title }: { sessionId: string; title?: string }) {
  const [open, setOpen] = useState(false);
  // The action currently fetching, encoded `${scope}:${kind}`, so its row shows
  // a disabled/loading state without freezing the others.
  const [busy, setBusy] = useState<string | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const restoreRef = useRef<Element | null>(null);
  const { copy } = useCopy();

  const close = useCallback(() => {
    setOpen(false);
    // Restore focus to whatever was focused at open (the trigger, normally).
    const el = restoreRef.current;
    if (el instanceof HTMLElement) el.focus();
  }, []);

  const toggle = useCallback(() => {
    setOpen((prev) => {
      const next = !prev;
      if (next) restoreRef.current = document.activeElement;
      return next;
    });
  }, []);

  const doCopy = useCallback(
    async (scope: Scope) => {
      const key = `${scope}:copy`;
      setBusy(key);
      try {
        const text = await fetchExport(sessionId, scope);
        copy(text);
      } catch {
        /* swallow — a failed export leaves the clipboard untouched */
      } finally {
        setBusy((b) => (b === key ? null : b));
      }
    },
    [sessionId, copy],
  );

  const doDownload = useCallback(
    async (scope: Scope) => {
      const key = `${scope}:download`;
      setBusy(key);
      try {
        const text = await fetchExport(sessionId, scope);
        triggerDownload(`${slugifyTitle(title, sessionId)}-${scope}.md`, text);
      } catch {
        /* swallow */
      } finally {
        setBusy((b) => (b === key ? null : b));
      }
    },
    [sessionId, title],
  );

  return (
    <div
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
      >
        Export ▾
      </button>
      {open && (
        <div
          className="conv-export-menu"
          role="menu"
          aria-label="Export transcript"
          tabIndex={-1}
          onKeyDown={(e) => {
            if (e.key === 'Escape') {
              e.stopPropagation();
              close();
            }
          }}
        >
          {SCOPES.map(({ scope, label }) => (
            <div key={scope} className="conv-export-row" role="none">
              <span className="conv-export-row-label">{label}</span>
              <button
                type="button"
                className="conv-export-action"
                aria-label={`${label} — Copy`}
                disabled={busy === `${scope}:copy`}
                onClick={() => void doCopy(scope)}
              >
                {busy === `${scope}:copy` ? '…' : 'Copy'}
              </button>
              <button
                type="button"
                className="conv-export-action"
                aria-label={`${label} — Download`}
                disabled={busy === `${scope}:download`}
                onClick={() => void doDownload(scope)}
              >
                {busy === `${scope}:download` ? '…' : 'Download'}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
