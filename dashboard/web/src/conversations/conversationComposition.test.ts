import { describe, expect, it } from 'vitest';
import { mergeConversationRows, mergeSearchHits } from './conversationComposition';
import type { ConversationSummary, SearchHit } from '../types/conversation';

function row(source: 'claude' | 'codex', key: string, when: string): ConversationSummary {
  return {
    conversation_ref: { source, key }, session_id: key, title: `${source}-${key}`,
    project_label: 'fixture', git_branch: null, started_utc: when,
    last_activity_utc: when, msg_count: 1, cost_usd: 0, models: [],
  };
}

function hit(source: 'claude' | 'codex', key: string, uuid: string, when: string): SearchHit {
  return {
    conversation_ref: { source, key }, session_id: key, uuid, title: `${source}-${key}`,
    project_label: 'fixture', ts: when, snippet: uuid, cost_usd: 0,
  };
}

describe('mixed conversation composition', () => {
  it('merges qualified browse rows by activity and preserves same-key source/root identities', () => {
    const rows = mergeConversationRows(
      [row('claude', 'v1.claude.shared', '2026-07-20T10:00:00Z')],
      [
        row('codex', 'v1.root-b.shared', '2026-07-20T12:00:00Z'),
        row('codex', 'v1.root-a.shared', '2026-07-20T11:00:00Z'),
      ],
    );
    expect(rows.map((r) => r.conversation_ref)).toEqual([
      { source: 'codex', key: 'v1.root-b.shared' },
      { source: 'codex', key: 'v1.root-a.shared' },
      { source: 'claude', key: 'v1.claude.shared' },
    ]);
  });

  it('deduplicates only an identical qualified hit, never a cross-source collision', () => {
    const duplicate = hit('codex', 'v1.root-a.shared', 'item-a', '2026-07-20T10:00:00Z');
    const hits = mergeSearchHits(
      [hit('claude', 'v1.claude.shared', 'item-a', '2026-07-20T10:00:00Z')],
      [duplicate, duplicate],
    );
    expect(hits).toHaveLength(2);
    expect(hits.map((h) => h.conversation_ref?.source).sort()).toEqual(['claude', 'codex']);
  });
});
