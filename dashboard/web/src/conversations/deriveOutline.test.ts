import { describe, it, expect } from 'vitest';
import { deriveOutline } from './deriveOutline';
import type { OutlineTurn, SubagentMeta } from '../types/conversation';

// Minimal OutlineTurn factory — every field the curation reads, sane defaults.
function turn(over: Partial<OutlineTurn> & { uuid: string; kind: OutlineTurn['kind'] }): OutlineTurn {
  return {
    ts: null,
    label: '',
    member_uuids: [over.uuid],
    subagent_key: null,
    parent_uuid: null,
    is_sidechain: false,
    ...over,
  };
}

describe('deriveOutline (#177 S5 §2 curation)', () => {
  it('human turn → landmark entry, label = turn label', () => {
    const out = deriveOutline([turn({ uuid: 'h1', kind: 'human', label: 'fix the bug' })], undefined);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ uuid: 'h1', type: 'human', label: 'fix the bug', depth: 0, error: false });
  });

  it('assistant with prose + thinking → entry plus a depth-1 child per thinking line', () => {
    const out = deriveOutline([
      turn({ uuid: 'a1', kind: 'assistant', label: 'here is the plan', thinking: ['weighing options', 'picking A'] }),
    ], undefined);
    expect(out.map((e) => [e.type, e.label, e.depth])).toEqual([
      ['assistant', 'here is the plan', 0],
      ['assistant', 'weighing options', 1],
      ['assistant', 'picking A', 1],
    ]);
    // Thinking children carry no error/glyph/tool noise.
    expect(out[1]).toMatchObject({ error: false, plan: false, question: false, toolCount: 0 });
    // entryId = render identity (distinct per entry); uuid = jump anchor (shared
    // by a turn + its thinking children). The parent's entryId is its uuid; each
    // thinking child gets `${uuid}#think${i}` so aria-current/React keys never
    // collide across the turn's entries (Task 3).
    expect(out[0]).toMatchObject({ uuid: 'a1', entryId: 'a1' });
    expect(out[1]).toMatchObject({ uuid: 'a1', entryId: 'a1#think0' });
    expect(out[2]).toMatchObject({ uuid: 'a1', entryId: 'a1#think1' });
    expect(out[1].entryId).not.toBe(out[0].entryId);
    expect(out[2].entryId).not.toBe(out[1].entryId);
  });

  it('every entryId in a derived list is unique across a rich mixed fixture', () => {
    const meta: Record<string, SubagentMeta> = {
      sk1: { kind: 'explore' },
      sk2: { kind: 'general-purpose' },
    };
    const out = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'dispatch', member_uuids: ['h1', 'pm'] }),
      turn({ uuid: 'a1', kind: 'assistant', label: 'plan', thinking: ['t-a', 't-b', 't-c'] }),
      // nested subagent under h1's member 'pm'
      turn({ uuid: 's1', kind: 'assistant', label: 'sub a', subagent_key: 'sk2', parent_uuid: 'pm' }),
      turn({ uuid: 'a2', kind: 'assistant', label: 'reply', thinking: ['t-d'] }),
      // non-nested subagent bucket
      turn({ uuid: 's2', kind: 'assistant', label: 'sub b', subagent_key: 'sk1', parent_uuid: 'unresolved' }),
      turn({ uuid: 'h2', kind: 'human', label: 'next' }),
      turn({ uuid: 'm1', kind: 'meta', label: '/commit', meta_kind: 'command' }),
    ], meta);
    const ids = out.map((e) => e.entryId);
    expect(ids.length).toBeGreaterThan(0);
    expect(new Set(ids).size).toBe(ids.length);
    // Every entry carries a non-empty entryId.
    expect(ids.every((id) => typeof id === 'string' && id.length > 0)).toBe(true);
  });

  it('pure tool-relay assistant (no prose, no thinking, no error, no plan/question) → NO entry', () => {
    const out = deriveOutline([
      turn({ uuid: 'a1', kind: 'assistant', label: '', tools: [{ name: 'Bash', is_error: false }, { name: 'Read', is_error: false }] }),
    ], undefined);
    expect(out).toHaveLength(0);
  });

  it('orphan tool_result with is_error → error entry', () => {
    const out = deriveOutline([
      turn({ uuid: 'tr1', kind: 'tool_result', tools: [{ name: null, is_error: true }] }),
    ], undefined);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ uuid: 'tr1', type: 'error', label: 'tool error', error: true });
  });

  it('prose-less assistant whose tool errored → error entry labeled with the failing tool', () => {
    const out = deriveOutline([
      turn({ uuid: 'a1', kind: 'assistant', label: '', tools: [{ name: 'Bash', is_error: true }] }),
    ], undefined);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ type: 'error', label: 'Bash', error: true });
  });

  it('ExitPlanMode in tools → plan flag on the entry', () => {
    const out = deriveOutline([
      turn({ uuid: 'a1', kind: 'assistant', label: 'proposing', tools: [{ name: 'ExitPlanMode', is_error: false }] }),
    ], undefined);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ type: 'assistant', plan: true, question: false });
  });

  it('AskUserQuestion in tools → question flag on the entry (even with no prose)', () => {
    const out = deriveOutline([
      turn({ uuid: 'a1', kind: 'assistant', label: '', tools: [{ name: 'AskUserQuestion', is_error: false }] }),
    ], undefined);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ type: 'assistant', question: true, plan: false });
  });

  it('meta command → landmark; meta skill/context → skipped', () => {
    const out = deriveOutline([
      turn({ uuid: 'm1', kind: 'meta', label: '/commit', meta_kind: 'command' }),
      turn({ uuid: 'm2', kind: 'meta', label: '', meta_kind: 'skill', skill_name: 'review' }),
      turn({ uuid: 'm3', kind: 'meta', label: 'ctx', meta_kind: 'context' }),
    ], undefined);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ uuid: 'm1', type: 'meta', label: '/commit' });
  });

  it('non-nested subagent → one entry at the bucket root position, label from subagent_meta kind', () => {
    const meta: Record<string, SubagentMeta> = { sk1: { kind: 'explore' } };
    const out = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'go' }),
      // sidechain bucket whose root parent_uuid resolves to NOTHING (no main member) -> non-nested.
      turn({ uuid: 's1', kind: 'assistant', label: 'sub a', subagent_key: 'sk1', parent_uuid: 'unresolved' }),
      turn({ uuid: 's2', kind: 'assistant', label: 'sub b', subagent_key: 'sk1', parent_uuid: 's1' }),
      turn({ uuid: 'h2', kind: 'human', label: 'next' }),
    ], meta);
    // Emitted at s1's document position (between h1 and h2): human, subagent, human.
    expect(out.map((e) => [e.type, e.label, e.depth])).toEqual([
      ['human', 'go', 0],
      ['subagent', 'subagent · explore', 0],
      ['human', 'next', 0],
    ]);
    const sub = out[1];
    expect(sub).toMatchObject({ uuid: 's1', subagentKey: 'sk1', subagentKind: 'explore', turnIndex: 1 });
  });

  it('nested subagent → depth-1 child right after the resolved parent entry', () => {
    const meta: Record<string, SubagentMeta> = { sk1: { kind: 'general-purpose' } };
    const out = deriveOutline([
      // Main human turn whose MEMBER uuid 'pm' the sidechain root nests under.
      turn({ uuid: 'h1', kind: 'human', label: 'dispatch', member_uuids: ['h1', 'pm'] }),
      turn({ uuid: 's1', kind: 'assistant', label: 'sub a', subagent_key: 'sk1', parent_uuid: 'pm' }),
      turn({ uuid: 'h2', kind: 'human', label: 'after' }),
    ], meta);
    expect(out.map((e) => [e.type, e.depth])).toEqual([
      ['human', 0],     // h1
      ['subagent', 1],  // nested under h1
      ['human', 0],     // h2
    ]);
    expect(out[1]).toMatchObject({ uuid: 's1', subagentKey: 'sk1', depth: 1 });
  });

  it('subagent error aggregation → entry gains error when any member tool errored', () => {
    const out = deriveOutline([
      turn({ uuid: 's1', kind: 'assistant', label: 'sub', subagent_key: 'sk1', parent_uuid: 'x', tools: [{ name: 'Read', is_error: false }] }),
      turn({ uuid: 's2', kind: 'assistant', label: 'sub2', subagent_key: 'sk1', parent_uuid: 's1', tools: [{ name: 'Bash', is_error: true }] }),
    ], { sk1: { kind: 'agent' } });
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ type: 'subagent', error: true, toolCount: 2 });
  });

  it('turnIndex tracks skeleton position across mixed turns', () => {
    const out = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'a' }),                                   // index 0
      turn({ uuid: 'a1', kind: 'assistant', label: '', tools: [{ name: 'Bash', is_error: false }] }), // index 1 — skipped
      turn({ uuid: 'a2', kind: 'assistant', label: 'reply' }),                            // index 2
    ], undefined);
    expect(out.map((e) => [e.uuid, e.turnIndex])).toEqual([
      ['h1', 0],
      ['a2', 2],
    ]);
  });
});
