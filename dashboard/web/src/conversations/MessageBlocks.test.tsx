import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { MessageBlocks } from './MessageBlocks';
import type { ConversationBlock } from '../types/conversation';

describe('MessageBlocks', () => {
  it('omits text blocks (prose is rendered at the item level)', () => {
    const blocks: ConversationBlock[] = [{ kind: 'text', text: 'already shown' }];
    const { container } = render(<MessageBlocks blocks={blocks} />);
    // Only text → nothing renders.
    expect(container.querySelector('.conv-blocks')).toBeNull();
    expect(container.textContent).not.toContain('already shown');
  });

  it('renders a thinking chip as a disclosure with the prose body', () => {
    const blocks: ConversationBlock[] = [{ kind: 'thinking', text: 'pondering' }];
    const { container } = render(<MessageBlocks blocks={blocks} />);
    const details = container.querySelector('details.conv-chip--thinking');
    expect(details).not.toBeNull();
    expect(details!.querySelector('summary')!.textContent).toContain('Thinking');
    expect(container.textContent).toContain('pondering');
  });

  it('renders a tool_use chip with name and input summary', () => {
    const blocks: ConversationBlock[] = [{ kind: 'tool_use', name: 'Bash', input_summary: 'ls -la' }];
    const { container } = render(<MessageBlocks blocks={blocks} />);
    const details = container.querySelector('details.conv-chip--tool');
    expect(details).not.toBeNull();
    expect(details!.querySelector('summary')!.textContent).toContain('Bash');
    expect(container.querySelector('pre')!.textContent).toBe('ls -la');
  });

  it('renders a tool_use chip with the default name when null', () => {
    const blocks: ConversationBlock[] = [{ kind: 'tool_use', name: null, input_summary: '{}' }];
    const { container } = render(<MessageBlocks blocks={blocks} />);
    expect(container.querySelector('details.conv-chip--tool summary')!.textContent).toContain('tool');
  });

  it('renders a tool_result chip and flags error + truncated', () => {
    const blocks: ConversationBlock[] = [{ kind: 'tool_result', text: 'boom', truncated: true, is_error: true }];
    const { container } = render(<MessageBlocks blocks={blocks} />);
    const summary = container.querySelector('details.conv-chip--result summary')!;
    expect(summary.textContent).toContain('Result');
    expect(summary.textContent).toContain('error');
    expect(summary.textContent).toContain('truncated');
    expect(container.querySelector('pre')!.textContent).toBe('boom');
  });

  it('renders an image placeholder (no base64)', () => {
    const blocks: ConversationBlock[] = [{ kind: 'image', media_type: 'image/png', bytes: 1234 }];
    const { container } = render(<MessageBlocks blocks={blocks} />);
    expect(container.querySelector('img')).toBeNull();
    const span = container.querySelector('.conv-chip--media')!;
    expect(span.textContent).toContain('image/png');
    expect(span.textContent).toContain('1234 B');
  });

  it('renders a document placeholder', () => {
    const blocks: ConversationBlock[] = [{ kind: 'document', media_type: 'application/pdf', bytes: 99 }];
    const { container } = render(<MessageBlocks blocks={blocks} />);
    const span = container.querySelector('.conv-chip--media')!;
    expect(span.textContent).toContain('application/pdf');
    expect(span.textContent).toContain('99 B');
  });

  it('renders a tool_reference span', () => {
    const blocks: ConversationBlock[] = [{ kind: 'tool_reference', name: 'WebFetch' }];
    const { container } = render(<MessageBlocks blocks={blocks} />);
    const span = container.querySelector('.conv-chip--ref')!;
    expect(span.textContent).toContain('WebFetch');
  });

  it('renders every non-text kind together and drops the text block', () => {
    const blocks: ConversationBlock[] = [
      { kind: 'text', text: 'prose' },
      { kind: 'thinking', text: 'hmm' },
      { kind: 'tool_use', name: 'Read', input_summary: 'file' },
      { kind: 'tool_result', text: 'ok', truncated: false, is_error: false },
      { kind: 'image', media_type: null, bytes: 0 },
      { kind: 'document', media_type: null, bytes: 0 },
      { kind: 'tool_reference', name: null },
    ];
    const { container } = render(<MessageBlocks blocks={blocks} />);
    // 6 non-text chips; text dropped.
    expect(container.querySelectorAll('.conv-chip')).toHaveLength(6);
    expect(container.textContent).not.toContain('prose');
  });
});
