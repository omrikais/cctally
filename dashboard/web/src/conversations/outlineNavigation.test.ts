import { describe, expect, it } from 'vitest';
import { buildOutlineTargets, nextTarget, outlineTurnVisible } from './outlineNavigation';
import type { OutlineTurn } from '../types/conversation';

function turn(over: Partial<OutlineTurn>): OutlineTurn {
  return {
    uuid: 'u',
    kind: 'assistant',
    ts: null,
    label: '',
    member_uuids: ['u'],
    subagent_key: null,
    parent_uuid: null,
    is_sidechain: false,
    ...over,
  };
}

// #184 — the lifted jump-target builder. Both ConversationReader and the
// OutlinePanel JumpCluster now consume this single source of truth.
describe('buildOutlineTargets', () => {
  it('returns empty lists + an empty map for no turns', () => {
    const t = buildOutlineTargets([]);
    expect(t.error).toEqual([]);
    expect(t.prompt).toEqual([]);
    expect(t.subagent).toEqual([]);
    expect(t.plan).toEqual([]);
    expect(t.indexByUuid.size).toBe(0);
  });

  it('collects human-turn indices into `prompt`', () => {
    const t = buildOutlineTargets([
      turn({ uuid: 'a', kind: 'assistant' }),
      turn({ uuid: 'h', kind: 'human' }),
      turn({ uuid: 'h2', kind: 'human' }),
    ]);
    expect(t.prompt).toEqual([1, 2]);
  });

  it('collects is_error tool turns into `error`', () => {
    const t = buildOutlineTargets([
      turn({ uuid: 'a', tools: [{ name: 'Bash', is_error: false }] }),
      turn({ uuid: 'b', tools: [{ name: 'Bash', is_error: true }] }),
    ]);
    expect(t.error).toEqual([1]);
  });

  it('records only the FIRST turn index per distinct subagent_key in `subagent`', () => {
    const t = buildOutlineTargets([
      turn({ uuid: 's1', subagent_key: 'A', is_sidechain: true }),
      turn({ uuid: 's2', subagent_key: 'A', is_sidechain: true }),
      turn({ uuid: 's3', subagent_key: 'B', is_sidechain: true }),
      turn({ uuid: 'm', subagent_key: null }),
    ]);
    expect(t.subagent).toEqual([0, 2]); // first-A at 0, first-B at 2; null ignored
  });

  it('collects plan/question tool turns (ExitPlanMode / AskUserQuestion) into `plan`', () => {
    const t = buildOutlineTargets([
      turn({ uuid: 'a', tools: [{ name: 'Read', is_error: false }] }),
      turn({ uuid: 'b', tools: [{ name: 'ExitPlanMode', is_error: false }] }),
      turn({ uuid: 'c', tools: [{ name: 'AskUserQuestion', is_error: false }] }),
    ]);
    expect(t.plan).toEqual([1, 2]);
  });

  it('maps every turn uuid to its skeleton index', () => {
    const t = buildOutlineTargets([
      turn({ uuid: 'x' }),
      turn({ uuid: 'y' }),
      turn({ uuid: 'z' }),
    ]);
    expect(t.indexByUuid.get('x')).toBe(0);
    expect(t.indexByUuid.get('y')).toBe(1);
    expect(t.indexByUuid.get('z')).toBe(2);
  });

  // cache-failure-markers spec §4 — flagged turns collect into a `cache` list.
  const cf = { tokens_recreated: 130000, prev_cached: 130000, est_wasted_usd: 0.75 };
  it('collects flagged (cache_failure) turn indices into `cache`', () => {
    const t = buildOutlineTargets([
      turn({ uuid: 'a' }),
      turn({ uuid: 'b', cache_failure: cf }),
      turn({ uuid: 'c' }),
      turn({ uuid: 'd', cache_failure: cf }),
    ]);
    expect(t.cache).toEqual([1, 3]);
  });

  it('cache list is empty when no turn is flagged', () => {
    const t = buildOutlineTargets([turn({ uuid: 'a' }), turn({ uuid: 'b' })]);
    expect(t.cache).toEqual([]);
  });
});

