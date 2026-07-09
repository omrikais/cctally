import { describe, expect, it } from 'vitest';
import { filterParams } from './conversationFilterParams';
import { EMPTY_FILTERS } from '../types/conversation';

// #278 Theme C — the model-family axis serializes exactly like projects: one
// repeated ?models= per selected family, absent when empty (so the unfiltered
// base URL stays byte-identical, shared by browse AND search).
describe('filterParams models axis', () => {
  it('emits one models= per selected family', () => {
    expect(filterParams({ ...EMPTY_FILTERS, models: ['opus', 'sonnet'] }))
      .toBe('&models=opus&models=sonnet');
  });

  it('omits models when empty (byte-stable base URL)', () => {
    expect(filterParams(EMPTY_FILTERS)).toBe('');
  });

  it('composes models with the other axes', () => {
    const s = filterParams({ ...EMPTY_FILTERS, projects: ['p'], models: ['haiku'] });
    expect(s).toContain('projects=p');
    expect(s).toContain('models=haiku');
  });
});
