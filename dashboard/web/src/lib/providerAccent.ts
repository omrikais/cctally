// #463 S5 (F24b) — the single source of truth for provider IDENTITY colour.
// Identity means "this is Codex" / "this is Claude". It is NOT for state
// colours: the Codex budget bar is orange, Codex errors are red and Codex
// pending is amber, and none of those names a provider.
export type ProviderSource = 'claude' | 'codex';

export function providerAccentClass(source: ProviderSource): string {
  return source === 'codex' ? 'accent-provider-codex' : 'accent-provider-claude';
}
