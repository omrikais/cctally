import { describe, expect, it } from 'vitest';
import { buildOutlineTargets, nextTarget, nextTargetEntry, outlineTurnVisible, resolveTurnIndex } from './outlineNavigation';
import type { OutlineLandmark, OutlineTurn } from '../types/conversation';

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
    // #463 S4 §6.3 — a target names the anchor it loads AND the tier-1 turn
    // that owns it, so a tier-2 landmark can jump to a segment while the three
    // consumers that need a whole `OutlineTurn` still resolve one.
    expect(t.prompt).toEqual([
      { anchorKey: 'h', ownerTurnIndex: 1 },
      { anchorKey: 'h2', ownerTurnIndex: 2 },
    ]);
  });

  it('collects is_error tool turns into `error`', () => {
    const t = buildOutlineTargets([
      turn({ uuid: 'a', tools: [{ name: 'Bash', is_error: false }] }),
      turn({ uuid: 'b', tools: [{ name: 'Bash', is_error: true }] }),
    ]);
    expect(t.error).toEqual([{ anchorKey: 'b', ownerTurnIndex: 1 }]);
  });

  it('records only the FIRST turn index per distinct subagent_key in `subagent`', () => {
    const t = buildOutlineTargets([
      turn({ uuid: 's1', subagent_key: 'A', is_sidechain: true }),
      turn({ uuid: 's2', subagent_key: 'A', is_sidechain: true }),
      turn({ uuid: 's3', subagent_key: 'B', is_sidechain: true }),
      turn({ uuid: 'm', subagent_key: null }),
    ]);
    // first-A at 0, first-B at 2; null ignored
    expect(t.subagent).toEqual([
      { anchorKey: 's1', ownerTurnIndex: 0 },
      { anchorKey: 's3', ownerTurnIndex: 2 },
    ]);
  });

  it('collects plan/question tool turns (ExitPlanMode / AskUserQuestion) into `plan`', () => {
    const t = buildOutlineTargets([
      turn({ uuid: 'a', tools: [{ name: 'Read', is_error: false }] }),
      turn({ uuid: 'b', tools: [{ name: 'ExitPlanMode', is_error: false }] }),
      turn({ uuid: 'c', tools: [{ name: 'AskUserQuestion', is_error: false }] }),
    ]);
    expect(t.plan).toEqual([
      { anchorKey: 'b', ownerTurnIndex: 1 },
      { anchorKey: 'c', ownerTurnIndex: 2 },
    ]);
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
    expect(t.cache).toEqual([
      { anchorKey: 'b', ownerTurnIndex: 1 },
      { anchorKey: 'd', ownerTurnIndex: 3 },
    ]);
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
    expect(t.compaction).toEqual([
      { anchorKey: 'cx1', ownerTurnIndex: 1 },
      { anchorKey: 'cx2', ownerTurnIndex: 3 },
    ]);
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
    // a and c, in document order
    expect(t.bookmark).toEqual([
      { anchorKey: 'a', ownerTurnIndex: 0 },
      { anchorKey: 'c', ownerTurnIndex: 2 },
    ]);
  });
  it('defaults bookmark to [] when no bookmarks param is passed', () => {
    const turns = [turn({ uuid: 'a', kind: 'human' })];
    expect(buildOutlineTargets(turns).bookmark).toEqual([]);
  });
});

// #463 S1 — a turn that segmentation split is still ONE outline entry, and only
// its segment 0 key appears as that entry's own uuid. A deep link, find hit or
// saved reading position naming segment 1..N therefore resolves through neither
// the own-uuid map nor the member map, and `loadToTarget` would no-op: the drain
// would never run and the jump would land nowhere. `segment_uuids` is the third
// channel, deliberately separate from `member_uuids`, which the reader must NOT
// treat as "already loaded".
describe('resolveTurnIndex — segment key resolution (#463 S1)', () => {
  const turns = [
    { uuid: 't0', kind: 'human', member_uuids: ['t0'] },
    {
      uuid: 't1', kind: 'assistant', member_uuids: ['t1', 'fragA'],
      segment_uuids: ['t1', 'seg1', 'seg2'],
    },
    { uuid: 't2', kind: 'assistant', member_uuids: ['t2'] },
  ] as unknown as OutlineTurn[];

  it('resolves a segment key past the first to its owning turn index', () => {
    const t = buildOutlineTargets(turns);
    expect(resolveTurnIndex(t, 'seg1')).toBe(1);
    expect(resolveTurnIndex(t, 'seg2')).toBe(1);
  });

  it('keeps segment keys out of the member map', () => {
    const t = buildOutlineTargets(turns);
    expect(t.memberIndex.has('seg1')).toBe(false);
    expect(t.memberIndex.has('seg2')).toBe(false);
    expect(t.segmentIndex.get('seg2')).toBe(1);
  });

  it('lets the own-uuid and member maps still win over the segment map', () => {
    const t = buildOutlineTargets([
      ...turns,
      // A pathological turn claiming another turn's own uuid as one of its
      // segments must not shadow the real owner.
      { uuid: 't3', kind: 'assistant', member_uuids: ['t3'], segment_uuids: ['t3', 't0', 'fragA'] },
    ] as unknown as OutlineTurn[]);
    expect(resolveTurnIndex(t, 't0')).toBe(0);
    expect(resolveTurnIndex(t, 'fragA')).toBe(1);
  });

  it('is inert for an outline with no segment keys', () => {
    const t = buildOutlineTargets([{ uuid: 'a', kind: 'human', member_uuids: ['a'] }] as unknown as OutlineTurn[]);
    expect(t.segmentIndex.size).toBe(0);
    expect(resolveTurnIndex(t, 'nope')).toBeUndefined();
  });
});

