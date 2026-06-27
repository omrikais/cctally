import { useState } from 'react';
import type { ComponentPropsWithoutRef } from 'react';
import type { ConversationBlock, FullPayload } from '../types/conversation';
import { Markdown, MdLink } from '../components/Markdown';
import { CodexIcon } from './ConvIcons';
import { CopyButton } from './CopyButton';
import { LoadFull } from './LoadFull';
import { parseCodexEnvelope, codexMeta, responseIsLong } from './codexEnvelope';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

// Codex content cites local files as [label](<path:line>); the reader can't open
// those, so render http(s) via MdLink (new tab) and everything else as a
// non-clickable chip. children may be React nodes (the find-bar rehype plugin
// wraps link text in <mark>), so render them as-is — never assume a string.
function CitationLink({ href, children, ...rest }: ComponentPropsWithoutRef<'a'>) {
  const isHttp = typeof href === 'string' && /^https?:\/\//i.test(href);
  if (isHttp) return <MdLink href={href} {...rest}>{children}</MdLink>;
  return <span className="conv-codex-cite">{children}</span>;
}
const CODEX_MD = { a: CitationLink };

// The card assumes a usable Codex prompt — the input guard lives in
// specialToolRenderer, so we never return null. `result` may be null
// (request-only / still-active call) and must still render the card.
export function CodexCard({ call }: { call: Call }) {
  const [fullResultText, setFullResultText] = useState<string | null>(null);
  const [fullInput, setFullInput] = useState<Record<string, unknown> | null>(null);
  const [promptExpanded, setPromptExpanded] = useState(false);
  const [responseExpanded, setResponseExpanded] = useState(false);

  const input = fullInput ?? call.input ?? null;
  const meta = codexMeta(input);
  const prompt = typeof (input as { prompt?: unknown } | null)?.prompt === 'string'
    ? (input as { prompt: string }).prompt : '';
  const preview = prompt.split('\n', 1)[0];

  const result = call.result;
  const responseRaw = fullResultText ?? result?.text ?? '';
  const parsed = parseCodexEnvelope(responseRaw);
  const isError = parsed.kind === 'error' || !!result?.is_error;

  const statusText = result == null
    ? ''
    : isError
      ? (parsed.kind === 'error' && parsed.status ? `✗ ${parsed.status}` : '✗ error')
      : '✓ ok';

  // The "agent run" status bar only earns its space when it has at least one
  // field. A bare codex-reply carries just prompt + threadId, which would leave
  // a lone dot — hide the bar entirely in that case (the thread chip already
  // lives in the summary).
  const hasBarMeta = !!(meta.model || meta.effort || meta.sandbox || meta.approval || meta.cwdBase);

  return (
    <details className={`conv-chip conv-codex${isError ? ' conv-codex--error' : ''}`}>
      <summary>
        <span className="conv-chev" aria-hidden="true" />
        <CodexIcon />
        <span className="conv-chip-name conv-codex-brand">codex</span>
        {meta.threadId
          ? <span className="conv-codex-thread">↩ thread …{meta.threadId.slice(-4)}</span>
          : meta.model && <span className="conv-codex-model">{meta.model}</span>}
        {statusText && <span className={`conv-codex-summary-status${isError ? ' conv-codex-summary-status--err' : ''}`}>{statusText}</span>}
        {preview && <span className="conv-chip-preview">{preview}</span>}
      </summary>

      <div className="conv-codex-body">
        {hasBarMeta && (
          <div className="conv-codex-bar">
            <span className="conv-codex-dot" aria-hidden="true" />
            {meta.model && <span className="conv-codex-bar-model">{meta.model}</span>}
            {meta.effort && <span className="conv-codex-bar-item">effort {meta.effort}</span>}
            {meta.sandbox && <span className="conv-codex-bar-item">{meta.sandbox}</span>}
            {meta.approval && <span className="conv-codex-bar-item">approval {meta.approval}</span>}
            {meta.cwdBase && <span className="conv-codex-bar-cwd">{meta.cwdBase}</span>}
          </div>
        )}

        {prompt && (
          <div className="conv-codex-prompt">
            <button type="button" className="conv-codex-prompt-toggle" onClick={() => setPromptExpanded((v) => !v)}>
              <span className="conv-codex-prompt-tag">{promptExpanded ? '▾' : '▸'} prompt</span>
              {!promptExpanded && <span className="conv-codex-prompt-line">{preview}</span>}
            </button>
            {promptExpanded && (
              <div className="conv-codex-prompt-md">
                <Markdown components={CODEX_MD}>{prompt}</Markdown>
              </div>
            )}
            {promptExpanded && call.input_truncated && fullInput == null && call.tool_use_id && (
              <LoadFull
                toolUseId={call.tool_use_id}
                which="input"
                fullLength={null}
                label="load full prompt"
                onLoaded={(p: FullPayload) => { if (p.which === 'input') setFullInput(p.input); }}
              />
            )}
          </div>
        )}

        {renderResponse()}
      </div>
    </details>
  );

  // Plain render helper — NOT a nested component. Calling it inlines the JSX into
  // the body so children (LoadFull/CopyButton state, the <Markdown> tree) survive
  // parent re-renders; a `<CodexResponse />` element would get a fresh identity
  // each render and remount the whole subtree (resetting an in-flight load and
  // re-parsing Markdown on every Virtuoso scroll). It reads parent state via
  // closure and owns no hooks, so the function-call form is safe.
  function renderResponse() {
    if (result == null) {
      return <div className="conv-tool-io-label conv-tool-io-label--none">no result</div>;
    }
    if (parsed.kind === 'error') {
      return (
        <div className="conv-codex-error">
          <div className="conv-codex-error-head">✗ {parsed.errorType ?? 'error'}{parsed.status ? ` · HTTP ${parsed.status}` : ''}</div>
          <div className="conv-codex-error-msg">{parsed.message}</div>
        </div>
      );
    }
    // result is non-null past the guard above. The result LoadFull is identical
    // across the raw + ok branches, so build it once.
    const resultLoadFull = result.truncated && fullResultText == null && call.tool_use_id ? (
      <LoadFull
        toolUseId={call.tool_use_id}
        which="result"
        fullLength={result.full_length ?? null}
        label="load full response"
        onLoaded={(p: FullPayload) => { if (p.which === 'result') setFullResultText(p.text); }}
      />
    ) : null;
    if (parsed.kind === 'raw') {
      if (isError) {
        return (
          <div className="conv-codex-error">
            <div className="conv-codex-error-head">✗ error</div>
            <div className="conv-codex-error-msg">{parsed.text}</div>
          </div>
        );
      }
      return (
        <>
          {result.truncated && fullResultText == null
            ? <div className="conv-codex-truncated">Response truncated — load the full response below.</div>
            : <pre className="conv-code conv-code--result">{parsed.text}</pre>}
          {resultLoadFull}
        </>
      );
    }
    // kind === 'ok'
    const content = parsed.content;
    const clamped = responseIsLong(content) && !responseExpanded;
    return (
      <>
        <div className="conv-codex-reslabel">↩ response <CopyButton text={content} /></div>
        <div className={'conv-codex-md' + (clamped ? ' conv-codex-md--clamp' : '')}>
          <Markdown components={CODEX_MD}>{content}</Markdown>
        </div>
        {clamped && (
          <div className="conv-codex-more">
            <button type="button" onClick={() => setResponseExpanded(true)}>Show full response ↓</button>
          </div>
        )}
        {resultLoadFull}
      </>
    );
  }
}
