import { describe, expect, it } from 'vitest';
import {
  buildConversationJump,
  legacyClaudeConversationRef,
  type ConversationJump,
  type ConversationJumpOptions,
  type ConversationRef,
  type FindOccurrence,
} from './conversation';

// #463 S4 remediation round 7 — `buildConversationJump`'s three optional inputs
// moved from positional parameters 4-6 to a single options object, because the
// three do not share an omission rule and the positional form hid that:
// `innerAnchorKey` and `findOccurrence` are truthiness-gated while
// `expandDetails` is undefined-gated, so a caller wanting only an occurrence had
// to write `(ref, uuid, qualified, null, undefined, occ)` and writing `false`
// rather than `undefined` in that fifth slot silently added `expand_details:
// false`. TypeScript accepted both spellings.
//
// The refactor is meant to change how a call is WRITTEN and nothing else, so
// this file proves that by execution rather than by reading: `legacyBuild` below
// is the pre-round-7 body copied verbatim, and every one of the fifteen
// production call-site shapes is run through both builders and compared on
// value AND on key order. A copy of the old implementation only proves the two
// agree — it cannot notice that the old one was wrong — which is why the second
// describe block pins the ABSOLUTE key set each option combination produces.
// That is the assertion a future change to a default has to move.

// ---------------------------------------------------------------------------
// The pre-round-7 builder, copied verbatim from types/conversation.ts.
// Do not "fix" this to match a later version of the real builder: its whole
// purpose is to be the frozen prior behavior the current one is compared to.
function legacyBuild(
  ref: ConversationRef, uuid: string, qualified: boolean,
  innerAnchorKey?: string | null, expandDetails?: boolean,
  findOccurrence?: FindOccurrence | null,
): ConversationJump {
  return {
    ...(qualified ? { conversation_ref: ref } : {}),
    session_id: ref.key,
    uuid,
    ...(innerAnchorKey ? { inner_anchor_key: innerAnchorKey } : {}),
    ...(expandDetails === undefined ? {} : { expand_details: expandDetails }),
    ...(findOccurrence ? { find_occurrence: findOccurrence } : {}),
  };
}

const CLAUDE: ConversationRef = legacyClaudeConversationRef('sess-1');
const CODEX: ConversationRef = { source: 'codex', key: 'v1.abc' };

const OCC: FindOccurrence = {
  occurrence_id: 'o1', item_key: 'i1', uuid: 'u1',
  block_key: 'b1', container_block_key: 'c1', surface: 'output',
  match_kinds: ['tool'], disclosure: ['d1'],
  fragments: [],
};

// Every production call site, by file and line, with the arguments it passes.
// `legacy` is the positional tuple it used to pass; `options` is what it passes
// now. A site is listed once per distinct argument SHAPE it can take at
// runtime — the find bar and the inner-anchor sites are each listed with both
// their populated and their empty value, because the omission gates are exactly
// what differs between the two.
interface Site {
  site: string;
  ref: ConversationRef;
  uuid: string;
  qualified: boolean;
  legacy: [string | null | undefined, boolean | undefined, FindOccurrence | null | undefined];
  options: ConversationJumpOptions;
}

