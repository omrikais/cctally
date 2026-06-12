import { useState } from 'react';
import type { ConversationBlock } from '../types/conversation';
import { SearchIcon } from './ConvIcons';
import { CopyButton } from './CopyButton';
import { domainOf, isHttpUrl } from './webUrl';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;
const INITIAL_LINKS = 10;

// #177 S4 (Q6-A): semantic card for WebSearch — quoted query + count chip,
// clickable {title, url} link list (first 10, then an expander) from the
// kernel's web_search fold; falls back to today's plain text panel on old
// rows (no capture) or zero links. http(s)-only anchors (Codex F6); domains
// render as dim text — NO favicon fetches (no external requests).

function queryOf(call: Call): string {
  const q = (call.input as { query?: unknown } | null | undefined)?.query;
  return typeof q === 'string' ? q : '';
}

export function WebSearchCard({ call }: { call: Call }) {
  const query = queryOf(call);
  const links = call.web_search?.links;
  const [showAll, setShowAll] = useState(false);
  const hasLinks = links != null && links.length > 0;
  const shown = hasLinks && !showAll ? links.slice(0, INITIAL_LINKS) : links ?? [];

  return (
    <details className="conv-chip conv-web" open>
      <summary>
        <span className="conv-chev" aria-hidden="true" />
        <SearchIcon />
        <span className="conv-chip-name">WebSearch</span>
        <span className="conv-web-domain">“{query}”</span>
        {links != null && (
          <span className="conv-web-status conv-web-status--ok">
            {links.length}
            {call.web_search?.links_truncated ? '+' : ''} results
          </span>
        )}
        {call.result?.is_error && <span className="conv-chip-status"> · error</span>}
      </summary>
      <div className="conv-web-body">
        {hasLinks ? (
          <>
            {shown.map((l, i) => (
              <div className="conv-web-link" key={i}>
                {isHttpUrl(l.url) ? (
                  <a href={l.url} target="_blank" rel="noopener noreferrer">{l.title}</a>
                ) : (
                  <span>{l.title}</span>
                )}
                <span className="conv-web-link-domain">{domainOf(l.url) || l.url}</span>
              </div>
            ))}
            {!showAll && links.length > INITIAL_LINKS && (
              <div className="conv-web-more">
                <button type="button" onClick={() => setShowAll(true)}>
                  + {links.length - INITIAL_LINKS} more results
                </button>
              </div>
            )}
          </>
        ) : call.result?.text ? (
          <div className="conv-tool-io">
            <CopyButton text={call.result.text} />
            <pre className="conv-code conv-code--result">{call.result.text}</pre>
          </div>
        ) : (
          <div className="conv-tool-io-label conv-tool-io-label--none">no result</div>
        )}
      </div>
    </details>
  );
}
