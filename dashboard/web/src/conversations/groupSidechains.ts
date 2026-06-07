import type { ConversationItem } from '../types/conversation';

// Render-grouping for the reader: a contiguous run of is_sidechain items
// collapses into ONE expandable group (spec §4 decision: frontend-only,
// no source_path grouping in v1). Non-sidechain items pass through as
// single entries. Order is preserved.
export type RenderGroup =
  | { kind: 'item'; item: ConversationItem }
  | { kind: 'sidechain'; items: ConversationItem[] };

export function groupSidechains(items: ConversationItem[]): RenderGroup[] {
  const out: RenderGroup[] = [];
  let run: ConversationItem[] = [];
  const flush = () => { if (run.length) { out.push({ kind: 'sidechain', items: run }); run = []; } };
  for (const it of items) {
    if (it.is_sidechain) { run.push(it); }
    else { flush(); out.push({ kind: 'item', item: it }); }
  }
  flush();
  return out;
}
