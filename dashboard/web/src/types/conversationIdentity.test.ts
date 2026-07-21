import { describe, expect, it } from 'vitest';
import * as conversation from './conversation';

const claude = { source: 'claude', key: 'shared-native-id' } as const;
const codexA = { source: 'codex', key: 'v1.root-a-shared-native-id' } as const;
const codexB = { source: 'codex', key: 'v1.root-b-shared-native-id' } as const;

describe('qualified conversation identity', () => {
  it('keeps provider and root-qualified identities collision-free', () => {
    const keyOf = (conversation as unknown as {
      conversationRefKey?: (ref: typeof claude | typeof codexA | typeof codexB) => string;
    }).conversationRefKey;

    expect(typeof keyOf).toBe('function');
    expect(new Set([keyOf!(claude), keyOf!(codexA), keyOf!(codexB)])).toHaveLength(3);
  });

  it('round-trips the canonical key without interpreting the opaque value', () => {
    const api = conversation as unknown as {
      conversationRefKey?: (ref: typeof codexA) => string;
      parseConversationRefKey?: (key: string) => unknown;
    };

    expect(api.parseConversationRefKey?.(api.conversationRefKey!(codexA))).toEqual(codexA);
  });
});
