import { render, screen, fireEvent } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MessageBlocks } from './MessageBlocks';
import type { ConversationBlock } from '../types/conversation';

const call = (
  over: Partial<Extract<ConversationBlock, { kind: 'tool_call' }>> = {},
): Extract<ConversationBlock, { kind: 'tool_call' }> => ({
  kind: 'tool_call',
  name: 'Read',
  input_summary: '{}',
  preview: '/a',
  tool_use_id: 't1',
  result: { text: 'A', truncated: false, is_error: false },
  ...over,
});

describe('MessageBlocks (ordered walk + tool-run grouping)', () => {
  it('renders text blocks as prose in document order (#164)', () => {
    render(
      <MessageBlocks
        blocks={[
          { kind: 'text', text: 'Reading the spec.' },
          call({ preview: '/spec.md' }),
        ]}
      />,
    );
    expect(screen.getByText('Reading the spec.')).toBeInTheDocument();
    expect(screen.getByText('/spec.md')).toBeInTheDocument();
  });

  it('coalesces consecutive text blocks into one Markdown render', () => {
    const { container } = render(
      <MessageBlocks
        blocks={[
          { kind: 'text', text: 'para one' },
          { kind: 'text', text: 'para two' },
        ]}
      />,
    );
    // One coalesced <Markdown> wrapper, not two.
    expect(container.querySelectorAll('.md')).toHaveLength(1);
    expect(container.textContent).toContain('para one');
    expect(container.textContent).toContain('para two');
  });

  it('groups consecutive tool_call blocks under a run head when N>=2', () => {
    render(
      <MessageBlocks
        blocks={[
          call({ name: 'Read', preview: '/a', tool_use_id: 't1', result: { text: 'A', truncated: false, is_error: false } }),
          call({ name: 'Bash', preview: 'ls', tool_use_id: 't2', result: { text: 'B', truncated: false, is_error: false } }),
        ]}
      />,
    );
    expect(screen.getByText(/tool run · 2 actions/i)).toBeInTheDocument();
    expect(screen.getByText('/a')).toBeInTheDocument(); // preview visible collapsed
    expect(screen.getByText('ls')).toBeInTheDocument();
  });

  it('single tool_call has no run head', () => {
    render(<MessageBlocks blocks={[call({ preview: '/a' })]} />);
    expect(screen.queryByText(/tool run ·/i)).toBeNull();
    expect(screen.getByText('/a')).toBeInTheDocument();
  });

  it('starts a new tool-run after a non-tool_call block interrupts', () => {
    const { container } = render(
      <MessageBlocks
        blocks={[
          call({ tool_use_id: 't1' }),
          { kind: 'text', text: 'mid' },
          call({ tool_use_id: 't2' }),
          call({ tool_use_id: 't3' }),
        ]}
      />,
    );
    // First run: single chip, no head. Second run: 2 chips, one head.
    const heads = container.querySelectorAll('.conv-toolrun-head');
    expect(heads).toHaveLength(1);
    expect(heads[0].textContent).toMatch(/2 actions/);
  });

  it('tool_call chip shows request + result on expand and an error status', () => {
    render(
      <MessageBlocks
        blocks={[
          call({
            name: 'Bash',
            input_summary: 'cmd',
            preview: 'ls',
            tool_use_id: 't1',
            result: { text: 'boom', truncated: false, is_error: true },
          }),
        ]}
      />,
    );
    // Summary carries the · error status.
    expect(screen.getByText(/^· error$/i)).toBeInTheDocument();
    // request + result bodies are rendered (inside the <details>).
    expect(screen.getByText('cmd')).toBeInTheDocument();
    expect(screen.getByText('boom')).toBeInTheDocument();
  });

  it('shows a truncated status and a "no result" note for a request-only call', () => {
    const { container } = render(
      <MessageBlocks
        blocks={[call({ preview: '/big.txt', result: { text: 'partial', truncated: true, is_error: false } })]}
      />,
    );
    expect(container.querySelector('.conv-chip-status')!.textContent).toContain('truncated');

    const { container: c2 } = render(
      <MessageBlocks blocks={[call({ tool_use_id: null, result: null })]} />,
    );
    expect(c2.textContent).toContain('no result');
  });

  it('every chip has a chevron affordance', () => {
    const { container } = render(<MessageBlocks blocks={[{ kind: 'thinking', text: 'hm' }]} />);
    expect(container.querySelector('.conv-chev')).not.toBeNull();
  });

  it('renders inline-SVG icons instead of emoji in chip summaries', () => {
    render(
      <MessageBlocks blocks={[{ kind: 'thinking', text: 'hm' }, call({ name: 'Read', preview: '/a' })]} />,
    );
    // Thinking chip: label still present, an aria-hidden svg in the summary, no emoji.
    const thinking = screen.getByText('Thinking').closest('summary')!;
    expect(thinking.querySelector('svg[aria-hidden="true"]')).toBeInTheDocument();
    expect(thinking.textContent).not.toMatch(/[💭🔧📤🖼📄↪⚙⏳⚠💬🧵]/);
    // Tool chip: name + svg, no emoji.
    const tool = screen.getByText('Read').closest('summary')!;
    expect(tool.querySelector('svg[aria-hidden="true"]')).toBeInTheDocument();
    expect(tool.textContent).not.toMatch(/[💭🔧📤🖼📄↪⚙⏳⚠💬🧵]/);
  });
});

