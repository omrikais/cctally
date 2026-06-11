import { render, screen, fireEvent } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MessageBlocks } from './MessageBlocks';
import { AskUserQuestionCard } from './AskUserQuestionCard'; // ensure import resolves
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

describe('MessageBlocks — tool I/O syntax highlighting', () => {
  const READ_PY = '1\timport os\n2\tprint(os.getcwd())';
  const readCall = (over = {}) =>
    call({ name: 'Read', preview: '/a/foo.py', input_summary: '{"file_path":"/a/foo.py"}',
           result: { text: READ_PY, truncated: false, is_error: false }, ...over });

  it('REQUEST is json-highlighted (Bash → result plain, so tokens prove the request)', () => {
    const { container } = render(
      <MessageBlocks blocks={[call({ name: 'Bash', preview: 'ls', input_summary: '{"command":"ls -la"}',
        result: { text: 'plain output', truncated: false, is_error: false } })]} />,
    );
    expect(container.querySelector('.conv-code--numbered')).toBeNull();
    expect(container.querySelector('.token')).toBeInTheDocument(); // from the json REQUEST
  });

  it('Read .py RESULT renders a numbered gutter + token spans', () => {
    const { container } = render(<MessageBlocks blocks={[readCall()]} />);
    expect(container.querySelector('.conv-code--numbered .cb-gutter')?.textContent).toBe('1\n2');
    expect(container.querySelector('.conv-code--numbered .token')).toBeInTheDocument();
  });

  it('Bash RESULT stays plain (no gutter)', () => {
    const { container } = render(
      <MessageBlocks blocks={[call({ name: 'Bash', preview: 'ls', input_summary: '{"command":"ls"}',
        result: { text: 'file1\nfile2', truncated: false, is_error: false } })]} />,
    );
    expect(container.querySelector('.conv-code--numbered')).toBeNull();
    expect(container.querySelector('pre.conv-code--result')?.textContent).toBe('file1\nfile2');
  });

  it('an error Read stays on the plain path (no mis-highlight)', () => {
    const { container } = render(
      <MessageBlocks blocks={[readCall({ result: { text: 'File does not exist.', truncated: false, is_error: true } })]} />,
    );
    expect(container.querySelector('.conv-code--numbered')).toBeNull();
    expect(container.querySelector('pre.conv-code--result')?.textContent).toBe('File does not exist.');
  });

  it('Edit/Write/MultiEdit/NotebookEdit results stay plain (v1 scope)', () => {
    for (const name of ['Edit', 'Write', 'MultiEdit', 'NotebookEdit']) {
      const { container } = render(
        <MessageBlocks blocks={[call({ name, preview: '/a/foo.py',
          result: { text: '1\tedited line', truncated: false, is_error: false } })]} />,
      );
      expect(container.querySelector('.conv-code--numbered')).toBeNull();
    }
  });

  it('SECURITY: raw HTML in a Read result stays escaped text', () => {
    const { container } = render(
      <MessageBlocks blocks={[readCall({ result: { text: '1\t<script>alert(1)</script>', truncated: false, is_error: false } })]} />,
    );
    expect(container.querySelector('script')).toBeNull();
    expect(container.textContent).toContain('<script>alert(1)</script>');
  });
});

