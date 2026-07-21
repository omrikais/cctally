import {
  conversationRefKey,
  conversationSummaryRef,
  searchHitConversationRef,
  type ConversationSummary,
  type SearchHit,
} from '../types/conversation';

function descendingTimestamp(a: string | null | undefined, b: string | null | undefined): number {
  return (b ?? '').localeCompare(a ?? '');
}

// S7 intentionally rejects source=all. All-mode fetches each qualified source
// independently, then uses these deterministic, identity-preserving merges.
export function mergeConversationRows(
  claude: ConversationSummary[],
  codex: ConversationSummary[],
): ConversationSummary[] {
  const byIdentity = new Map<string, ConversationSummary>();
  for (const row of [...claude, ...codex]) {
    const key = conversationRefKey(conversationSummaryRef(row));
    if (!byIdentity.has(key)) byIdentity.set(key, row);
  }
  return [...byIdentity.values()].sort((a, b) =>
    descendingTimestamp(a.last_activity_utc, b.last_activity_utc)
    || conversationRefKey(conversationSummaryRef(a)).localeCompare(conversationRefKey(conversationSummaryRef(b))));
}

export function mergeSearchHits(claude: SearchHit[], codex: SearchHit[]): SearchHit[] {
  const byIdentity = new Map<string, SearchHit>();
  for (const hit of [...claude, ...codex]) {
    const key = JSON.stringify([conversationRefKey(searchHitConversationRef(hit)), hit.uuid]);
    if (!byIdentity.has(key)) byIdentity.set(key, hit);
  }
  return [...byIdentity.values()].sort((a, b) =>
    descendingTimestamp(a.ts, b.ts)
    || conversationRefKey(searchHitConversationRef(a)).localeCompare(conversationRefKey(searchHitConversationRef(b)))
    || a.uuid.localeCompare(b.uuid));
}