describe('MessageBlocks (single-block kinds)', () => {
  it('renders nothing for an empty block list', () => {
    const { container } = render(<MessageBlocks blocks={[]} />);
    expect(container.querySelector('.conv-blocks')).toBeNull();
  });

  it('renders a thinking chip as a disclosure with the prose body + preview', () => {
    const blocks: ConversationBlock[] = [{ kind: 'thinking', text: 'pondering deeply' }];
    const { container } = render(<MessageBlocks blocks={blocks} />);
    const details = container.querySelector('details.conv-chip--thinking');
    expect(details).not.toBeNull();
    expect(details!.querySelector('summary')!.textContent).toContain('Thinking');
    expect(details!.querySelector('.conv-chip-preview')!.textContent).toBe('pondering deeply');
    expect(container.textContent).toContain('pondering deeply');
  });

  it('renders the tool_use degradation chip with name and input summary', () => {
    const blocks: ConversationBlock[] = [{ kind: 'tool_use', name: 'Bash', input_summary: 'ls -la' }];
    const { container } = render(<MessageBlocks blocks={blocks} />);
    const details = container.querySelector('details.conv-chip--tool');
    expect(details).not.toBeNull();
    expect(details!.querySelector('summary')!.textContent).toContain('Bash');
    expect(container.querySelector('pre')!.textContent).toBe('ls -la');
  });

  it('renders a tool_use degradation chip with the default name when null', () => {
    const blocks: ConversationBlock[] = [{ kind: 'tool_use', name: null, input_summary: '{}' }];
    const { container } = render(<MessageBlocks blocks={blocks} />);
    expect(container.querySelector('details.conv-chip--tool summary')!.textContent).toContain('tool');
  });

  it('renders an orphan tool_result chip and flags error + truncated', () => {
    const blocks: ConversationBlock[] = [{ kind: 'tool_result', text: 'boom', truncated: true, is_error: true }];
    const { container } = render(<MessageBlocks blocks={blocks} />);
    const summary = container.querySelector('details.conv-chip--result summary')!;
    expect(summary.textContent).toContain('Result');
    expect(summary.textContent).toContain('error');
    expect(summary.textContent).toContain('truncated');
    expect(summary.querySelector('svg[aria-hidden="true"]')).toBeInTheDocument();
    expect(summary.textContent).not.toMatch(/[💭🔧📤🖼📄↪⚙⏳⚠💬🧵]/);
    expect(container.querySelector('pre')!.textContent).toBe('boom');
  });

  it('renders an image placeholder (no base64)', () => {
    const blocks: ConversationBlock[] = [{ kind: 'image', media_type: 'image/png', bytes: 1234 }];
    const { container } = render(<MessageBlocks blocks={blocks} />);
    expect(container.querySelector('img')).toBeNull();
    const span = container.querySelector('.conv-chip--media')!;
    expect(span.textContent).toContain('image/png');
    expect(span.textContent).toContain('1234 B');
    expect(span.querySelector('svg[aria-hidden="true"]')).toBeInTheDocument();
    expect(span.textContent).not.toMatch(/[💭🔧📤🖼📄↪⚙⏳⚠💬🧵]/);
  });

  it('renders a document placeholder', () => {
    const blocks: ConversationBlock[] = [{ kind: 'document', media_type: 'application/pdf', bytes: 99 }];
    const { container } = render(<MessageBlocks blocks={blocks} />);
    const span = container.querySelector('.conv-chip--media')!;
    expect(span.textContent).toContain('application/pdf');
    expect(span.textContent).toContain('99 B');
    expect(span.querySelector('svg[aria-hidden="true"]')).toBeInTheDocument();
    expect(span.textContent).not.toMatch(/[💭🔧📤🖼📄↪⚙⏳⚠💬🧵]/);
  });

  it('renders a tool_reference span', () => {
    const blocks: ConversationBlock[] = [{ kind: 'tool_reference', name: 'WebFetch' }];
    const { container } = render(<MessageBlocks blocks={blocks} />);
    const span = container.querySelector('.conv-chip--ref')!;
    expect(span.textContent).toContain('WebFetch');
    expect(span.querySelector('svg[aria-hidden="true"]')).toBeInTheDocument();
    expect(span.textContent).not.toMatch(/[💭🔧📤🖼📄↪⚙⏳⚠💬🧵]/);
  });
});

describe('MessageBlocks (copy affordances, G2)', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('an open ToolCallChip exposes copy buttons for the request + result', () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const { container } = render(
      <MessageBlocks
        blocks={[
          call({ input_summary: 'the-request', result: { text: 'the-result', truncated: false, is_error: false } }),
        ]}
      />,
    );
    // Open the disclosure so the I/O bodies (and their copy buttons) mount.
    const details = container.querySelector('details.conv-chip--tool') as HTMLDetailsElement;
    details.open = true;
    const buttons = screen.getAllByRole('button', { name: 'Copy' });
    expect(buttons.length).toBeGreaterThanOrEqual(2);
    fireEvent.click(buttons[0]);
    fireEvent.click(buttons[1]);
    expect(writeText).toHaveBeenCalledWith('the-request');
    expect(writeText).toHaveBeenCalledWith('the-result');
  });

  it('an orphan tool_result body has a copy button copying the result text', () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(
      <MessageBlocks blocks={[{ kind: 'tool_result', text: 'orphan-out', truncated: false, is_error: false }]} />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }));
    expect(writeText).toHaveBeenCalledWith('orphan-out');
  });

  it('a tool_use degradation body has a copy button copying the input summary', () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(<MessageBlocks blocks={[{ kind: 'tool_use', name: 'Bash', input_summary: 'ls -la' }]} />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }));
    expect(writeText).toHaveBeenCalledWith('ls -la');
  });
});
