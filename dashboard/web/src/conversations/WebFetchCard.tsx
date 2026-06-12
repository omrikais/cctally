import { useState } from 'react';
import type { ConversationBlock, FullPayload } from '../types/conversation';
import { Markdown } from '../components/Markdown';
import { GlobeIcon } from './ConvIcons';
import { CopyButton } from './CopyButton';
import { LoadFull } from './LoadFull';
import { MediaFigure } from './MediaFigure';
import { domainOf, isHttpUrl } from './webUrl';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

// #177 S4 (Q6-A): semantic source card for WebFetch — domain + HTTP status
// chip header (status from the kernel's web_fetch fold; absent on old rows →
// no chip), labeled url/prompt fields, the Markdown summary clamped with a
// show-full toggle, LoadFull on a truncated result. Bespoke anchors bypass
// the <Markdown> link pipeline → http(s)-only (Codex F6).

function urlOf(call: Call): string {
  const u = (call.input as { url?: unknown } | null | undefined)?.url;
  return typeof u === 'string' ? u : '';
}
function promptOf(call: Call): string {
  const p = (call.input as { prompt?: unknown } | null | undefined)?.prompt;
  return typeof p === 'string' ? p : '';
}
// Same content-length proxy as ExitPlanModeCard's planIsLong.
function resultIsLong(text: string): boolean {
  return text.split('\n').length > 24 || text.length > 1400;
}

export function WebFetchCard({ call }: { call: Call }) {
  const url = urlOf(call);
  const prompt = promptOf(call);
  const domain = domainOf(url);
  const [expanded, setExpanded] = useState(false);
  const [full, setFull] = useState<string | null>(null);
  const text = full ?? call.result?.text ?? '';
  const clamped = resultIsLong(text) && !expanded;
  const status = call.web_fetch;
  const ok = status != null && status.code >= 200 && status.code < 400;

  return (
    <details className="conv-chip conv-web" open>
      <summary>
        <span className="conv-chev" aria-hidden="true" />
        <GlobeIcon />
        <span className="conv-chip-name">WebFetch</span>
        {domain && <span className="conv-web-domain">{domain}</span>}
        {status != null && (
          <span className={`conv-web-status ${ok ? 'conv-web-status--ok' : 'conv-web-status--err'}`}>
            {status.code}
            {status.code_text ? ` ${status.code_text}` : ''}
          </span>
        )}
        {call.result?.is_error && <span className="conv-chip-status"> · error</span>}
      </summary>
      <div className="conv-web-body">
        <div className="conv-web-field">
          <span className="conv-web-key">url</span>
          {isHttpUrl(url) ? (
            <a href={url} target="_blank" rel="noopener noreferrer">{url}</a>
          ) : (
            <span>{url}</span>
          )}
        </div>
        {prompt && (
          <div className="conv-web-field">
            <span className="conv-web-key">prompt</span>
            <span>{prompt}</span>
          </div>
        )}
        {text ? (
          <>
            <div className="conv-web-copy"><CopyButton text={text} /></div>
            <div className={'conv-web-md' + (clamped ? ' conv-web-md--clamp' : '')}>
              <Markdown>{text}</Markdown>
            </div>
            {clamped && (
              <div className="conv-web-more">
                <button type="button" onClick={() => setExpanded(true)}>Show full summary ↓</button>
              </div>
            )}
          </>
        ) : (
          call.result == null && (
            <div className="conv-tool-io-label conv-tool-io-label--none">no result</div>
          )
        )}
        {call.result?.media?.map((m) => (
          <MediaFigure key={m.index} media={m} toolUseId={call.tool_use_id} context="WebFetch" />
        ))}
        {call.result?.truncated && full == null && call.tool_use_id && (
          <LoadFull
            toolUseId={call.tool_use_id}
            which="result"
            fullLength={call.result.full_length ?? null}
            label="load full summary"
            onLoaded={(p: FullPayload) => {
              if (p.which === 'result') setFull(p.text);
            }}
          />
        )}
      </div>
    </details>
  );
}
