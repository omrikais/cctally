import type { ReactNode } from 'react';
import { Markdown } from '../components/Markdown';
import {
  toolIcon,
  ThinkingIcon,
  ResultIcon,
  ImageIcon,
  DocumentIcon,
  ReferenceIcon,
} from './ConvIcons';
import { CopyButton } from './CopyButton';
import type { ConversationBlock } from '../types/conversation';

// Render a turn's blocks in DOCUMENT ORDER (#164): consecutive `text` coalesce
// into one <Markdown>; a maximal run of consecutive `tool_call` becomes one
// tool-run group (head only when N>=2); `thinking` is its own chip; media /
// references are inline placeholder spans (and terminate a tool-run). Every
// disclosure is a native <details> with a chevron. `tool_use` (id-less
// degradation) and `tool_result` (orphan item only) render as single chips too.
// This single source of truth is used by both the assistant turn (which renders
// its prose-from-text-blocks here, in order) and the human turn.
export function MessageBlocks({ blocks }: { blocks: ConversationBlock[] }) {
  const out: ReactNode[] = [];
  let i = 0;
  let textRun: string[] = [];
  const flushText = () => {
    if (textRun.length) {
      // Coalesced text fragments rejoin with a blank line so adjacent prose
      // paragraphs stay distinct in the rendered Markdown.
      out.push(<Markdown key={`t${out.length}`}>{textRun.join('\n\n')}</Markdown>);
      textRun = [];
    }
  };
  while (i < blocks.length) {
    const b = blocks[i];
    if (b.kind === 'text') {
      textRun.push(b.text);
      i++;
      continue;
    }
    flushText();
    if (b.kind === 'tool_call') {
      const run: Extract<ConversationBlock, { kind: 'tool_call' }>[] = [];
      while (i < blocks.length && blocks[i].kind === 'tool_call') {
        run.push(blocks[i] as Extract<ConversationBlock, { kind: 'tool_call' }>);
        i++;
      }
      out.push(<ToolRun key={`r${out.length}`} calls={run} />);
      continue;
    }
    out.push(<BlockChip key={`c${out.length}`} block={b} />);
    i++;
  }
  flushText();
  if (out.length === 0) return null;
  return <div className="conv-blocks">{out}</div>;
}

// A maximal run of consecutive tool_call blocks. A run of N>=2 gets a
// "tool run · N actions" head (label + trailing rule via CSS); a single call
// renders a bare chip with no head.
function ToolRun({ calls }: { calls: Extract<ConversationBlock, { kind: 'tool_call' }>[] }) {
  return (
    <div className="conv-toolrun">
      {calls.length >= 2 && (
        <div className="conv-toolrun-head">tool run · {calls.length} actions</div>
      )}
      {calls.map((c, i) => (
        <ToolCallChip key={i} call={c} />
      ))}
    </div>
  );
}

// One paired request+result disclosure. Collapsed: chevron · tool icon · name ·
// one-line preview · status (· error / · truncated). Expanded: the request
// (input_summary) plus the result body (result.text, scroll-capped) or a
// "no result" note when the request was never matched (result === null).
function ToolCallChip({ call }: { call: Extract<ConversationBlock, { kind: 'tool_call' }> }) {
  const status = call.result?.is_error
    ? ' · error'
    : call.result?.truncated
      ? ' · truncated'
      : '';
  return (
    <details className="conv-chip conv-chip--tool">
      <summary>
        <span className="conv-chev" aria-hidden="true" />
        {toolIcon(call.name)} <span className="conv-chip-name">{call.name ?? 'tool'}</span>
        <span className="conv-chip-preview">{call.preview}</span>
        {status && <span className="conv-chip-status">{status}</span>}
      </summary>
      <div className="conv-chip-body conv-chip-body--io">
        <div className="conv-tool-io">
          <div className="conv-tool-io-label">request</div>
          <CopyButton text={call.input_summary} />
          <pre className="conv-code">{call.input_summary}</pre>
        </div>
        {call.result ? (
          <div className="conv-tool-io">
            <div className="conv-tool-io-label">
              result{call.result.is_error ? ' · error' : ' · ok'}
              {call.result.truncated ? ' · truncated' : ''}
            </div>
            <CopyButton text={call.result.text} />
            <pre className="conv-code conv-code--result">{call.result.text}</pre>
          </div>
        ) : (
          <div className="conv-tool-io">
            <div className="conv-tool-io-label conv-tool-io-label--none">no result</div>
          </div>
        )}
      </div>
    </details>
  );
}

// First non-blank line of a block's text, trimmed + capped, for a collapsed
// chip's one-line preview.
function firstLine(s: string): string {
  const t = s.split('\n').map((l) => l.trim()).find((l) => l.length > 0) ?? '';
  return t.length > 80 ? `${t.slice(0, 80).trimEnd()}…` : t;
}

// Single non-text, non-tool_call block: thinking chip, the tool_use degradation
// fallback, an orphan tool_result chip, or an inline media/reference span.
function BlockChip({ block }: { block: ConversationBlock }) {
  switch (block.kind) {
    case 'thinking':
      return (
        <details className="conv-chip conv-chip--thinking">
          <summary>
            <span className="conv-chev" aria-hidden="true" />
            <ThinkingIcon /> <span className="conv-chip-name">Thinking</span>
            <span className="conv-chip-preview">{firstLine(block.text)}</span>
          </summary>
          <div className="conv-chip-body">
            <Markdown>{block.text}</Markdown>
          </div>
        </details>
      );
    case 'tool_use': // degradation only (id-less pre-migration rows)
      return (
        <details className="conv-chip conv-chip--tool">
          <summary>
            <span className="conv-chev" aria-hidden="true" />
            {toolIcon(block.name)} <span className="conv-chip-name">{block.name ?? 'tool'}</span>
          </summary>
          <div className="conv-chip-body conv-tool-io">
            <CopyButton text={block.input_summary} />
            <pre className="conv-code">{block.input_summary}</pre>
          </div>
        </details>
      );
    case 'tool_result': // orphan items only
      return (
        <details className="conv-chip conv-chip--result">
          <summary>
            <span className="conv-chev" aria-hidden="true" />
            <ResultIcon /> <span className="conv-chip-name">Result</span>
            <span className="conv-chip-preview">{firstLine(block.text)}</span>
            {block.is_error && <span className="conv-chip-status"> · error</span>}
            {block.truncated && <span className="conv-chip-status"> · truncated</span>}
          </summary>
          <div className="conv-chip-body conv-tool-io">
            <CopyButton text={block.text} />
            <pre className="conv-code">{block.text}</pre>
          </div>
        </details>
      );
    case 'image':
      return (
        <span className="conv-chip conv-chip--media">
          <ImageIcon /> {block.media_type ?? 'image'} · {block.bytes} B
        </span>
      );
    case 'document':
      return (
        <span className="conv-chip conv-chip--media">
          <DocumentIcon /> {block.media_type ?? 'document'} · {block.bytes} B
        </span>
      );
    case 'tool_reference':
      return <span className="conv-chip conv-chip--ref"><ReferenceIcon /> {block.name ?? 'tool'}</span>;
    default:
      return null; // text + tool_call are handled by the walk above
  }
}
