import { describe, expect, it } from 'vitest';
import { adaptQualifiedOutline } from './conversationAdapters';
import { buildOutlineTargets } from '../conversations/outlineNavigation';
import { deriveOutline } from '../conversations/deriveOutline';
import type { ConversationRef } from '../types/conversation';

// #463 S4 §7.2 / D7 — the jump-family property test.
//
// It exists because §1.5 records why all four of this session's findings
// survived three sessions: every component here is unit-tested, but every one of
// those tests builds its `OutlineTurn` fixtures BY HAND with `tools`,
// `subagent_key` and `cache_failure` already populated. No test anywhere ran a
// real server envelope through `adaptQualifiedOutline` before asserting, so the
// suite proved the components work when given data and never asked whether the
// adapter gives them any. This asks that question, over the whole jump-family
// vocabulary at once.
//
// DISJOINT from the outline golden by design (D7): the golden catches a value
// that drifted, this catches a family somebody adds and never wires up.
//
// ITS LIMIT, recorded rather than oversold: it catches an OMISSION, not a wrong
// value. A family that is populated with the wrong anchor passes here and is
// caught by the golden instead.

const ref: ConversationRef = { source: 'codex', key: 'v1.family-coverage' };

// One Codex envelope carrying a source for every family the chrome navigates.
// Shaped like the server's: landmarks anchor on SEGMENT keys and parent to a
// turn the navigation filter would otherwise drop.
type OutlineWire = Parameters<typeof adaptQualifiedOutline>[1];
const envelope = (): OutlineWire => ({
  status: 'ok',
  conversation_key: ref.key,
  turns: [
    {
      item_key: 'civ1.prompt', label: 'fix the build', timestamp_utc: null,
      kinds: { user: 1 }, segment_item_keys: ['civ1.prompt'],
    },
    {
      item_key: 'civ1.reply', label: 'Long reply', timestamp_utc: '2026-08-04T09:00:00Z',
      kinds: { event: 22, assistant: 15, tool_call: 142 },
      segment_item_keys: ['civ1.reply', 'civ1.reply.s1', 'civ1.reply.s2'],
      tools: [{ name: 'exec', is_error: true }, { name: 'update_plan', is_error: false }],
      tool_call_count: 142,
      first_failure_name: 'exec',
      thinking: ['Read the failing case'],
      model: 'gpt-synthetic-codex',
    },
    {
      item_key: 'civ1.compact', label: 'context_compacted', timestamp_utc: null,
      kinds: { event: 1 }, segment_item_keys: ['civ1.compact'],
    },
  ],
  landmarks: [
    {
      landmark_key: 'cbk1.r#0', block_key: 'cbk1.r', item_key: 'civ1.reply',
      parent_item_key: 'civ1.reply', kind: 'reasoning',
      label: 'Read the failing case', timestamp_utc: null,
    },
    {
      landmark_key: 'cbk1.e#tool_error', block_key: 'cbk1.e', item_key: 'civ1.reply.s2',
      parent_item_key: 'civ1.reply', kind: 'tool_error', label: 'exec',
      timestamp_utc: null,
    },
    {
      landmark_key: 'cbk1.p#plan', block_key: 'cbk1.p', item_key: 'civ1.reply.s1',
      parent_item_key: 'civ1.reply', kind: 'plan', label: 'update_plan',
      timestamp_utc: null,
    },
  ],
  stats: {
    items: 3, kinds: { user: 1, assistant: 15, event: 23 },
    tool_counts: { exec: 2, update_plan: 1 }, error_count: 1,
    models: { 'gpt-synthetic-codex': 1 }, duration_seconds: 181,
  },
  files: [{
    file_path: 'bin/a.py', tool: 'apply_patch', count: 1, added: 5, removed: 1,
    touches: [{ item_key: 'civ1.reply.s1', timestamp_utc: null, op: 'update' }],
  }],
  children: [],
});

const adapt = () => adaptQualifiedOutline(ref, envelope(), {}, new Set(['civ1.prompt']));

describe('#463 S4 §7.2 — every jump family the adapter is meant to fill, is filled', () => {
  it('populates the target list of every family this envelope carries a source for', () => {
    const outline = adapt();
    const targets = buildOutlineTargets(outline.turns, undefined, outline.landmarks);
    for (const family of ['error', 'prompt', 'plan', 'compaction'] as const) {
      expect(targets[family].length, `the ${family} jump family is empty`).toBeGreaterThan(0);
    }
    // Every anchor a jump would load is a real key in this envelope, so no
    // family navigates to somewhere that does not exist.
    const addressable = new Set([
      ...outline.turns.flatMap((turn) => [turn.uuid, ...(turn.segment_uuids ?? [])]),
      ...(outline.landmarks ?? []).map((landmark) => landmark.uuid),
    ]);
    for (const family of ['error', 'prompt', 'plan', 'compaction'] as const) {
      for (const target of targets[family]) {
        expect(addressable).toContain(target.anchorKey);
        expect(outline.turns[target.ownerTurnIndex]).toBeDefined();
      }
    }
  });

  it('renders a rail row for every family it navigates', () => {
    const outline = adapt();
    const { entries } = deriveOutline(
      outline.turns, outline.subagent_meta, true, outline.task_completion,
      undefined, outline.landmarks);
    const types = new Set(entries.map((entry) => entry.type));
    for (const type of ['human', 'error', 'plan', 'heading', 'compaction'] as const) {
      expect([...types], `no rail row of type ${type}`).toContain(type);
    }
  });

  it('populates every tier-1 field the families and the stats card read', () => {
    const outline = adapt();
    const reply = outline.turns.find((turn) => turn.uuid === 'civ1.reply')!;
    expect(reply.tools?.length).toBeGreaterThan(0);
    expect(reply.tool_call_count).toBeGreaterThan(0);
    expect(reply.first_failure_name).toBeTruthy();
    expect(reply.thinking?.length).toBeGreaterThan(0);
    expect(reply.model).toBeTruthy();
    expect(outline.stats.models).not.toEqual({});
    expect(outline.stats.tool_counts).not.toEqual({});
    expect(outline.stats.duration_seconds).not.toBeNull();
    expect(outline.files?.length).toBeGreaterThan(0);
    expect(outline.files?.[0].touches.length).toBeGreaterThan(0);
  });

  it('leaves subagent and cache empty BECAUSE they are structurally absent', () => {
    // The two families that stay empty are decisions, not omissions, and they
    // are asserted here so a later reader does not mistake one for the other.
    // §6.2: Codex nests through separate CHILD CONVERSATIONS, so there is no
    // `subagent_key` to publish and fabricating a grouping that does not exist
    // would be worse than an empty family; `cache_failure` is a Claude concept.
    const outline = adapt();
    const targets = buildOutlineTargets(outline.turns, undefined, outline.landmarks);
    expect(targets.subagent).toEqual([]);
    expect(targets.cache).toEqual([]);
    expect(outline.turns.every((turn) => turn.subagent_key === null)).toBe(true);
    expect(outline.turns.every((turn) => turn.cache_failure === undefined)).toBe(true);
  });
});
