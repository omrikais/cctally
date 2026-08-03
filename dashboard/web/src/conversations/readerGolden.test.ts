/// <reference types="node" />
import { describe, expect, it } from 'vitest';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { adaptQualifiedDetail } from '../lib/conversationAdapters';
import type { ConversationRef } from '../types/conversation';

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
