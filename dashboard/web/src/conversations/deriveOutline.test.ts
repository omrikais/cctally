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

// #186 §3 — deriveOutline is a SECTION WALK (direction C: prompt spine +
// curated landmarks). Each human prompt opens a depth-0 section; only curated
// assistant landmarks (error / plan / question / Markdown-heading-led /
// subagent) emit under it at depth 1; generic prose drops out; thinking
// collapses to a per-prompt `thinkingCount` badge. The return shape is
// `{ entries, sectionByUuid }`.
describe('deriveOutline (#186 §3 section walk)', () => {
  it('returns { entries, sectionByUuid }', () => {
    const out = deriveOutline([turn({ uuid: 'h1', kind: 'human', label: 'go' })], undefined);
    expect(Array.isArray(out.entries)).toBe(true);
    expect(out.sectionByUuid instanceof Map).toBe(true);
  });

  it('a human prompt becomes a depth-0 entry; thinkingCount defaults to 0', () => {
    const { entries } = deriveOutline([turn({ uuid: 'h1', kind: 'human', label: 'fix the bug' })], undefined);
    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({
      uuid: 'h1', type: 'human', label: 'fix the bug', depth: 0, error: false, thinkingCount: 0,
    });
  });

  it('a generic assistant turn after a prompt produces NO entry', () => {
    const { entries } = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'do it' }),
      turn({ uuid: 'a1', kind: 'assistant', label: 'on it', tools: [{ name: 'Bash', is_error: false }] }),
    ], undefined);
    expect(entries.map((e) => [e.uuid, e.type])).toEqual([['h1', 'human']]);
  });

  it('ExitPlanMode → a depth-1 plan landmark under the section prompt', () => {
    const { entries } = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'plan it' }),
      turn({ uuid: 'a1', kind: 'assistant', label: 'proposing', tools: [{ name: 'ExitPlanMode', is_error: false }] }),
    ], undefined);
    expect(entries.map((e) => [e.uuid, e.type, e.depth, e.plan])).toEqual([
      ['h1', 'human', 0, false],
      ['a1', 'plan', 1, true],
    ]);
  });

  it('AskUserQuestion → a depth-1 question landmark', () => {
    const { entries } = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'ask me' }),
      turn({ uuid: 'a1', kind: 'assistant', label: '', tools: [{ name: 'AskUserQuestion', is_error: false }] }),
    ], undefined);
    expect(entries[1]).toMatchObject({ uuid: 'a1', type: 'question', depth: 1, question: true });
  });

  it('a Markdown-heading-led assistant turn → a depth-1 heading landmark, label = the heading verbatim', () => {
    const { entries } = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'structure it' }),
      turn({ uuid: 'a1', kind: 'assistant', label: '## Section 1 — Backend' }),
    ], undefined);
    expect(entries[1]).toMatchObject({ uuid: 'a1', type: 'heading', depth: 1, label: '## Section 1 — Backend' });
  });

  it('a non-heading prose turn is NOT a heading landmark (regex needs # + space + non-space)', () => {
    const { entries } = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'go' }),
      turn({ uuid: 'a1', kind: 'assistant', label: '#nospace is not a heading' }),
      turn({ uuid: 'a2', kind: 'assistant', label: 'plain prose' }),
    ], undefined);
    // Only the prompt; neither assistant qualifies.
    expect(entries.map((e) => e.uuid)).toEqual(['h1']);
  });

  it('an assistant error turn → a depth-1 error landmark, error:true, label "tool error · <tool>"', () => {
    const { entries } = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'run it' }),
      turn({ uuid: 'a1', kind: 'assistant', label: '', tools: [{ name: 'Bash', is_error: true }] }),
    ], undefined);
    expect(entries[1]).toMatchObject({ uuid: 'a1', type: 'error', depth: 1, error: true, label: 'tool error · Bash' });
  });

  it('an orphan tool_result error → a depth-1 error landmark labeled "tool error"', () => {
    const { entries } = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'go' }),
      turn({ uuid: 'tr1', kind: 'tool_result', tools: [{ name: null, is_error: true }] }),
    ], undefined);
    expect(entries[1]).toMatchObject({ uuid: 'tr1', type: 'error', depth: 1, error: true, label: 'tool error' });
  });

  it('type precedence: an errored heading-led turn keeps its heading label but takes error:true (error > heading)', () => {
    const { entries } = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'go' }),
      turn({ uuid: 'a1', kind: 'assistant', label: '## Broken section', tools: [{ name: 'Bash', is_error: true }] }),
    ], undefined);
    // error wins the type/flag, label stays the heading.
    expect(entries[1]).toMatchObject({ uuid: 'a1', type: 'error', error: true, label: '## Broken section' });
  });

  it('two thinking blocks across the section → prompt thinkingCount === 2, NO depth-1 thinking rows', () => {
    const { entries } = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'think hard' }),
      turn({ uuid: 'a1', kind: 'assistant', label: 'first', thinking: ['weighing'] }),
      turn({ uuid: 'a2', kind: 'assistant', label: 'second', thinking: ['picking'] }),
    ], undefined);
    // Only the prompt entry; both generic assistants drop out (no heading/error).
    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({ uuid: 'h1', thinkingCount: 2 });
    expect(entries.some((e) => e.label === 'weighing' || e.label === 'picking')).toBe(false);
  });

  it('thinking accrues even from heading/error turns that DO emit a landmark', () => {
    const { entries } = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'go' }),
      turn({ uuid: 'a1', kind: 'assistant', label: '## A heading', thinking: ['t1', 't2'] }),
    ], undefined);
    expect(entries[0]).toMatchObject({ uuid: 'h1', thinkingCount: 2 });
    expect(entries[1]).toMatchObject({ uuid: 'a1', type: 'heading' });
  });

  it('a meta_kind:command turn produces NO entry', () => {
    const { entries } = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'go' }),
      turn({ uuid: 'm1', kind: 'meta', label: '/commit', meta_kind: 'command' }),
      turn({ uuid: 'h2', kind: 'human', label: 'next' }),
    ], undefined);
    expect(entries.map((e) => e.uuid)).toEqual(['h1', 'h2']);
  });

  it('sectionByUuid maps a generic assistant turn member uuid → the section prompt uuid', () => {
    const { sectionByUuid } = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'prompt' }),
      turn({ uuid: 'a1', kind: 'assistant', label: 'generic reply', member_uuids: ['a1', 'a1b'] }),
    ], undefined);
    expect(sectionByUuid.get('a1')).toBe('h1');
    expect(sectionByUuid.get('a1b')).toBe('h1'); // every member uuid, not just the anchor
    expect(sectionByUuid.get('h1')).toBe('h1');  // the prompt maps to itself
  });

  it('sectionByUuid restarts at each new prompt', () => {
    const { sectionByUuid } = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'one' }),
      turn({ uuid: 'a1', kind: 'assistant', label: 'r1' }),
      turn({ uuid: 'h2', kind: 'human', label: 'two' }),
      turn({ uuid: 'a2', kind: 'assistant', label: 'r2' }),
    ], undefined);
    expect(sectionByUuid.get('a1')).toBe('h1');
    expect(sectionByUuid.get('a2')).toBe('h2');
    expect(sectionByUuid.get('h2')).toBe('h2');
  });

  it('a subagent bucket → exactly one subagent landmark at the bucket position; all member uuids in sectionByUuid', () => {
    const meta: Record<string, SubagentMeta> = { sk1: { kind: 'explore' } };
    const { entries, sectionByUuid } = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'dispatch' }),
      turn({ uuid: 's1', kind: 'assistant', label: 'sub a', subagent_key: 'sk1', parent_uuid: 'unresolved', member_uuids: ['s1', 's1b'] }),
      turn({ uuid: 's2', kind: 'assistant', label: 'sub b', subagent_key: 'sk1', parent_uuid: 's1', member_uuids: ['s2'] }),
    ], meta);
    const subs = entries.filter((e) => e.type === 'subagent');
    expect(subs).toHaveLength(1);
    expect(subs[0]).toMatchObject({ subagentKey: 'sk1', subagentKind: 'explore', depth: 1, label: 'subagent · explore' });
    // Every bucket member uuid maps to the enclosing section prompt.
    expect(sectionByUuid.get('s1')).toBe('h1');
    expect(sectionByUuid.get('s1b')).toBe('h1');
    expect(sectionByUuid.get('s2')).toBe('h1');
  });

  it('a subagent bucket whose error any member carried → the landmark gets error:true', () => {
    const { entries } = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'go' }),
      turn({ uuid: 's1', kind: 'assistant', label: 'sub', subagent_key: 'sk1', parent_uuid: 'x', tools: [{ name: 'Read', is_error: false }] }),
      turn({ uuid: 's2', kind: 'assistant', label: 'sub2', subagent_key: 'sk1', parent_uuid: 's1', tools: [{ name: 'Bash', is_error: true }] }),
    ], { sk1: { kind: 'agent' } });
    const sub = entries.find((e) => e.type === 'subagent')!;
    expect(sub).toMatchObject({ error: true });
  });

  it('a curated landmark BEFORE any human prompt emits at depth 0 and is in no section', () => {
    const { entries, sectionByUuid } = deriveOutline([
      // a heading-led assistant turn before the first prompt (rare; e.g. SessionStart skill).
      turn({ uuid: 'a0', kind: 'assistant', label: '## Pre-prompt note' }),
      turn({ uuid: 'h1', kind: 'human', label: 'the prompt' }),
    ], undefined);
    const pre = entries.find((e) => e.uuid === 'a0')!;
    expect(pre).toMatchObject({ type: 'heading', depth: 0 });
    // not mapped to any section.
    expect(sectionByUuid.has('a0')).toBe(false);
    expect(sectionByUuid.get('h1')).toBe('h1');
  });

  it('a zero-human session yields no sections (sectionByUuid empty); landmarks emit at depth 0', () => {
    const { entries, sectionByUuid } = deriveOutline([
      turn({ uuid: 'a1', kind: 'assistant', label: '## heading-only' }),
      turn({ uuid: 'a2', kind: 'assistant', label: 'generic prose' }),
    ], undefined);
    expect(sectionByUuid.size).toBe(0);
    expect(entries.map((e) => [e.uuid, e.depth])).toEqual([['a1', 0]]);
  });

  it('entryId is unique across a rich mixed fixture', () => {
    const meta: Record<string, SubagentMeta> = { sk1: { kind: 'explore' }, sk2: { kind: 'general-purpose' } };
    const { entries } = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'dispatch', member_uuids: ['h1', 'pm'] }),
      turn({ uuid: 'a1', kind: 'assistant', label: '## Plan', thinking: ['t-a', 't-b'] }),
      turn({ uuid: 's1', kind: 'assistant', label: 'sub a', subagent_key: 'sk2', parent_uuid: 'pm' }),
      turn({ uuid: 'a2', kind: 'assistant', label: 'reply' }),
      turn({ uuid: 's2', kind: 'assistant', label: 'sub b', subagent_key: 'sk1', parent_uuid: 'unresolved' }),
      turn({ uuid: 'h2', kind: 'human', label: 'next' }),
      turn({ uuid: 'm1', kind: 'meta', label: '/commit', meta_kind: 'command' }),
      turn({ uuid: 'e1', kind: 'assistant', label: '', tools: [{ name: 'Bash', is_error: true }] }),
    ], meta);
    const ids = entries.map((e) => e.entryId);
    expect(ids.length).toBeGreaterThan(0);
    expect(new Set(ids).size).toBe(ids.length);
    expect(ids.every((id) => typeof id === 'string' && id.length > 0)).toBe(true);
  });

  // cache-failure-markers spec §4 — three placement cases + the opt-out gate.
  // deriveOutline takes a third `markersEnabled` arg (default true); when on, a
  // flagged turn either emits a standalone 'cache' landmark (generic prose),
  // flags a coinciding landmark, or flags its subagent bucket row.
  const cf = { tokens_recreated: 130000, prev_cached: 130000, est_wasted_usd: 0.7475 };

  it('a generic flagged prose turn → a standalone cache landmark with cacheInfo', () => {
    const { entries } = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'go' }),
      // generic prose (no error/plan/heading) but flagged.
      turn({ uuid: 'a1', kind: 'assistant', label: 'rebuilt the prefix', cache_failure: cf }),
    ], undefined, true);
    const cache = entries.find((e) => e.type === 'cache');
    expect(cache).toBeTruthy();
    expect(cache).toMatchObject({
      uuid: 'a1', type: 'cache', depth: 1, cache: true,
      cacheInfo: { tokens_recreated: 130000, est_wasted_usd: 0.7475 },
    });
    // The label carries the humanized tokens + ~$ wasted.
    expect(cache!.label.toLowerCase()).toContain('cache rebuilt');
    expect(cache!.label).toContain('130K');
    expect(cache!.label).toContain('$0.75');
    // No duplicate row for the same turn — exactly one entry at a1.
    expect(entries.filter((e) => e.uuid === 'a1')).toHaveLength(1);
  });

  it('coincide: a flagged turn that is ALSO a plan → no second row, the plan entry gets cache:true', () => {
    const { entries } = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'plan it' }),
      turn({ uuid: 'a1', kind: 'assistant', label: 'proposing', cache_failure: cf,
             tools: [{ name: 'ExitPlanMode', is_error: false }] }),
    ], undefined, true);
    // The plan entry stays a plan (its glyph/label win); it just carries the flag.
    const plan = entries.find((e) => e.uuid === 'a1')!;
    expect(plan.type).toBe('plan');
    expect(plan.cache).toBe(true);
    expect(plan.cacheInfo).toMatchObject({ tokens_recreated: 130000, est_wasted_usd: 0.7475 });
    // No separate standalone cache row for the same turn.
    expect(entries.filter((e) => e.uuid === 'a1')).toHaveLength(1);
    expect(entries.some((e) => e.type === 'cache')).toBe(false);
  });

  it('coincide: a flagged turn that is ALSO an error → error type kept, cache:true added', () => {
    const { entries } = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'run it' }),
      turn({ uuid: 'a1', kind: 'assistant', label: '', cache_failure: cf,
             tools: [{ name: 'Bash', is_error: true }] }),
    ], undefined, true);
    const e = entries.find((x) => x.uuid === 'a1')!;
    expect(e.type).toBe('error');     // error still wins the type/glyph (red)
    expect(e.cache).toBe(true);       // but the cache flag rides along
  });

  it('subagent: a flagged subagent turn → bucket row gets cache:true, no nested cache row', () => {
    const meta = { sk1: { kind: 'explore' } };
    const { entries } = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'dispatch' }),
      turn({ uuid: 's1', kind: 'assistant', label: 'sub a', subagent_key: 'sk1', parent_uuid: 'x', member_uuids: ['s1'] }),
      turn({ uuid: 's2', kind: 'assistant', label: 'rebuilt', subagent_key: 'sk1', parent_uuid: 's1', cache_failure: cf, member_uuids: ['s2'] }),
    ], meta, true);
    const bucket = entries.find((e) => e.type === 'subagent')!;
    expect(bucket.cache).toBe(true);
    expect(bucket.cacheInfo).toMatchObject({ tokens_recreated: 130000 });
    // No standalone 'cache' row nested inside the bucket.
    expect(entries.some((e) => e.type === 'cache')).toBe(false);
  });

  it('markersEnabled=false → zero cache curation (no rows, no flags, no cacheInfo)', () => {
    const meta = { sk1: { kind: 'explore' } };
    const { entries } = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'go' }),
      turn({ uuid: 'a1', kind: 'assistant', label: 'generic', cache_failure: cf }),
      turn({ uuid: 'a2', kind: 'assistant', label: 'plan', cache_failure: cf, tools: [{ name: 'ExitPlanMode', is_error: false }] }),
      turn({ uuid: 's1', kind: 'assistant', label: 'sub', subagent_key: 'sk1', parent_uuid: 'x', cache_failure: cf }),
    ], meta, false);
    expect(entries.some((e) => e.type === 'cache')).toBe(false);
    expect(entries.some((e) => e.cache)).toBe(false);
    expect(entries.every((e) => e.cacheInfo === undefined)).toBe(true);
  });

  it('default markersEnabled (omitted arg) curates cache landmarks (back-compat default ON)', () => {
    const { entries } = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'go' }),
      turn({ uuid: 'a1', kind: 'assistant', label: 'rebuilt', cache_failure: cf }),
    ], undefined);
    expect(entries.some((e) => e.type === 'cache')).toBe(true);
  });

  it('#193: subagent landmark label uses description, falls back to kind', () => {
    // With a spawning Task description, the landmark mirrors the thread header.
    const withDesc: Record<string, SubagentMeta> = {
      abc: { kind: 'general-purpose', description: 'Implement #180' },
    };
    const { entries } = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'dispatch' }),
      turn({ uuid: 's1', kind: 'assistant', label: 'raw prompt', subagent_key: 'abc', parent_uuid: 'x' }),
    ], withDesc);
    const sc = entries.find((e) => e.type === 'subagent');
    expect(sc?.label).toBe('Implement #180');

    // No description → the existing `subagent · <kind>` label.
    const noDesc: Record<string, SubagentMeta> = { abc: { kind: 'general-purpose' } };
    const { entries: e2 } = deriveOutline([
      turn({ uuid: 'h1', kind: 'human', label: 'dispatch' }),
      turn({ uuid: 's1', kind: 'assistant', label: 'raw prompt', subagent_key: 'abc', parent_uuid: 'x' }),
    ], noDesc);
    expect(e2.find((e) => e.type === 'subagent')?.label).toBe('subagent · general-purpose');
  });
});
