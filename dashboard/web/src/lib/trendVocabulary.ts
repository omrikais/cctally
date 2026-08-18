export type TrendUnit = 'week' | 'cycle';

export interface TrendVocabulary {
  unit: TrendUnit;
  plural: 'weeks' | 'cycles';
  column: 'Week' | 'Cycle';
  detail: 'Weekly' | 'Cycle';
  relativePrefix: 'W' | 'C';
}

const WEEK_VOCABULARY: TrendVocabulary = {
  unit: 'week',
  plural: 'weeks',
  column: 'Week',
  detail: 'Weekly',
  relativePrefix: 'W',
};

const CYCLE_VOCABULARY: TrendVocabulary = {
  unit: 'cycle',
  plural: 'cycles',
  column: 'Cycle',
  detail: 'Cycle',
  relativePrefix: 'C',
};

export function trendVocabulary(source: 'claude' | 'codex'): TrendVocabulary {
  return source === 'claude' ? WEEK_VOCABULARY : CYCLE_VOCABULARY;
}

export function trendUnitCount(n: number, vocabulary: TrendVocabulary): string {
  return `${n} ${n === 1 ? vocabulary.unit : vocabulary.plural}`;
}

export function trendRelativeLabel(
  label: string | null | undefined,
  distance: number,
  vocabulary: TrendVocabulary,
): string {
  const relative = distance === 0 ? 'Now' : `${vocabulary.relativePrefix}−${distance}`;
  return relative + (label ? ` · ${label}` : '');
}
