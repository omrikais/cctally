import { Fragment, type ReactNode } from 'react';

// Convert a search snippet into React nodes. FTS snippets delimit hits
// with literal '[' / ']' (the SQLite snippet() markers we configured);
// LIKE snippets carry no markers. We split on balanced [..] pairs and
// wrap the inside in <mark>. Everything is rendered as React TEXT nodes
// (never innerHTML), so raw HTML in the prose is inert. An unbalanced
// '[' with no closing ']' renders verbatim (defensive — a literal
// bracket in user content must not eat the rest of the snippet).
export function renderSnippet(snippet: string): ReactNode {
  const out: ReactNode[] = [];
  let i = 0;
  let key = 0;
  while (i < snippet.length) {
    const open = snippet.indexOf('[', i);
    if (open === -1) {
      out.push(<Fragment key={key++}>{snippet.slice(i)}</Fragment>);
      break;
    }
    const close = snippet.indexOf(']', open + 1);
    if (close === -1) {
      out.push(<Fragment key={key++}>{snippet.slice(i)}</Fragment>);
      break;
    }
    if (open > i) out.push(<Fragment key={key++}>{snippet.slice(i, open)}</Fragment>);
    out.push(<mark key={key++}>{snippet.slice(open + 1, close)}</mark>);
    i = close + 1;
  }
  return <>{out}</>;
}
