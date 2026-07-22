import { describe, expect, it } from 'vitest';
import { buildOutlineTargets, nextTarget, outlineTurnVisible, resolveTurnIndex } from './outlineNavigation';
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

  // #217 S3 F8 — compaction landmark jump family. A meta_kind:'compaction' turn
  // collects into `compaction`, navigable like the other landmark lists.
  it('collects meta_kind:compaction turn indices into `compaction`', () => {
    const t = buildOutlineTargets([
      turn({ uuid: 'h', kind: 'human' }),
      turn({ uuid: 'cx1', kind: 'meta', meta_kind: 'compaction' }),
      turn({ uuid: 'a', kind: 'assistant' }),
      turn({ uuid: 'cx2', kind: 'meta', meta_kind: 'compaction' }),
    ]);
    expect(t.compaction).toEqual([1, 3]);
  });

  it('a non-compaction meta turn does NOT collect into `compaction`', () => {
    const t = buildOutlineTargets([
      turn({ uuid: 'h', kind: 'human' }),
      turn({ uuid: 'm', kind: 'meta', meta_kind: 'command' }),
    ]);
    expect(t.compaction).toEqual([]);
  });
});

// #217 S3 E2 (Codex P1) — `loadToTarget` must resolve a deep-link / search uuid
// to its OWNING outline turn before deciding a nearest-edge direction, because
// the target can be a FOLDED FRAGMENT's uuid (present in a turn's member_uuids,
// not its own `uuid`). `buildOutlineTargets` therefore also builds a member-uuid
// → owning-turn-index map; `resolveTurnIndex` checks the own-uuid map first then
// the member map.
describe('resolveTurnIndex — member (folded-fragment) uuid resolution (#217 S3 E2)', () => {
  it('resolves a member (folded-fragment) uuid to its owning turn index', () => {
    const turns = [
      { uuid: 't0', kind: 'human', member_uuids: ['t0'] },
      { uuid: 't1', kind: 'assistant', member_uuids: ['t1', 'fragA', 'fragB'] },
    ] as unknown as OutlineTurn[];
    const t = buildOutlineTargets(turns);
    // member uuid → owning turn index
    expect(resolveTurnIndex(t, 'fragB')).toBe(1);
    expect(resolveTurnIndex(t, 'fragA')).toBe(1);
    // own uuid still resolves (indexByUuid wins).
    expect(resolveTurnIndex(t, 't0')).toBe(0);
    expect(resolveTurnIndex(t, 't1')).toBe(1);
    // an unknown uuid resolves to undefined (graceful no-op jump).
    expect(resolveTurnIndex(t, 'missing')).toBeUndefined();
  });

  it('own uuid takes precedence over a member-map collision', () => {
    // A pathological transcript where turn 1 lists turn 0's uuid as a member.
    // resolveTurnIndex must prefer the OWN-uuid map (index 0), not the member map.
    const turns = [
      turn({ uuid: 'shared', kind: 'human', member_uuids: ['shared'] }),
      turn({ uuid: 't1', kind: 'assistant', member_uuids: ['t1', 'shared'] }),
    ];
    const t = buildOutlineTargets(turns);
    expect(resolveTurnIndex(t, 'shared')).toBe(0);
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

  // #217 S5 E4 — the twin must mirror nodeVisible for the three More modes, else
  // an outline/files jump lands behind the active filter (Codex P1-5).
  describe('edits mode (twin)', () => {
    it('shows turns carrying an Edit/MultiEdit/Write tool', () => {
      expect(outlineTurnVisible(turn({ tools: [{ name: 'Edit', is_error: false }] }), 'edits')).toBe(true);
      expect(outlineTurnVisible(turn({ tools: [{ name: 'MultiEdit', is_error: false }] }), 'edits')).toBe(true);
      expect(outlineTurnVisible(turn({ tools: [{ name: 'Write', is_error: false }] }), 'edits')).toBe(true);
      expect(outlineTurnVisible(turn({ tools: [{ name: 'apply_patch', is_error: false }] }), 'edits')).toBe(true);
      expect(outlineTurnVisible(turn({ tools: [{ name: 'patch_apply_end', is_error: true }] }), 'edits')).toBe(true);
    });
    it('hides Bash / Read / prose-only turns', () => {
      expect(outlineTurnVisible(turn({ tools: [{ name: 'Bash', is_error: false }] }), 'edits')).toBe(false);
      expect(outlineTurnVisible(turn({ tools: [{ name: 'Read', is_error: false }] }), 'edits')).toBe(false);
      expect(outlineTurnVisible(turn({ kind: 'human', label: 'hi' }), 'edits')).toBe(false);
    });
  });

  describe('bash mode (twin)', () => {
    it('shows Bash turns and hides edit turns', () => {
      expect(outlineTurnVisible(turn({ tools: [{ name: 'Bash', is_error: false }] }), 'bash')).toBe(true);
      expect(outlineTurnVisible(turn({ tools: [{ name: 'exec', is_error: false }] }), 'bash')).toBe(true);
      expect(outlineTurnVisible(turn({ tools: [{ name: 'Edit', is_error: false }] }), 'bash')).toBe(false);
      expect(outlineTurnVisible(turn({ kind: 'human', label: 'hi' }), 'bash')).toBe(false);
    });
  });

  describe('subagent:<key> mode (twin)', () => {
    it('shows only turns whose subagent_key matches', () => {
      expect(outlineTurnVisible(turn({ subagent_key: 'k1', is_sidechain: true }), 'subagent:k1')).toBe(true);
      expect(outlineTurnVisible(turn({ subagent_key: 'k2', is_sidechain: true }), 'subagent:k1')).toBe(false);
      // a main-thread turn (subagent_key null) never matches a subagent filter.
      expect(outlineTurnVisible(turn({ subagent_key: null }), 'subagent:k1')).toBe(false);
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

// #217 S6 F4 — buildOutlineTargets threads a client-only bookmark list (the
// bookmarked turn indices in document order) so the cluster + i/I keys can
// navigate it; OutlineTurn has no bookmark field, so the bookmarks are passed in
// explicitly (not derived from the server skeleton).
describe('buildOutlineTargets bookmark list (#217 S6 F4)', () => {
  it('builds a bookmark target list from the bookmarks param', () => {
    const turns = [
      turn({ uuid: 'a', kind: 'human' }),
      turn({ uuid: 'b', kind: 'assistant' }),
      turn({ uuid: 'c', kind: 'assistant' }),
    ];
    const t = buildOutlineTargets(turns, { c: { note: '', ts: 1 }, a: { note: '', ts: 2 } });
    expect(t.bookmark).toEqual([0, 2]); // indices of a and c, in document order
  });
  it('defaults bookmark to [] when no bookmarks param is passed', () => {
    const turns = [turn({ uuid: 'a', kind: 'human' })];
    expect(buildOutlineTargets(turns).bookmark).toEqual([]);
  });
});
