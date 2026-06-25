import { render, fireEvent, act } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { BashCard } from './BashCard';
import { TranscriptContext } from './TranscriptContext';
import { HighlightContext } from './HighlightContext';
import type { ConversationBlock } from '../types/conversation';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

// Bash-shaped tool_call builder (base()/over convention).
const base = (over: Partial<Call> = {}): Call =>
  ({
    kind: 'tool_call',
    name: 'Bash',
    input_summary: '{}',
    preview: 'ls',
    tool_use_id: 'b1',
    input: { command: 'ls -la' },
    result: { text: 'out\nboom', truncated: false, is_error: true },
    stderr: 'boom',
    interrupted: false,
    ...over,
  }) as Call;

function renderCard(call: Call, sessionId: string | null = 's1') {
  return render(
    <TranscriptContext.Provider value={{ sessionId }}>
      <BashCard call={call} />
    </TranscriptContext.Provider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('BashCard', () => {
  it('renders the command as $ <command>, bash-highlighted', () => {
    const { container } = renderCard(base());
    const cmd = container.querySelector('.conv-term-cmd');
    expect(cmd?.textContent).toContain('$');
    expect(cmd?.textContent).toContain('ls -la');
  });

  it('splits stderr into a red block and badges error', () => {
    const { container } = renderCard(base());
    const stderr = container.querySelector('.conv-term-stderr');
    expect(stderr?.textContent).toContain('boom');
    // stdout portion = result.text minus the trailing stderr suffix.
    expect(container.querySelector('.conv-term-out')?.textContent).toContain('out');
    expect(container.querySelector('.conv-term-out')?.textContent).not.toContain('boom');
    expect(container.querySelector('.conv-term-badge--err')).toBeTruthy();
  });

  it('legacy row without stderr renders merged output, no stderr block', () => {
    const { container } = renderCard(
      base({ stderr: undefined, result: { text: 'just out', truncated: false, is_error: false } }),
    );
    expect(container.querySelector('.conv-term-stderr')).toBeNull();
    expect(container.querySelector('.conv-term-out')?.textContent).toContain('just out');
    expect(container.querySelector('.conv-term-badge--err')).toBeNull();
  });

  it('stderr present but not a suffix of result.text → merged output (guarded fallback)', () => {
    const { container } = renderCard(
      base({ stderr: 'XYZ', result: { text: 'out only', truncated: false, is_error: false } }),
    );
    // No suffix match → no split; render whole text, no red stderr block.
    expect(container.querySelector('.conv-term-stderr')).toBeNull();
    expect(container.querySelector('.conv-term-out')?.textContent).toContain('out only');
  });

  it('interrupted → interrupted badge', () => {
    const { container } = renderCard(
      base({ stderr: undefined, interrupted: true, result: { text: '', truncated: false, is_error: false } }),
    );
    expect(container.querySelector('.conv-term-badge--int')).toBeTruthy();
  });

  it('request-only (result === null) → command only, no terminal body', () => {
    const { container } = renderCard(base({ result: null, stderr: undefined }));
    expect(container.querySelector('.conv-term-cmd')).toBeTruthy();
    expect(container.querySelector('.conv-term-out')).toBeNull();
    expect(container.querySelector('.conv-term-stderr')).toBeNull();
  });

  it('result.truncated → load-full output affordance that re-renders', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        which: 'result',
        tool_use_id: 'b1',
        text: 'FULL OUTPUT',
        full_length: 99999,
        truncated: false,
        is_error: false,
      }),
    });
    vi.stubGlobal('fetch', fetchMock);
    const call = base({
      stderr: undefined,
      result: { text: 'partial', truncated: true, is_error: false, full_length: 99999 },
    });
    const { container, getByRole } = renderCard(call);
    const btn = getByRole('button', { name: /load full output/i });
    await act(async () => {
      fireEvent.click(btn);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(container.querySelector('.conv-term-out')?.textContent).toContain('FULL OUTPUT');
  });

  it('load-full preserves the stderr split from the loaded payload', async () => {
    // The truncated result carries discrete stderr in its full payload. After
    // load-full, the red stderr block must persist AND the stdout block must NOT
    // contain the stderr suffix — re-split against the LOADED stderr, not null.
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        which: 'result',
        tool_use_id: 'b1',
        text: 'OUT\nERRTAIL',
        stderr: 'ERRTAIL',
        full_length: 12345,
        truncated: false,
        is_error: true,
      }),
    });
    vi.stubGlobal('fetch', fetchMock);
    const call = base({
      stderr: 'partialerr',
      result: { text: 'partial', truncated: true, is_error: true, full_length: 12345 },
    });
    const { container, getByRole } = renderCard(call);
    const btn = getByRole('button', { name: /load full output/i });
    await act(async () => {
      fireEvent.click(btn);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    // Red stderr block still renders the loaded stderr text.
    const stderr = container.querySelector('.conv-term-stderr');
    expect(stderr?.textContent).toContain('ERRTAIL');
    // stdout block carries only the stdout portion, NOT the stderr suffix.
    const out = container.querySelector('.conv-term-out');
    expect(out?.textContent).toContain('OUT');
    expect(out?.textContent).not.toContain('ERRTAIL');
  });
});

