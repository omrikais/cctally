import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ExternalAgentCall } from './ExternalAgentCall';
import type { ConversationBlock } from '../types/conversation';

type Block = Extract<ConversationBlock, { kind: 'external_call' }>;

const block = (over: Partial<Block> = {}): Block => ({
  kind: 'external_call', name: 'ToolSearch',
  input: { query: 'select:SyntheticAlpha,SyntheticBeta' },
  truncated: false, block_key: 'cbk.marker', ...over,
});

describe('ExternalAgentCall (#463 S3 §5.5)', () => {
  it('names the invoked tool in the summary and holds its input behind a disclosure', () => {
    const { container } = render(<ExternalAgentCall block={block()} />);
    const details = container.querySelector('details.conv-external-call');
    expect(details).toBeTruthy();
    expect(details?.querySelector('summary')?.textContent).toContain('ToolSearch');
    expect(container.querySelector('pre')?.textContent).toContain('SyntheticAlpha');
  });

  it('is not a tool chip and carries no tool styling', () => {
    // §5.5 — these blocks must not enter chips, filters, the Files tab or the
    // outline, so they must not borrow the tool chip's class either.
    const { container } = render(<ExternalAgentCall block={block()} />);
    expect(container.querySelector('.conv-chip--tool')).toBeNull();
  });

  it('reports a bounded input as bounded', () => {
    const { container } = render(<ExternalAgentCall block={block({ truncated: true })} />);
    expect(container.textContent).toContain('clipped');
  });

  it('renders a non-object input without throwing', () => {
    const { container } = render(<ExternalAgentCall block={block({ input: 'a bare string' })} />);
    expect(container.querySelector('pre')?.textContent).toContain('a bare string');
  });
});
