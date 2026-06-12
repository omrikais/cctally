import { describe, expect, it } from 'vitest';
import { specialToolRenderer } from './specialTools';
import type { ConversationBlock } from '../types/conversation';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

const call = (over: Partial<Call>): Call =>
  ({
    kind: 'tool_call',
    name: 'Edit',
    input_summary: '{}',
    preview: 'x',
    tool_use_id: 't1',
    result: null,
    ...over,
  }) as Call;

describe('specialToolRenderer dispatch (#177 S3)', () => {
  it('dispatches Edit/MultiEdit/Write/Bash with valid structured input', () => {
    expect(
      specialToolRenderer(call({ name: 'Edit', input: { file_path: '/f', old_string: 'a', new_string: 'b' } })),
    ).toBeTruthy();
    expect(
      specialToolRenderer(
        call({ name: 'MultiEdit', input: { file_path: '/f', edits: [{ old_string: 'a', new_string: 'b' }] } }),
      ),
    ).toBeTruthy();
    expect(specialToolRenderer(call({ name: 'Write', input: { file_path: '/f', content: 'x' } }))).toBeTruthy();
    expect(specialToolRenderer(call({ name: 'Bash', input: { command: 'ls' } }))).toBeTruthy();
  });

  it('case-insensitive on tool name', () => {
    expect(specialToolRenderer(call({ name: 'edit', input: { old_string: 'a', new_string: 'b' } }))).toBeTruthy();
    expect(specialToolRenderer(call({ name: 'BASH', input: { command: 'ls' } }))).toBeTruthy();
  });

  it('returns null (→ generic chip) when the structured input is absent/malformed', () => {
    // The guard returns null BEFORE constructing the card (Codex P1.2) — a card
    // that internally returned null would be a truthy element and vanish the tool.
    expect(specialToolRenderer(call({ name: 'Edit', input: null }))).toBeNull();
    expect(specialToolRenderer(call({ name: 'Edit', input: { old_string: 'a' } }))).toBeNull(); // no new_string
    expect(specialToolRenderer(call({ name: 'MultiEdit', input: { file_path: '/f' } }))).toBeNull(); // no edits[]
    // An empty edits[] passes Array.isArray but yields a hollow card → fall
    // through to the generic chip.
    expect(specialToolRenderer(call({ name: 'MultiEdit', input: { file_path: '/f', edits: [] } }))).toBeNull();
    expect(specialToolRenderer(call({ name: 'Write', input: { file_path: '/f' } }))).toBeNull(); // no content
    expect(specialToolRenderer(call({ name: 'Bash', input: null }))).toBeNull(); // no command
  });

  it('non-special tools always fall through to the generic chip (null)', () => {
    expect(specialToolRenderer(call({ name: 'Grep', input: { pattern: 'x' } }))).toBeNull();
    expect(specialToolRenderer(call({ name: 'Read', input: { file_path: '/f' } }))).toBeNull();
    expect(specialToolRenderer(call({ name: null, input: null }))).toBeNull();
  });

  it('preserves the existing Session-2 cases', () => {
    expect(specialToolRenderer(call({ name: 'AskUserQuestion', input: { questions: [] } }))).toBeTruthy();
    expect(specialToolRenderer(call({ name: 'TodoWrite', input: { todos: [] } }))).toBeTruthy();
    expect(specialToolRenderer(call({ name: 'ExitPlanMode', input: { plan: 'p' } }))).toBeTruthy();
    // ExitPlanMode with empty plan still falls through (existing defensive guard).
    expect(specialToolRenderer(call({ name: 'ExitPlanMode', input: { plan: '' } }))).toBeNull();
  });

  it('dispatches WebFetch/WebSearch only with their string input (Codex P1.2 guard)', () => {
    expect(specialToolRenderer(call({ name: 'WebFetch', input: { url: 'https://x.com' } }))).toBeTruthy();
    expect(specialToolRenderer(call({ name: 'WebSearch', input: { query: 'cats' } }))).toBeTruthy();
    // Case-insensitive.
    expect(specialToolRenderer(call({ name: 'webfetch', input: { url: 'https://x.com' } }))).toBeTruthy();
    expect(specialToolRenderer(call({ name: 'websearch', input: { query: 'cats' } }))).toBeTruthy();
    // Absent/malformed input → null (generic chip), guard runs before the card.
    expect(specialToolRenderer(call({ name: 'WebFetch', input: null }))).toBeNull();
    expect(specialToolRenderer(call({ name: 'WebFetch', input: { url: 42 } }))).toBeNull();
    expect(specialToolRenderer(call({ name: 'WebSearch', input: null }))).toBeNull();
    expect(specialToolRenderer(call({ name: 'WebSearch', input: { query: 42 } }))).toBeNull();
  });
});
