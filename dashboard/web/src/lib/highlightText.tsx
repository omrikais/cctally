import { Fragment, type ReactNode } from 'react';

// Render-time substring highlighter (S7 SESS-2). Sessions search matches a
// whole-row haystack (row indices), so there are no per-cell match spans;
// this marks literal (non-regex), case-insensitive occurrences of `query`
// in the visible cell text at render time, preserving the original casing
// of each matched slice. Empty/whitespace query or no match → plain text.

export function HighlightText({ text, query }: { text: string; query: string }): ReactNode {
  const q = query.trim().toLowerCase();
  if (!q) return <>{text}</>;
  const hay = text.toLowerCase();
  const parts: ReactNode[] = [];
  let i = 0;
  let key = 0;
  for (;;) {
    const hit = hay.indexOf(q, i);
    if (hit === -1) {
      if (i < text.length) parts.push(<Fragment key={key++}>{text.slice(i)}</Fragment>);
      break;
    }
    if (hit > i) parts.push(<Fragment key={key++}>{text.slice(i, hit)}</Fragment>);
    parts.push(<mark key={key++}>{text.slice(hit, hit + q.length)}</mark>);
    i = hit + q.length;
  }
  return <>{parts}</>;
}