describe('outlineTurnVisible', () => {
  it('all mode: every turn is visible', () => {
    expect(outlineTurnVisible(turn({ kind: 'human' }), 'all')).toBe(true);
    expect(outlineTurnVisible(turn({ kind: 'meta' }), 'all')).toBe(true);
    expect(outlineTurnVisible(turn({ kind: 'tool_result' }), 'all')).toBe(true);
    expect(outlineTurnVisible(turn({ is_sidechain: true }), 'all')).toBe(true);
  });

  describe('prompts mode', () => {
    it('keeps human turns only', () => {
      expect(outlineTurnVisible(turn({ kind: 'human', label: 'hi' }), 'prompts')).toBe(true);
    });
    it('hides assistant / tool_result / meta turns', () => {
      expect(outlineTurnVisible(turn({ kind: 'assistant', label: 'prose' }), 'prompts')).toBe(false);
      expect(outlineTurnVisible(turn({ kind: 'tool_result' }), 'prompts')).toBe(false);
      expect(outlineTurnVisible(turn({ kind: 'meta', meta_kind: 'command' }), 'prompts')).toBe(false);
    });
  });

  describe('errors mode', () => {
    it('keeps any turn with an is_error tool result', () => {
      const t = turn({ kind: 'assistant', tools: [{ name: 'Bash', is_error: true }] });
      expect(outlineTurnVisible(t, 'errors')).toBe(true);
    });
    it('keeps an orphan tool_result error turn (name-less tool ref)', () => {
      const t = turn({ kind: 'tool_result', tools: [{ name: null, is_error: true }] });
      expect(outlineTurnVisible(t, 'errors')).toBe(true);
    });
    it('hides turns with no error', () => {
      expect(outlineTurnVisible(turn({ kind: 'human', label: 'hi' }), 'errors')).toBe(false);
      const t = turn({ kind: 'assistant', tools: [{ name: 'Read', is_error: false }] });
      expect(outlineTurnVisible(t, 'errors')).toBe(false);
    });
  });

  describe('chat mode', () => {
    it('keeps human turns', () => {
      expect(outlineTurnVisible(turn({ kind: 'human', label: 'hi' }), 'chat')).toBe(true);
    });
    it('keeps assistant turns with prose', () => {
      expect(outlineTurnVisible(turn({ kind: 'assistant', label: 'prose' }), 'chat')).toBe(true);
    });
    it('keeps assistant turns with thinking but no prose', () => {
      const t = turn({ kind: 'assistant', label: '', thinking: ['hmm'] });
      expect(outlineTurnVisible(t, 'chat')).toBe(true);
    });
    it('hides a pure-tool assistant turn (no prose, no thinking)', () => {
      const t = turn({ kind: 'assistant', label: '', tools: [{ name: 'Bash', is_error: false }] });
      expect(outlineTurnVisible(t, 'chat')).toBe(false);
    });
    it('hides orphan tool_result and meta turns', () => {
      expect(outlineTurnVisible(turn({ kind: 'tool_result' }), 'chat')).toBe(false);
      expect(outlineTurnVisible(turn({ kind: 'meta', meta_kind: 'command' }), 'chat')).toBe(false);
    });
  });

  describe('sidechain turns', () => {
    it('are visible only in errors mode AND only with an error', () => {
      const errSide = turn({ is_sidechain: true, subagent_key: 'k', tools: [{ name: 'Bash', is_error: true }] });
      const okSide = turn({ is_sidechain: true, subagent_key: 'k', label: 'prose' });
      expect(outlineTurnVisible(errSide, 'errors')).toBe(true);
      expect(outlineTurnVisible(okSide, 'errors')).toBe(false);
      // suppressed in every non-error mode regardless of content
      expect(outlineTurnVisible(errSide, 'chat')).toBe(false);
      expect(outlineTurnVisible(errSide, 'prompts')).toBe(false);
      expect(outlineTurnVisible(okSide, 'chat')).toBe(false);
    });
  });
});

describe('nextTarget — forward (dir=1)', () => {
  const idx = [2, 5, 9];
  it('finds the first index strictly greater than the cursor', () => {
    expect(nextTarget(idx, 2, 1)).toBe(5);
    expect(nextTarget(idx, 4, 1)).toBe(5);
    expect(nextTarget(idx, 5, 1)).toBe(9);
  });
  it('a cursor of -1 (before the start) finds the first target', () => {
    expect(nextTarget(idx, -1, 1)).toBe(2);
  });
  it('returns null at/after the last target (no wrap)', () => {
    expect(nextTarget(idx, 9, 1)).toBeNull();
    expect(nextTarget(idx, 12, 1)).toBeNull();
  });
});

describe('nextTarget — backward (dir=-1)', () => {
  const idx = [2, 5, 9];
  it('finds the first index strictly less than the cursor', () => {
    expect(nextTarget(idx, 9, -1)).toBe(5);
    expect(nextTarget(idx, 6, -1)).toBe(5);
    expect(nextTarget(idx, 5, -1)).toBe(2);
  });
  it('returns null at/before the first target (no wrap)', () => {
    expect(nextTarget(idx, 2, -1)).toBeNull();
    expect(nextTarget(idx, -1, -1)).toBeNull();
  });
});

describe('nextTarget — edge cases', () => {
  it('empty list yields null in both directions', () => {
    expect(nextTarget([], 0, 1)).toBeNull();
    expect(nextTarget([], 0, -1)).toBeNull();
  });
  it('cursor not in the list still finds neighbors', () => {
    expect(nextTarget([1, 4, 8], 3, 1)).toBe(4);
    expect(nextTarget([1, 4, 8], 3, -1)).toBe(1);
  });
});
