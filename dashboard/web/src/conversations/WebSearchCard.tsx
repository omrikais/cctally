import { useState } from 'react';
import type { ConversationBlock } from '../types/conversation';
import { SearchIcon } from './ConvIcons';
import { CopyButton } from './CopyButton';
import { OutcomeBadge, OutcomeEvidence, outcomeFromStatus } from './OutcomeBadge';
import { splitToExactNodes, splitToReactNodes, useExactSurfaceTargets, useFindSplit } from './findMark';
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
  const split = useFindSplit();
  const exactCall = useExactSurfaceTargets(call.block_key, 'call');
  const exactCompletion = useExactSurfaceTargets(call.block_key, 'completion');
  const exactOutput = useExactSurfaceTargets(call.block_key, 'output');
  const query = queryOf(call);
  const links = call.web_search?.links;
  const [showAll, setShowAll] = useState(false);
  const hasLinks = links != null && links.length > 0;
  const shown = hasLinks && !showAll ? links.slice(0, INITIAL_LINKS) : links ?? [];
  const native = call.native_card?.type === 'web_search' ? call.native_card : null;
  const outcome = native
    ? call.outcome ?? outcomeFromStatus(native.completion.status, call.result?.is_error === true)
    : null;

  return (
    <details className="conv-chip conv-web" open
             {...(call.block_key ? { 'data-disclosure-key': call.block_key } : {})}>
      <summary>
        <span className="conv-chev" aria-hidden="true" />
        <SearchIcon />
        <span className="conv-chip-name">WebSearch</span>
        <span className="conv-web-domain">“{
          exactCall.get('query')?.length
            ? splitToExactNodes(query, exactCall.get('query')!)
            : query
        }”</span>
        {links != null && (
          // #217 S3 E10#9 — a green "ok" count chip on an errored search reads as
          // success. Drop --ok and take the neutral/error style when the result
          // carries is_error; the `· error` span below still renders.
          <span className={`conv-web-status ${call.result?.is_error ? 'conv-web-status--err' : 'conv-web-status--ok'}`}>
            {links.length}
            {call.web_search?.links_truncated ? '+' : ''} results
          </span>
        )}
        {outcome
          ? <OutcomeBadge outcome={outcome} isError={call.result?.is_error === true} />
          : call.result?.is_error && <span className="conv-chip-status"> · error</span>}
      </summary>
      <div className="conv-web-body">
        {outcome && <OutcomeEvidence outcome={outcome} />}
        {hasLinks ? (
          <>
            {shown.map((l, i) => (
              <div className="conv-web-link" key={i}>
                {isHttpUrl(l.url) ? (
                  <a href={l.url} target="_blank" rel="noopener noreferrer">{
                    exactCompletion.get(`results.${i}.title`)?.length
                      ? splitToExactNodes(l.title, exactCompletion.get(`results.${i}.title`)!)
                      : l.title
                  }</a>
                ) : (
                  <span>{exactCompletion.get(`results.${i}.title`)?.length
                    ? splitToExactNodes(l.title, exactCompletion.get(`results.${i}.title`)!)
                    : l.title}</span>
                )}
                <span className="conv-web-link-domain">{
                  exactCompletion.get(`results.${i}.domain`)?.length
                    ? splitToExactNodes(domainOf(l.url) || l.domain || l.url, exactCompletion.get(`results.${i}.domain`)!)
                    : domainOf(l.url) || l.domain || l.url
                }</span>
                {l.snippet && <span className="conv-web-link-snippet">{
                  exactCompletion.get(`results.${i}.snippet`)?.length
                    ? splitToExactNodes(l.snippet, exactCompletion.get(`results.${i}.snippet`)!)
                    : l.snippet
                }</span>}
                {l.ref_id && <span className="conv-web-link-ref">{
                  exactCompletion.get(`results.${i}.ref_id`)?.length
                    ? splitToExactNodes(l.ref_id, exactCompletion.get(`results.${i}.ref_id`)!)
                    : l.ref_id
                }</span>}
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
            <pre className="conv-code conv-code--result">
              {exactOutput.get('t0')?.length
                ? splitToExactNodes(call.result.text, exactOutput.get('t0')!)
                : split ? splitToReactNodes(call.result.text, split) : call.result.text}
            </pre>
          </div>
        ) : (
          <div className="conv-tool-io-label conv-tool-io-label--none">no result</div>
        )}
      </div>
    </details>
  );
}
