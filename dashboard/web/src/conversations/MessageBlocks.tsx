import { Markdown } from '../components/Markdown';
import type { ConversationBlock } from '../types/conversation';

// Collapsed-by-default block chips for a message (prose-first). `text`
// blocks are filtered out — the item's joined prose is already rendered
// via <Markdown> at the MessageItem level, so re-rendering them here would
// double the body. Every other block kind becomes a disclosure (thinking /
// tool_use / tool_result) or a compact non-collapsible placeholder span
// (image / document / tool_reference). No base64 image data is rendered.
export function MessageBlocks({ blocks }: { blocks: ConversationBlock[] }) {
  const renderable = blocks.filter((b) => b.kind !== 'text');
  if (renderable.length === 0) return null;
  return (
    <div className="conv-blocks">
      {renderable.map((block, i) => (
        <BlockChip key={i} block={block} />
      ))}
    </div>
  );
}

function BlockChip({ block }: { block: ConversationBlock }) {
  switch (block.kind) {
    case 'thinking':
      return (
        <details className="conv-chip conv-chip--thinking">
          <summary>💭 Thinking</summary>
          <div className="conv-chip-body"><Markdown>{block.text}</Markdown></div>
        </details>
      );
    case 'tool_use':
      return (
        <details className="conv-chip conv-chip--tool">
          <summary>🔧 {block.name ?? 'tool'}</summary>
          <pre className="conv-chip-body">{block.input_summary}</pre>
        </details>
      );
    case 'tool_result':
      return (
        <details className="conv-chip conv-chip--result">
          <summary>📤 Result{block.is_error ? ' · error' : ''}{block.truncated ? ' · truncated' : ''}</summary>
          <pre className="conv-chip-body">{block.text}</pre>
        </details>
      );
    case 'image':
      return <span className="conv-chip conv-chip--media">🖼 {block.media_type ?? 'image'} · {block.bytes} B</span>;
    case 'document':
      return <span className="conv-chip conv-chip--media">📄 {block.media_type ?? 'document'} · {block.bytes} B</span>;
    case 'tool_reference':
      return <span className="conv-chip conv-chip--ref">↪ {block.name ?? 'tool'}</span>;
    default:
      // `text` is filtered out before mapping; this is unreachable but keeps
      // the switch exhaustive for the discriminated union.
      return null;
  }
}
