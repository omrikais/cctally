import type { ConversationBlock } from '../types/conversation';
import { CopyButton } from './CopyButton';
import { PlugIcon } from './ConvIcons';

type Block = Extract<ConversationBlock, { kind: 'external_call' }>;

// #463 S3 §5.5 — the external-agent marker, rendered as a labelled disclosure
// rather than as the serialized prose the provider wrote into the assistant
// message. It is deliberately NOT a tool chip: these blocks never enter the
// chip vocabulary, the focus filters, the Files tab or the outline, so they
// borrow none of the tool chip's classes either.
export function ExternalAgentCall({ block }: { block: Block }) {
  const input = JSON.stringify(block.input ?? null, null, 2);
  return (
    <details className="conv-external-call">
      <summary>
        <span className="conv-chev" aria-hidden="true" />
        <PlugIcon />
        <span className="conv-external-call-label">External agent call</span>
        <span className="conv-external-call-name">{block.name}</span>
        {block.truncated && (
          <span className="conv-external-call-clipped">· input clipped</span>
        )}
      </summary>
      <div className="conv-external-call-body">
        <CopyButton text={input} />
        <pre className="conv-code">{input}</pre>
      </div>
    </details>
  );
}