describe('MessageBlocks — Skill tool_call body (skill-content nesting)', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  const skillCall = (over: Partial<Extract<ConversationBlock, { kind: 'tool_call' }>> = {}) =>
    call({
      name: 'Skill',
      input_summary: '{"skill":"brainstorming"}',
      preview: 'brainstorming',
      tool_use_id: 'toolu_S',
      result: null,
      skill_body: '# Heading\n\nsome **body** text',
      skill_name: 'brainstorming',
      ...over,
    });

  it('renders the skill body as rich markdown with no request/result panels, collapsed', () => {
    const { container } = render(<MessageBlocks blocks={[skillCall()]} />);
    // No request/result I/O panels for a skill chip.
    expect(container.querySelector('.conv-tool-io-label')).toBeNull();
    expect(container.querySelector('.conv-chip-body--io')).toBeNull();
    // The markdown body rendered (heading text present, bold rendered to <strong>).
    expect(container.textContent).toContain('Heading');
    expect(container.querySelector('.md')).not.toBeNull();
    expect(container.querySelector('strong')!.textContent).toBe('body');
    // Collapsed by default.
    expect((container.querySelector('details.conv-chip--tool') as HTMLDetailsElement).open).toBe(false);
    // Header unchanged: Skill name + the skill-name preview.
    const summary = container.querySelector('summary')!;
    expect(summary.textContent).toContain('Skill');
    expect(summary.textContent).toContain('brainstorming');
    // No · error / · truncated / · ok status (result is dropped).
    expect(container.querySelector('.conv-chip-status')).toBeNull();
  });

  it('exposes a copy button for the skill body', () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const { container } = render(
      <MessageBlocks blocks={[skillCall({ skill_body: 'the-skill-body' })]} />,
    );
    (container.querySelector('details.conv-chip--tool') as HTMLDetailsElement).open = true;
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }));
    expect(writeText).toHaveBeenCalledWith('the-skill-body');
  });

  it('a normal tool_call (no skill_body) is unchanged — request/result panels render', () => {
    const { container } = render(
      <MessageBlocks blocks={[call({ name: 'Bash', input_summary: 'ls', preview: 'ls' })]} />,
    );
    expect(container.querySelector('.conv-chip-body--io')).not.toBeNull();
    expect(container.querySelector('.conv-tool-io-label')).not.toBeNull();
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

describe('MessageBlocks — Session 2 special tool dispatch', () => {
  it('the AskUserQuestion card component resolves', () => {
    expect(AskUserQuestionCard).toBeTypeOf('function');
  });
  it('routes AskUserQuestion to the Q&A card (not the generic chip)', () => {
    const { container } = render(<MessageBlocks blocks={[call({
      name: 'AskUserQuestion',
      input: { questions: [{ question: 'Pick?', header: 'H', multiSelect: false,
        options: [{ label: 'X', description: 'd' }] }] },
      answers: { 'Pick?': 'X' },
    })]} />);
    expect(container.querySelector('.conv-ask')).toBeTruthy();
    expect(screen.getByText('Pick?')).toBeInTheDocument();
  });
  it('routes TodoWrite and ExitPlanMode to their cards', () => {
    const { container: a } = render(<MessageBlocks blocks={[call({
      name: 'TodoWrite', input: { todos: [{ content: 'c', status: 'pending', activeForm: 'c' }] } })]} />);
    expect(a.querySelector('.conv-todo')).toBeTruthy();
    const { container: b } = render(<MessageBlocks blocks={[call({
      name: 'ExitPlanMode', input: { plan: '# P' }, result: null })]} />);
    expect(b.querySelector('.conv-plan')).toBeTruthy();
  });
  it('an ExitPlanMode with an empty/missing plan falls through to the generic chip', () => {
    // Empty plan string → generic chip (defensive), NOT the plan card.
    const { container: empty } = render(<MessageBlocks blocks={[call({
      name: 'ExitPlanMode', input: { plan: '' }, result: null })]} />);
    expect(empty.querySelector('.conv-plan')).toBeNull();
    expect(empty.querySelector('.conv-chip--tool')).toBeTruthy();
    // No plan key at all → also the generic chip.
    const { container: missing } = render(<MessageBlocks blocks={[call({
      name: 'ExitPlanMode', input: {}, result: null })]} />);
    expect(missing.querySelector('.conv-plan')).toBeNull();
    expect(missing.querySelector('.conv-chip--tool')).toBeTruthy();
    // Sanity: a non-empty plan still routes to the plan card.
    const { container: present } = render(<MessageBlocks blocks={[call({
      name: 'ExitPlanMode', input: { plan: '# P' }, result: null })]} />);
    expect(present.querySelector('.conv-plan')).toBeTruthy();
  });
  it('leaves a non-special tool on the generic chip', () => {
    const { container } = render(<MessageBlocks blocks={[call({ name: 'Read', preview: '/x' })]} />);
    expect(container.querySelector('.conv-ask')).toBeNull();
    expect(container.querySelector('.conv-chip--tool')).toBeTruthy();
  });
  it('AskUserQuestion card is a <details> so [ / ] collapse-all still reaches it', () => {
    const { container } = render(<MessageBlocks blocks={[call({
      name: 'AskUserQuestion',
      input: { questions: [{ question: 'Pick?', header: 'H', multiSelect: false, options: [] }] } })]} />);
    const d = container.querySelector('.conv-ask') as HTMLDetailsElement;
    expect(d.tagName.toLowerCase()).toBe('details');
    d.open = false; // the reader's [ sweep sets .open=false on every <details>
    expect(d.open).toBe(false);
  });
});

describe('MessageBlocks — Task* checklist run collapses to one card', () => {
  const taskSnap = [
    { content: 'Alpha', status: 'completed', activeForm: 'Alphaing' },
    { content: 'Beta', status: 'in_progress', activeForm: 'Betaing' },
    { content: 'Gamma', status: 'pending', activeForm: 'Gammaing' },
  ];

  it('a Task* run (first call carries task_snapshot) renders ONE Tasks card, no run head', () => {
    // Two TaskCreate calls + a TaskUpdate; only the FIRST carries the snapshot
    // (the kernel stamps the run's first call). The whole run collapses to one
    // checklist card — NOT three generic chips with a "tool run · N actions" head.
    const { container } = render(<MessageBlocks blocks={[
      call({ name: 'TaskCreate', tool_use_id: 'c1', task_snapshot: taskSnap,
             input: { subject: 'Alpha', activeForm: 'Alphaing' } }),
      call({ name: 'TaskCreate', tool_use_id: 'c2',
             input: { subject: 'Beta', activeForm: 'Betaing' } }),
      call({ name: 'TaskUpdate', tool_use_id: 'u1',
             input: { taskId: '1', status: 'in_progress' } }),
    ]} />);
    expect(container.querySelectorAll('.conv-todo')).toHaveLength(1);
    expect(container.querySelector('.conv-chip-name')?.textContent).toBe('Tasks');
    expect(container.querySelector('.conv-toolrun-head')).toBeNull();
    // none of the individual Task* calls leaked as a generic tool chip
    expect(container.querySelector('.conv-chip--tool')).toBeNull();
    // the snapshot rendered (1 of 3 done)
    expect(container.querySelector('.conv-todo-frac')?.textContent?.replace(/\s+/g, ' '))
      .toContain('1 / 3');
  });

  it('a single Task* call carrying a snapshot also collapses to one Tasks card', () => {
    const { container } = render(<MessageBlocks blocks={[
      call({ name: 'TaskList', tool_use_id: 'l1', task_snapshot: taskSnap }),
    ]} />);
    expect(container.querySelectorAll('.conv-todo')).toHaveLength(1);
    expect(container.querySelector('.conv-chip-name')?.textContent).toBe('Tasks');
    expect(container.querySelector('.conv-toolrun-head')).toBeNull();
  });

  it('a Task* run whose first call lacks a snapshot stays a generic group (degrades)', () => {
    // Legacy / non-folded rows have no task_snapshot on the first call → the
    // checklist interception does not fire; the generic chips render.
    const { container } = render(<MessageBlocks blocks={[
      call({ name: 'TaskCreate', tool_use_id: 'c1', input: { subject: 'Alpha' } }),
      call({ name: 'TaskCreate', tool_use_id: 'c2', input: { subject: 'Beta' } }),
    ]} />);
    expect(container.querySelector('.conv-todo')).toBeNull();
    expect(container.querySelector('.conv-toolrun-head')).toBeTruthy(); // N>=2 head
    expect(container.querySelectorAll('.conv-chip--tool').length).toBeGreaterThan(0);
  });

  it('a non-Task run still renders the generic tool-run group', () => {
    const { container } = render(<MessageBlocks blocks={[
      call({ name: 'Read', tool_use_id: 't1', preview: '/a' }),
      call({ name: 'Read', tool_use_id: 't2', preview: '/b' }),
    ]} />);
    expect(container.querySelector('.conv-todo')).toBeNull();
    expect(container.querySelector('.conv-toolrun-head')).toBeTruthy();
    expect(container.querySelectorAll('.conv-chip--tool').length).toBe(2);
  });
});
