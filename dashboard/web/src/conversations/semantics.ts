// #228 S1 — the conversation viewer's "never convey state by colour alone"
// chokepoint. `deltaIntent` is pure A-vs-B logic; `semanticState` maps a
// semantic token to its presentation and ALWAYS returns a glyph + sr label
// alongside the colour className, so a consumer can never style by colour only.
// Defined in S1; consumed by S5's comparison strip (cost/errors/duration =
// 'lower-better'; tokens/prompts/files = 'neutral').

export type Polarity = 'lower-better' | 'higher-better' | 'neutral';
export type Direction = 'up' | 'down' | 'flat';
export type Intent = 'improve' | 'regress' | 'neutral';

export interface DeltaIntent {
  direction: Direction;
  intent: Intent;
}

export function deltaIntent(polarity: Polarity, a: number | null, b: number | null): DeltaIntent {
  if (a == null || b == null) return { direction: 'flat', intent: 'neutral' };
  const d = b - a;
  const direction: Direction = d > 0 ? 'up' : d < 0 ? 'down' : 'flat';
  if (direction === 'flat' || polarity === 'neutral') return { direction, intent: 'neutral' };
  const improved =
    (polarity === 'lower-better' && direction === 'down') ||
    (polarity === 'higher-better' && direction === 'up');
  return { direction, intent: improved ? 'improve' : 'regress' };
}

export type Semantic = 'improve' | 'regress' | 'neutral' | 'match' | 'add' | 'del';

export interface SemanticPresentation {
  className: string;
  glyph: string;
  srLabel: string;
}

function arrow(direction?: Direction): string {
  return direction === 'up' ? '▲' : direction === 'down' ? '▼' : '';
}

export function semanticState(s: Semantic, direction?: Direction): SemanticPresentation {
  switch (s) {
    case 'improve':
      return { className: 'sem-improve', glyph: arrow(direction), srLabel: 'improved' };
    case 'regress':
      return { className: 'sem-regress', glyph: arrow(direction), srLabel: 'regression' };
    case 'neutral':
      return {
        className: 'sem-neutral',
        glyph: arrow(direction),
        srLabel: direction === 'up' ? 'increased' : direction === 'down' ? 'decreased' : 'no change',
      };
    case 'match':
      return { className: 'sem-match', glyph: '=', srLabel: 'match' };
    case 'add':
      return { className: 'sem-add', glyph: '+', srLabel: 'added' };
    case 'del':
      return { className: 'sem-del', glyph: '−', srLabel: 'removed' };
  }
}
