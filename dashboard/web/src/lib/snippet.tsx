import { Fragment, type ReactNode } from 'react';
import type { SnippetPart } from '../types/conversation';

// #1036 — a search snippet is rendered from the server's structured
// `snippet_parts` (literal transcript text plus the FTS match flag of each
// part), never by parsing brackets: the transcript's own `[` / `]` are text,
// and the legacy bracket `snippet` string is the CLI's form. Without valid
// parts (an old server, a LIKE or Codex excerpt with none, a malformed list)
// the snippet renders as ONE literal text node with no highlight. Everything
// is rendered as React TEXT nodes (never innerHTML), so raw HTML is inert.
export function isSnippetParts(value: unknown): value is SnippetPart[] {
  return Array.isArray(value) && value.every((part) =>
    typeof part === 'object' && part !== null
    && typeof (part as { text?: unknown }).text === 'string'
    && typeof (part as { match?: unknown }).match === 'boolean');
}

export function renderSnippet(snippet: string | null | undefined, parts?: unknown): ReactNode {
  if (isSnippetParts(parts) && parts.length > 0) {
    return (
      <>
        {parts.map((part, i) => (part.match
          ? <mark key={i}>{part.text}</mark>
          : <Fragment key={i}>{part.text}</Fragment>))}
      </>
    );
  }
  return <>{snippet ?? ''}</>;
}
