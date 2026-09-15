// #750 S4 §4.1. ONE vocabulary, both providers.
//
// This module used to return a week-flavoured vocabulary for Claude and a
// cycle-flavoured one for Codex. The distinction described a difference that
// does not exist: a Claude row has always been a billing CYCLE, because
// `report` split a credited week into two long before this epic. An in-place
// Anthropic quota credit ends one cycle and begins another inside the same
// subscription week, so a week credited n times renders n+1 rows.
//
// `trendVocabulary` keeps its `source` parameter and its shape so no caller
// has to change, and so a future provider that genuinely counts something
// else has somewhere to say so.
export type TrendUnit = 'cycle';

export interface TrendVocabulary {
  unit: TrendUnit;
  plural: 'cycles';
  column: 'Cycle';
  detail: 'Cycle';
  relativePrefix: 'C';
}

const CYCLE_VOCABULARY: TrendVocabulary = {
  unit: 'cycle',
  plural: 'cycles',
  column: 'Cycle',
  detail: 'Cycle',
  relativePrefix: 'C',
};

export function trendVocabulary(_source: 'claude' | 'codex'): TrendVocabulary {
  return CYCLE_VOCABULARY;
}

export function trendUnitCount(n: number, vocabulary: TrendVocabulary): string {
  return `${n} ${n === 1 ? vocabulary.unit : vocabulary.plural}`;
}

export function trendRelativeLabel(
  label: string | null | undefined,
  distance: number,
  vocabulary: TrendVocabulary,
): string {
  const relative = distance === 0 ? 'Now' : `${vocabulary.relativePrefix}\u2212${distance}`;
  return relative + (label ? ` \u00b7 ${label}` : '');
}