const SITES: Site[] = [
  // --- the nine sites that pass no options at all -------------------------
  { site: 'modals/CacheRebuildsSection.tsx:62', ref: CLAUDE, uuid: 'u', qualified: false,
    legacy: [undefined, undefined, undefined], options: {} },
  { site: 'conversations/ComparisonView.tsx:176', ref: CODEX, uuid: 'u', qualified: true,
    legacy: [undefined, undefined, undefined], options: {} },
  { site: 'conversations/ConversationRail.tsx:811', ref: CODEX, uuid: 'u', qualified: true,
    legacy: [undefined, undefined, undefined], options: {} },
  { site: 'ConversationReader.tsx:858 (jumpToLatest)', ref: CODEX, uuid: 'u', qualified: true,
    legacy: [undefined, undefined, undefined], options: {} },
  { site: 'ConversationReader.tsx:1162 (openIntent command)', ref: CLAUDE, uuid: 'u', qualified: false,
    legacy: [undefined, undefined, undefined], options: {} },
  { site: 'ConversationReader.tsx:2180 (jumpToCompletion)', ref: CODEX, uuid: 'u', qualified: true,
    legacy: [undefined, undefined, undefined], options: {} },
  { site: 'ConversationReader.tsx:2600 (stepHeading)', ref: CODEX, uuid: 'u', qualified: true,
    legacy: [undefined, undefined, undefined], options: {} },
  { site: 'ConversationReader.tsx:2662 (heading walk continue)', ref: CODEX, uuid: 'u', qualified: true,
    legacy: [undefined, undefined, undefined], options: {} },
  { site: 'ConversationReader.tsx:2921 (hidden-group reveal)', ref: CLAUDE, uuid: 'u', qualified: false,
    legacy: [undefined, undefined, undefined], options: {} },

  // --- the five sites that pass an inner anchor, populated and empty ------
  { site: 'conversations/OutlinePanel.tsx:112 (chip, with anchor)', ref: CODEX, uuid: 'seg-1', qualified: true,
    legacy: ['cbk1.e2', undefined, undefined], options: { innerAnchorKey: 'cbk1.e2' } },
  { site: 'conversations/OutlinePanel.tsx:112 (chip, no anchor)', ref: CODEX, uuid: 'seg-1', qualified: true,
    legacy: [undefined, undefined, undefined], options: { innerAnchorKey: undefined } },
  { site: 'conversations/OutlinePanel.tsx:554 (rail row, with anchor)', ref: CODEX, uuid: 'seg-2', qualified: true,
    legacy: ['cbk1.e3', undefined, undefined], options: { innerAnchorKey: 'cbk1.e3' } },
  { site: 'conversations/OutlinePanel.tsx:554 (rail row, no anchor)', ref: CLAUDE, uuid: 'turn-2', qualified: false,
    legacy: [undefined, undefined, undefined], options: { innerAnchorKey: undefined } },
  { site: 'ConversationReader.tsx:2137 (jumpNext, with anchor)', ref: CODEX, uuid: 'seg-3', qualified: true,
    legacy: ['cbk2.e1', undefined, undefined], options: { innerAnchorKey: 'cbk2.e1' } },
  { site: 'ConversationReader.tsx:2137 (jumpNext, bare turn)', ref: CODEX, uuid: 'turn-3', qualified: true,
    legacy: [undefined, undefined, undefined], options: { innerAnchorKey: undefined } },
  { site: 'ConversationReader.tsx:2166 (jumpToLast, with anchor)', ref: CODEX, uuid: 'seg-4', qualified: true,
    legacy: ['cbk3.e1', undefined, undefined], options: { innerAnchorKey: 'cbk3.e1' } },
  { site: 'ConversationReader.tsx:2166 (jumpToLast, bare turn)', ref: CLAUDE, uuid: 'turn-4', qualified: false,
    legacy: [undefined, undefined, undefined], options: { innerAnchorKey: undefined } },
  { site: 'ConversationReader.tsx:2410 (requestHeading, heading key)', ref: CODEX, uuid: 'seg-5', qualified: true,
    legacy: ['h.2', undefined, undefined], options: { innerAnchorKey: 'h.2' } },
  // A heading target whose key is the empty string: truthiness-gated, so the
  // key is omitted. This is the shape that would differ if the gate were
  // changed to `!= null`.
  { site: 'ConversationReader.tsx:2410 (requestHeading, empty key)', ref: CODEX, uuid: 'seg-5', qualified: true,
    legacy: ['', undefined, undefined], options: { innerAnchorKey: '' } },

  // --- the one site that passes expandDetails and findOccurrence ----------
  // Schema-2 occurrence-exact find: an occurrence with disclosures.
  { site: 'conversations/FindBar.tsx:123 (occurrence, disclosures)', ref: CODEX, uuid: 'u1', qualified: true,
    legacy: [null, true, OCC], options: { expandDetails: true, findOccurrence: OCC } },
  // The same, with expand_details FALSE — the payload the positional form made
  // easy to write by accident and hard to write on purpose.
  { site: 'conversations/FindBar.tsx:123 (occurrence, no disclosures)', ref: CODEX, uuid: 'u1', qualified: true,
    legacy: [null, false, OCC], options: { expandDetails: false, findOccurrence: OCC } },
  // Legacy schema-1 anchor: no occurrence, expand_details from match_kinds.
  { site: 'conversations/FindBar.tsx:123 (legacy anchor, tool match)', ref: CLAUDE, uuid: 'a1', qualified: false,
    legacy: [null, true, null], options: { expandDetails: true, findOccurrence: null } },
  { site: 'conversations/FindBar.tsx:123 (legacy anchor, prose only)', ref: CLAUDE, uuid: 'a1', qualified: false,
    legacy: [null, false, null], options: { expandDetails: false, findOccurrence: null } },
];

