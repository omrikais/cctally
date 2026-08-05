import type { ConversationBlock, ConversationSessionIndex } from '../types/conversation';

type Call = Extract<ConversationBlock, { kind: 'tool_call' }>;

// #463 S3 §5.3 — every SHELL session ordinal a call references. A `cell`
// reference is deliberately excluded: the two namespaces have zero overlapping
// values, so treating a cell id as a session ordinal would group unrelated
// calls under one heading. A null ref contributes nothing, because the server
// could not resolve that session and the client must not invent an ordinal.
export function shellSessionRefs(call: Call): string[] {
  const card = call.native_card;
  if (card?.type === 'session_ref') {
    return card.scope === 'shell' && card.ref != null ? [card.ref] : [];
  }
  if (card?.type === 'program') {
    return card.invocations.flatMap((invocation) => (
      invocation.kind === 'session' && invocation.scope === 'shell' && invocation.ref != null
        ? [invocation.ref] : []
    ));
  }
  return [];
}

// §5.4 — the ONE vocabulary for naming a session reference and what a call did
// to it. The collapsed row (composed in the adapter) and the card body (composed
// in the components) both read from here, because when each composed its own
// wording they drifted: one invocation read "wrote to shell 1" on the row and
// "session 1" in the body.
//
// A null ref names nothing at all. The server registers a session only from a
// standalone `write_stdin` row, so a session named inside a program body has no
// ordinal, and inventing one would make two different sessions read as one.
export function sessionReferenceLabel(
  scope: 'shell' | 'cell', sessionRef: string | null,
): string {
  if (sessionRef == null) return '';
  return scope === 'shell' ? `session ${sessionRef}` : `cell ${sessionRef}`;
}

// The short form the .conv-status-narrow media pair shows at 640px.
export function sessionReferenceLabelNarrow(
  scope: 'shell' | 'cell', sessionRef: string | null,
): string {
  if (sessionRef == null) return '';
  return scope === 'shell' ? `s${sessionRef}` : `c${sessionRef}`;
}

// `write_stdin` is always a write and `wait` is always a poll, so the label
// follows the operation alone.
export function sessionOperationLabel(operation: 'write' | 'poll'): string {
  return operation === 'write' ? 'wrote to' : 'polled';
}

export function sessionOperationLabelNarrow(operation: 'write' | 'poll'): string {
  return operation === 'write' ? 'wrote' : 'poll';
}

// The bound on a one-line preview of what a call wrote. Whitespace is collapsed
// because the summary row is a single nowrap line; the expanded body shows the
// raw bytes. Shared so the adapter's collapsed preview and the card's own
// preview cannot clamp at two different lengths.
const SESSION_CHARS_PREVIEW_MAX = 60;

export function sessionCharsPreview(chars: string): string {
  const collapsed = chars.replace(/\s+/g, ' ').trim();
  return collapsed.length > SESSION_CHARS_PREVIEW_MAX
    ? `${collapsed.slice(0, SESSION_CHARS_PREVIEW_MAX)}…`
    : collapsed;
}

// The one session a RUN belongs to, or null when the run names none or mixes
// several. This reads ordinals the server assigned over the WHOLE conversation;
// it never computes uniqueness, ordering or shortening from the loaded window,
// which is what keeps the label stable across pages and live-tail appends.
export function runSessionRef(calls: readonly Call[]): string | null {
  const refs = new Set(calls.flatMap((call) => shellSessionRefs(call)));
  return refs.size === 1 ? [...refs][0] : null;
}

// §5.3 keeps three statements distinct so a reader is never told something is
// absent when it was merely not loaded:
//   opener          — this call is the one that started the session
//   not_retained    — no retained output announced an opener for it
//   index_truncated — the index itself was capped, so an opener may exist
//   silent          — nothing to say (no index, a cell, an unresolved ref, or
//                     a later call in a session whose opener is elsewhere)
export type OpenerState = 'opener' | 'not_retained' | 'index_truncated' | 'silent';

export function openerState(
  index: ConversationSessionIndex | undefined,
  scope: 'shell' | 'cell',
  sessionRef: string | null,
  blockKey: string | null,
): OpenerState {
  if (!index || scope !== 'shell' || sessionRef == null) return 'silent';
  const entry = index.sessions[sessionRef];
  if (entry?.opener_block_key != null) {
    return blockKey != null && blockKey === entry.opener_block_key ? 'opener' : 'silent';
  }
  // Truncation wins over absence: the index being capped means an opener may
  // simply not have been loaded, and saying "not retained" there would be a
  // claim the data does not support.
  return index.truncated ? 'index_truncated' : 'not_retained';
}
