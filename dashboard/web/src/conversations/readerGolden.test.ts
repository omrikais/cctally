/// <reference types="node" />
import { describe, expect, it } from 'vitest';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { adaptQualifiedDetail, adaptQualifiedOutline } from '../lib/conversationAdapters';
import type { ConversationOutline, ConversationRef } from '../types/conversation';
import { deriveOutline } from './deriveOutline';
import { buildOutlineTargets } from './outlineNavigation';

// #463 S1, finding F23 — the client half of a two-sided pin. Each committed wire
// fixture holds the exact envelope `get_codex_conversation` produces for a
// synthetic Codex conversation; this test adapts it and hands the serialized
// neutral model to `bin/cctally-frontend-test`, which diffs it against the
// matching golden through the `_lib-golden-diff.sh` chokepoint (#106/#107 — the
// golden compare deliberately does NOT live in this file).
//
// A server-side change to segmentation or block building moves the wire
// fixture; a client-side change to adaptation moves the adapted golden. Neither
// side can drift undetected.
//
// TWO scenarios. `modern-full` covers every record family the reader renders but
// holds no turn near the 40-block segment budget, so every item in it carries
// `segment_ordinal: 0` and it would not move if segment boundaries or block
// accounting changed. `segmented-turn` holds one turn well past the budget, so a
// committed fixture actually carries several segments of one turn.
//
// vitest runs with cwd at dashboard/web. Resolve from cwd, which is a real fs
// path — `import.meta.url` carries a non-file scheme under vitest's transform.
const FIXTURES = resolve(process.cwd(), '../../tests/fixtures/codex-reader');
const OUT_DIR = process.env.CCTALLY_READER_GOLDEN_OUT_DIR;

const SCENARIOS = [
  { name: 'modern-full', wire: 'wire-detail.json', out: 'adapted.json', multiSegment: false },
  {
    name: 'segmented-turn',
    wire: 'wire-detail-segmented.json',
    out: 'adapted-segmented.json',
    multiSegment: true,
  },
  // #463 S3. Holds the tool-legibility shapes — a dict-shaped patch event, a
  // mixed `exec` program, the five `function_call` families, every output
  // preamble grammar, two shell sessions and the external-agent marker — none
  // of which appear in the two scenarios above.
  {
    name: 'tool-legibility',
    wire: 'wire-detail-tool-legibility.json',
    out: 'adapted-tool-legibility.json',
    multiSegment: false,
  },
] as const;

describe('codex reader-path golden (#463 S1 / F23)', () => {
  for (const scenario of SCENARIOS) {
    it(`adapts the pinned ${scenario.name} wire envelope deterministically`, () => {
      const wirePath = resolve(FIXTURES, scenario.wire);
      expect(
        existsSync(wirePath),
        `expected the wire fixture at ${wirePath} — regenerate it with bin/build-codex-reader-fixtures.py`,
      ).toBe(true);
      const wire = JSON.parse(readFileSync(wirePath, 'utf8'));
      const ref: ConversationRef = { source: 'codex', key: wire.conversation_key };
      const adapted = adaptQualifiedDetail(ref, wire);
      const serialized = `${JSON.stringify(adapted, null, 2)}\n`;

      // Non-vacuity: an empty adaptation would serialize to a stable golden that
      // proves nothing, so assert the fixture actually carried items through.
      expect(adapted.items.length).toBeGreaterThan(0);
      expect(adapted.items.every((item) => Array.isArray(item.member_uuids))).toBe(true);

      if (scenario.multiSegment) {
        // The whole point of this second scenario: if the fixture ever collapses
        // to one segment per turn it pins nothing about segmentation, and the
        // two-sided pin stops catching boundary or block-accounting drift.
        const ordinals = (wire.items as Array<{ segment_ordinal?: number }>).map(
          (item) => item.segment_ordinal ?? 0,
        );
        expect(Math.max(...ordinals)).toBeGreaterThan(0);
      }

      if (OUT_DIR) {
        const outPath = resolve(OUT_DIR, scenario.out);
        mkdirSync(dirname(outPath), { recursive: true });
        writeFileSync(outPath, serialized, 'utf8');
      }
    });
  }
});

// #463 S4, Task 0 — the same two-sided pin for the OUTLINE envelope. Until this
// block existed, nothing anywhere compared a real server outline envelope
// against a committed byte, and no test ran one through `adaptQualifiedOutline`
// at all: §1.5 of the S4 spec records that every outline unit test hand-builds
// its `OutlineTurn` fixtures with `tools`, `subagent_key` and `cache_failure`
// already populated, so the suite proved the components work when given data and
// never asked whether the adapter gives them any.
//
// `positionByKey` needs explicit serialization. `JSON.stringify` of a `Map` is
// `{}`, so a golden that stringified the adapted outline directly would pin the
// whole position index as the two characters `{}` and would not move if the
// index emptied — the exact vacuous golden this pin exists to avoid.

function serializeOutline(outline: ConversationOutline) {
  return {
    ...outline,
    positionByKey: Object.fromEntries(outline.positionByKey ?? new Map<string, number>()),
  };
}