describe('#463 S4 round 7 — the options object emits a byte-identical payload', () => {
  it.each(SITES.map((s) => [s.site, s] as const))(
    'matches the positional builder at %s',
    (_label, s) => {
      const [anchor, expand, occ] = s.legacy;
      const before = legacyBuild(s.ref, s.uuid, s.qualified, anchor, expand, occ);
      const after = buildConversationJump(s.ref, s.uuid, s.qualified, s.options);
      expect(after).toEqual(before);
      // `toEqual` compares values and rejects extra keys but ignores key ORDER,
      // and the payload is JSON-serialized onto the wire, so pin the order too.
      expect(Object.keys(after)).toEqual(Object.keys(before));
      expect(JSON.stringify(after)).toBe(JSON.stringify(before));
    },
  );

  it('covers all fifteen production call sites', () => {
    // Each site appears under a `file:line (case)` label; strip the case to get
    // the distinct sites. TWO LIMITS, stated because the count reads stronger
    // than it is. This counts `SITES`, which this file authors — so it pins the
    // fifteen sites known when it was written, and a SIXTEENTH production call
    // site would not redden it. And each row's legacy tuple and options object
    // are both authored here, so a production site converted to the WRONG
    // options object would still pass. What this file does prove is that the
    // builder emits byte-identical payloads for the argument shapes those
    // fifteen sites use. The literal ban is enforced separately by
    // `jumpConstruction.test.ts`, which scans the real tree.
    const distinct = new Set(SITES.map((s) => s.site.replace(/\s*\(.*\)$/, '')));
    expect(distinct.size).toBe(15);
  });
});

describe('#463 S4 round 7 — the builder emits an exact key set per option', () => {
  const keys = (j: ConversationJump) => Object.keys(j);

  it('omits every optional key when no options are supplied', () => {
    expect(keys(buildConversationJump(CLAUDE, 'u', false)))
      .toEqual(['session_id', 'uuid']);
    expect(keys(buildConversationJump(CODEX, 'u', true)))
      .toEqual(['conversation_ref', 'session_id', 'uuid']);
    // An options object naming nothing is the same as none at all.
    expect(buildConversationJump(CODEX, 'u', true, {}))
      .toEqual(buildConversationJump(CODEX, 'u', true));
  });

  it('gates innerAnchorKey and findOccurrence on TRUTHINESS', () => {
    for (const falsy of [undefined, null, '']) {
      expect(keys(buildConversationJump(CODEX, 'u', true, { innerAnchorKey: falsy })))
        .toEqual(['conversation_ref', 'session_id', 'uuid']);
    }
    expect(keys(buildConversationJump(CODEX, 'u', true, { innerAnchorKey: 'k' })))
      .toEqual(['conversation_ref', 'session_id', 'uuid', 'inner_anchor_key']);
    for (const falsy of [undefined, null]) {
      expect(keys(buildConversationJump(CODEX, 'u', true, { findOccurrence: falsy })))
        .toEqual(['conversation_ref', 'session_id', 'uuid']);
    }
    expect(keys(buildConversationJump(CODEX, 'u', true, { findOccurrence: OCC })))
      .toEqual(['conversation_ref', 'session_id', 'uuid', 'find_occurrence']);
  });

  it('gates expandDetails on being SUPPLIED, so an explicit false IS emitted', () => {
    // This is the asymmetry the options object exists to make visible. `false`
    // is a real value the find bar sends; only omission removes the key.
    expect(keys(buildConversationJump(CODEX, 'u', true, { expandDetails: false })))
      .toEqual(['conversation_ref', 'session_id', 'uuid', 'expand_details']);
    expect(buildConversationJump(CODEX, 'u', true, { expandDetails: false }).expand_details)
      .toBe(false);
    expect(keys(buildConversationJump(CODEX, 'u', true, { expandDetails: true })))
      .toEqual(['conversation_ref', 'session_id', 'uuid', 'expand_details']);
    expect(keys(buildConversationJump(CODEX, 'u', true, { expandDetails: undefined })))
      .toEqual(['conversation_ref', 'session_id', 'uuid']);
  });

  it('emits the full key set in wire order when every option is supplied', () => {
    expect(keys(buildConversationJump(CODEX, 'u', true, {
      innerAnchorKey: 'k', expandDetails: false, findOccurrence: OCC,
    }))).toEqual([
      'conversation_ref', 'session_id', 'uuid',
      'inner_anchor_key', 'expand_details', 'find_occurrence',
    ]);
  });
});