describe('BashCard collapse heuristic (#217 S3 E9)', () => {
  // A long output (> the line threshold) renders the <details> CLOSED by default
  // so a 200-line `ls` doesn't bury the next turn. We assert on the `open`
  // attribute/state — never by click-toggling a <button> nested in <summary>
  // (vacuous in JSDOM/Chromium per the button-in-summary gotcha).
  const longOutput = (n: number) => Array.from({ length: n }, (_, i) => `line ${i + 1}`).join('\n');

  it('long output (> threshold) renders <details> collapsed (not open)', () => {
    const { container } = renderCard(
      base({
        stderr: undefined,
        result: { text: longOutput(40), truncated: false, is_error: false },
      }),
    );
    const d = container.querySelector('details.conv-term') as HTMLDetailsElement;
    expect(d.open).toBe(false);
  });

  it('short output (≤ threshold) stays open', () => {
    const { container } = renderCard(
      base({
        stderr: undefined,
        result: { text: longOutput(3), truncated: false, is_error: false },
      }),
    );
    const d = container.querySelector('details.conv-term') as HTMLDetailsElement;
    expect(d.open).toBe(true);
  });

  it('collapsed long output shows a "show N lines" summary hint', () => {
    const { container } = renderCard(
      base({
        stderr: undefined,
        result: { text: longOutput(40), truncated: false, is_error: false },
      }),
    );
    const hint = container.querySelector('.conv-term-collapsed-hint');
    expect(hint?.textContent).toMatch(/show\s+40\s+lines/i);
  });

  it('a request-only card (no result) stays open — nothing to collapse', () => {
    const { container } = renderCard(base({ result: null, stderr: undefined }));
    const d = container.querySelector('details.conv-term') as HTMLDetailsElement;
    expect(d.open).toBe(true);
  });

  it('counts the rendered output (stdout + stderr) lines, not the raw command', () => {
    // 30-line stdout split from a trailing stderr suffix → over threshold even
    // though the command itself is one line.
    const stdout = Array.from({ length: 25 }, (_, i) => `o${i}`).join('\n');
    const stderr = Array.from({ length: 5 }, (_, i) => `e${i}`).join('\n');
    const text = stdout + '\n' + stderr;
    const { container } = renderCard(
      base({ stderr, result: { text, truncated: false, is_error: true } }),
    );
    const d = container.querySelector('details.conv-term') as HTMLDetailsElement;
    expect(d.open).toBe(false);
  });
});

describe('BashCard dimmed line (#193)', () => {
  it('shows input.description when present', () => {
    const { container } = renderCard(
      base({ input: { command: 'ls -la', description: 'List files' }, preview: 'ls -la' }),
    );
    // The dimmed chip-preview slot carries the human description, not the command.
    expect(container.querySelector('.conv-chip-preview')!.textContent).toBe('List files');
    // The command still lives in the expanded `$ …` body.
    expect(container.querySelector('.conv-term-cmd')!.textContent).toContain('ls -la');
  });

  it('falls back to preview (command) when description blank/absent', () => {
    const blank = renderCard(
      base({ input: { command: 'ls -la', description: '   ' }, preview: 'ls -la' }),
    );
    expect(blank.container.querySelector('.conv-chip-preview')!.textContent).toBe('ls -la');

    const absent = renderCard(base({ input: { command: 'ls -la' }, preview: 'ls -la' }));
    expect(absent.container.querySelector('.conv-chip-preview')!.textContent).toBe('ls -la');
  });
});

// #217 S5 §4 / I-1.4 — a second copy action copies the full session output:
// `$ <command>` + stdout + a stderr block + a `… [truncated]` marker when the
// result is truncated (Codex P2-3 — no auto load-full).
describe('BashCard full-session copy (#217 S5)', () => {
  it('copies command + stdout (+ stderr) with a truncation marker when truncated', () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const call = base({
      input: { command: 'make build' },
      result: { text: 'compiling…\nboom', truncated: true, is_error: true },
      stderr: 'boom',
    });
    const { getByRole } = renderCard(call);
    fireEvent.click(getByRole('button', { name: /copy full/i }));
    expect(writeText).toHaveBeenCalledTimes(1);
    const copied = writeText.mock.calls[0][0] as string;
    expect(copied).toContain('$ make build');
    expect(copied).toContain('compiling…');
    expect(copied).toContain('boom');
    expect(copied).toContain('[truncated]');
  });

  it('omits the truncation marker when the result is complete', () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const call = base({
      input: { command: 'echo hi' },
      result: { text: 'hi', truncated: false, is_error: false },
      stderr: null,
    });
    const { getByRole } = renderCard(call);
    fireEvent.click(getByRole('button', { name: /copy full/i }));
    const copied = writeText.mock.calls[0][0] as string;
    expect(copied).toContain('$ echo hi');
    expect(copied).toContain('hi');
    expect(copied).not.toContain('[truncated]');
  });
});

// #236 — the Bash command body highlights find matches when find is open.
describe('BashCard find highlighting (#236)', () => {
  it('marks find terms in the command', () => {
    const { container } = render(
      <TranscriptContext.Provider value={{ sessionId: 's1' }}>
        <HighlightContext.Provider value={{ kind: 'terms', terms: ['flock'], caseSensitive: false }}>
          <BashCard call={base({ input: { command: 'flock cache.db.lock' }, result: null, stderr: undefined })} />
        </HighlightContext.Provider>
      </TranscriptContext.Provider>,
    );
    const mark = container.querySelector('.conv-term-cmd mark');
    expect(mark?.textContent).toBe('flock');
  });
});

void act;
