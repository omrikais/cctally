import { conversationRefKey, normalizeConversationRef, type ConversationRefInput } from '../types/conversation';

// #228 S5 E5 — resolve the comparison pick-mode banner label: the anchor
// session's cached title (truncated) when known, else the opaque short hash
// (cold-boot / pasted-URL case, before the rail title cache is populated).
const MAX = 48;
export function pickBannerLabel(
  anchor: ConversationRefInput,
  titles: Record<string, string>,
): { kind: 'title' | 'hash'; text: string } {
  const ref = normalizeConversationRef(anchor);
  const t = titles[conversationRefKey(ref)]?.trim()
    ?? (typeof anchor === 'string' ? titles[anchor]?.trim() : undefined);
  if (t) return { kind: 'title', text: t.length > MAX ? `${t.slice(0, MAX - 1)}…` : t };
  return { kind: 'hash', text: ref.key.slice(0, 8) };
}
