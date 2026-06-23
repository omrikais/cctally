import { describe, it, expect } from 'vitest';
import { deltaIntent, semanticState } from './semantics';
import type { Semantic } from './semantics';

describe('deltaIntent', () => {
  it('lower-better: b<a improves (down), b>a regresses (up), b==a flat/neutral', () => {
    expect(deltaIntent('lower-better', 10, 4)).toEqual({ direction: 'down', intent: 'improve' });
    expect(deltaIntent('lower-better', 4, 10)).toEqual({ direction: 'up', intent: 'regress' });
    expect(deltaIntent('lower-better', 7, 7)).toEqual({ direction: 'flat', intent: 'neutral' });
  });

  it('higher-better: b>a improves (up), b<a regresses (down)', () => {
    expect(deltaIntent('higher-better', 4, 10)).toEqual({ direction: 'up', intent: 'improve' });
    expect(deltaIntent('higher-better', 10, 4)).toEqual({ direction: 'down', intent: 'regress' });
  });

  it('neutral polarity: direction is computed but intent is always neutral', () => {
    expect(deltaIntent('neutral', 4, 10)).toEqual({ direction: 'up', intent: 'neutral' });
    expect(deltaIntent('neutral', 10, 4)).toEqual({ direction: 'down', intent: 'neutral' });
    expect(deltaIntent('neutral', 5, 5)).toEqual({ direction: 'flat', intent: 'neutral' });
  });

  it('null a or b → flat / neutral', () => {
    expect(deltaIntent('lower-better', null, 10)).toEqual({ direction: 'flat', intent: 'neutral' });
    expect(deltaIntent('lower-better', 10, null)).toEqual({ direction: 'flat', intent: 'neutral' });
    expect(deltaIntent('higher-better', null, null)).toEqual({ direction: 'flat', intent: 'neutral' });
  });
});

describe('semanticState', () => {
  it('improve/regress resolve the arrow from direction', () => {
    expect(semanticState('improve', 'down')).toEqual({ className: 'sem-improve', glyph: '▼', srLabel: 'improved' });
    expect(semanticState('improve', 'up')).toEqual({ className: 'sem-improve', glyph: '▲', srLabel: 'improved' });
    expect(semanticState('regress', 'up')).toEqual({ className: 'sem-regress', glyph: '▲', srLabel: 'regression' });
    expect(semanticState('regress', 'down')).toEqual({ className: 'sem-regress', glyph: '▼', srLabel: 'regression' });
  });

  it('improve/regress with flat or absent direction → empty glyph, className+srLabel retained', () => {
    expect(semanticState('improve', 'flat')).toEqual({ className: 'sem-improve', glyph: '', srLabel: 'improved' });
    expect(semanticState('improve')).toEqual({ className: 'sem-improve', glyph: '', srLabel: 'improved' });
    expect(semanticState('regress')).toEqual({ className: 'sem-regress', glyph: '', srLabel: 'regression' });
  });

  it('neutral: arrow + increased/decreased/no change by direction', () => {
    expect(semanticState('neutral', 'up')).toEqual({ className: 'sem-neutral', glyph: '▲', srLabel: 'increased' });
    expect(semanticState('neutral', 'down')).toEqual({ className: 'sem-neutral', glyph: '▼', srLabel: 'decreased' });
    expect(semanticState('neutral', 'flat')).toEqual({ className: 'sem-neutral', glyph: '', srLabel: 'no change' });
    expect(semanticState('neutral')).toEqual({ className: 'sem-neutral', glyph: '', srLabel: 'no change' });
  });

  it('diff kinds have fixed glyphs and ignore direction', () => {
    expect(semanticState('match')).toEqual({ className: 'sem-match', glyph: '=', srLabel: 'match' });
    expect(semanticState('add')).toEqual({ className: 'sem-add', glyph: '+', srLabel: 'added' });
    expect(semanticState('del')).toEqual({ className: 'sem-del', glyph: '−', srLabel: 'removed' });
  });

  it('never colour alone: every Semantic returns a non-empty className AND a non-empty glyph OR srLabel', () => {
    const states: Semantic[] = ['improve', 'regress', 'neutral', 'match', 'add', 'del'];
    for (const s of states) {
      const p = semanticState(s);
      expect(p.className).not.toBe('');
      expect(p.glyph !== '' || p.srLabel !== '').toBe(true);
    }
  });
});