// THIS IS A PROJECTION, NOT A COMPONENT RENDER. `deriveOutline` and
// `buildOutlineTargets` are the two pure functions that stand between the
// adapted outline and what `OutlinePanel` puts on screen, so serializing them
// pins the chrome's INPUT MODEL. It does not render `OutlinePanel`, evaluate any
// CSS, or exercise a click. A reader must not infer component coverage from a
// green run here; the real-browser gate is what covers the rendered surface.
function serializeChrome(outline: ConversationOutline) {
  // #463 S4 §1.4 — landmarks are passed exactly as `OutlinePanel` passes them,
  // or the projection would pin the pre-S4 turn-granular derivation and go green
  // whether or not landmark-awareness works.
  const derived = deriveOutline(
    outline.turns, outline.subagent_meta, true, undefined, undefined, outline.landmarks);
  const targets = buildOutlineTargets(outline.turns, undefined, outline.landmarks);
  return {
    entries: derived.entries,
    sectionByUuid: Object.fromEntries(derived.sectionByUuid),
    targets: {
      error: targets.error,
      prompt: targets.prompt,
      subagent: targets.subagent,
      plan: targets.plan,
      cache: targets.cache,
      compaction: targets.compaction,
      bookmark: targets.bookmark,
      indexByUuid: Object.fromEntries(targets.indexByUuid),
      memberIndex: Object.fromEntries(targets.memberIndex),
      segmentIndex: Object.fromEntries(targets.segmentIndex),
    },
  };
}

// `landmarkKinds` is the client twin of the server's Task 4 Step 4 exposure and
// is an ACCEPTANCE ITEM, not decoration. Breaking the server's `update_plan`
// mapping turns the wire golden red; breaking the ADAPTER's landmark mapping
// would not have, because before S4 these three goldens carried no `landmarks`
// key at all — so an adapter that dropped a family would have shipped with every
// golden byte-identical. Each scenario names the families its wire fixture
// really carries, and the assertion below fails if the adapted model loses one.
//
// `plan` exists on exactly one fixture. `update_plan` appears zero times in
// every rollout under tests/fixtures/codex-parity/v1/rollouts/, so the
// tool-legibility rollout gained one deliberately; without it the plan family
// had no fixture anywhere and its mapping was invisible to every golden.
const OUTLINE_SCENARIOS = [
  {
    name: 'modern-full',
    wire: 'wire-outline-modern-full.json',
    out: 'adapted-outline-modern-full.json',
    landmarkKinds: ['reasoning'],
  },
  {
    name: 'segmented-turn',
    wire: 'wire-outline-segmented-turn.json',
    out: 'adapted-outline-segmented-turn.json',
    landmarkKinds: ['reasoning'],
  },
  {
    name: 'tool-legibility',
    wire: 'wire-outline-tool-legibility.json',
    out: 'adapted-outline-tool-legibility.json',
    landmarkKinds: ['tool_error', 'plan'],
  },
] as const;

describe('codex outline golden (#463 S4 / Task 0)', () => {
  for (const scenario of OUTLINE_SCENARIOS) {
    it(`adapts the pinned ${scenario.name} outline envelope deterministically`, () => {
      const wirePath = resolve(FIXTURES, scenario.wire);
      expect(
        existsSync(wirePath),
        `expected the outline wire fixture at ${wirePath} — regenerate it with bin/build-codex-reader-fixtures.py`,
      ).toBe(true);
      const wire = JSON.parse(readFileSync(wirePath, 'utf8'));
      const ref: ConversationRef = { source: 'codex', key: wire.conversation_key };
      // No `totals` and no `promptItemKeys`: both are supplied by
      // `useConversationOutline` from separate fetches that this fixture family
      // does not carry, and passing neither keeps the golden a function of the
      // outline envelope alone.
      const outline = adaptQualifiedOutline(ref, wire);
      const serialized = `${JSON.stringify(
        { outline: serializeOutline(outline), chrome: serializeChrome(outline) },
        null,
        2,
      )}\n`;

      // Non-vacuity. An outline that adapted to nothing would serialize to a
      // stable golden proving nothing at all, and the position index is the one
      // field a naive `JSON.stringify` silently flattens to `{}`.
      expect(outline.turns.length).toBeGreaterThan(0);
      expect((outline.positionByKey?.size ?? 0)).toBeGreaterThan(0);
      // Every landmark family the wire carries survives adaptation, and every
      // landmark parents to a turn that is actually in the navigation subset —
      // the §1.3 retention rule, asserted on generated data rather than on a
      // hand-built fixture that could not have exhibited the orphaning.
      const kinds = new Set((outline.landmarks ?? []).map((landmark) => landmark.kind));
      for (const kind of scenario.landmarkKinds) expect([...kinds]).toContain(kind);
      const turnKeys = new Set(outline.turns.map((turn) => turn.uuid));
      for (const landmark of outline.landmarks ?? []) {
        expect(turnKeys).toContain(landmark.parent_uuid);
      }

      if (OUT_DIR) {
        const outPath = resolve(OUT_DIR, scenario.out);
        mkdirSync(dirname(outPath), { recursive: true });
        writeFileSync(outPath, serialized, 'utf8');
      }
    });
  }
});