// #463 S4 §6.3 — the target representation. `buildOutlineTargets` returned
// numeric indices into the array it was given, and three sites dereferenced
// them against tier-1 `turns`: JumpCluster's `jumpToIndex` and two sites in
// ConversationReader. Indexing a merged list would have broken all three, so a
// target now carries the anchor it loads AND the tier-1 turn that owns it.
describe('#463 S4 — landmark-aware jump targets', () => {
  const landmark = (over: Partial<OutlineLandmark> & { landmark_key: string }): OutlineLandmark => ({
    block_key: 'cbk1.b', uuid: 'seg', parent_uuid: 'a1', kind: 'tool_error',
    label: 'exec', ts: null, ...over,
  });
  const turns = () => [
    turn({ uuid: 'h1', kind: 'human' }),
    turn({ uuid: 'a1', kind: 'assistant', tools: [{ name: 'exec', is_error: true }] }),
    turn({ uuid: 'h2', kind: 'human' }),
  ];

  it('carries the anchor and its owning turn index', () => {
    const t = buildOutlineTargets(turns());
    expect(t.prompt).toEqual([
      { anchorKey: 'h1', ownerTurnIndex: 0 },
      { anchorKey: 'h2', ownerTurnIndex: 2 },
    ]);
  });

  it('anchors the error family on the landmark segment, not the turn', () => {
    // Non-vacuity: without landmarks the family still targets the turn key.
    expect(buildOutlineTargets(turns()).error)
      .toEqual([{ anchorKey: 'a1', ownerTurnIndex: 1 }]);
    const t = buildOutlineTargets(turns(), undefined, [
      landmark({ landmark_key: 'e#tool_error', uuid: 'seg-15' }),
      landmark({ landmark_key: 'p#plan', uuid: 'seg-3', kind: 'plan', label: 'update_plan' }),
    ]);
    expect(t.error).toEqual([
      { anchorKey: 'seg-15', ownerTurnIndex: 1, innerAnchorKey: 'cbk1.b' },
    ]);
    // `PLAN_QUESTION_TOOLS` holds only Claude's ExitPlanMode and
    // AskUserQuestion, so without the landmark the Codex plan family is empty.
    expect(t.plan).toEqual([
      { anchorKey: 'seg-3', ownerTurnIndex: 1, innerAnchorKey: 'cbk1.b' },
    ]);
  });

  it('resolves a landmark jump to its owning turn via parent_uuid', () => {
    const t = buildOutlineTargets(turns(), undefined, [
      landmark({ landmark_key: 'e#tool_error', uuid: 'seg-15' }),
    ]);
    // `segmentIndex` is built from SURVIVING turns' `segment_uuids` and is EMPTY
    // on real Codex data — the fixture below has none — so consulting it would
    // return undefined and the focus-mode visibility test would be skipped
    // exactly as it is today: a production no-op. The owner comes from the
    // landmark's own `parent_uuid`, which the server already sends.
    expect(t.segmentIndex.size).toBe(0);
    expect(t.error[0].ownerTurnIndex).toBe(1);
    expect(turns()[t.error[0].ownerTurnIndex].uuid).toBe('a1');
  });

  // #463 S4 F-A — the gate measured a jump landing the right SEGMENT at the top
  // of a 635px viewport with the failure it named 1,984-6,574px below the fold,
  // because one segment can be 4,098px tall. The target therefore carries a
  // second, finer key naming the element inside that item.
  it('carries the inner anchor each landmark family addresses', () => {
    const t = buildOutlineTargets(turns(), undefined, [
      landmark({ landmark_key: 'cbk1.e#tool_error', block_key: 'cbk1.e', uuid: 'seg-15' }),
      landmark({ landmark_key: 'cbk1.p#plan', block_key: 'cbk1.p', uuid: 'seg-3',
                 kind: 'plan', label: 'update_plan' }),
    ]);
    expect(t.error[0].innerAnchorKey).toBe('cbk1.e');
    expect(t.plan[0].innerAnchorKey).toBe('cbk1.p');
  });

  it('leaves a tier-1 target with no inner anchor', () => {
    const t = buildOutlineTargets(turns());
    expect(t.prompt[0].innerAnchorKey).toBeUndefined();
    expect(t.error[0].innerAnchorKey).toBeUndefined();
  });

  it('steps through landmark targets with the shared cursor math', () => {
    const t = buildOutlineTargets(turns(), undefined, [
      landmark({ landmark_key: 'e#tool_error', uuid: 'seg-15' }),
    ]);
    expect(nextTargetEntry(t.error, -1, 1))
      .toEqual({ anchorKey: 'seg-15', ownerTurnIndex: 1, innerAnchorKey: 'cbk1.b' });
    expect(nextTargetEntry(t.error, 1, 1)).toBeNull();
    expect(nextTargetEntry(t.error, 2, -1))
      .toEqual({ anchorKey: 'seg-15', ownerTurnIndex: 1, innerAnchorKey: 'cbk1.b' });
  });
});
