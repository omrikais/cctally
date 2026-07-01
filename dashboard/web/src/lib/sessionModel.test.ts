import { describe, it, expect } from 'vitest';
import { isSingleModel } from './sessionModel';

const d = (models: string[], cpm: string[]) => ({
  models: models.map((name) => ({ name })),
  cost_per_model: cpm.map((model) => ({ model, cost_usd: 1 })),
} as never);

describe('isSingleModel', () => {
  it('true for exactly one distinct model', () => {
    expect(isSingleModel(d(['opus'], ['opus']))).toBe(true);
  });
  it('false for two models', () => {
    expect(isSingleModel(d(['opus', 'haiku'], ['opus', 'haiku']))).toBe(false);
  });
  it('false for zero models', () => {
    expect(isSingleModel(d([], []))).toBe(false);
  });
  // Non-vacuous: distinctness is measured over the UNION of `models` and
  // `cost_per_model`, so a session whose one model chip and one cost row name
  // DIFFERENT models is two distinct models, not one.
  it('false when models and cost_per_model name different single models', () => {
    expect(isSingleModel(d(['opus'], ['haiku']))).toBe(false);
  });
  // A single distinct model that appears only in cost_per_model (models empty)
  // still collapses — the union has size 1.
  it('true when the sole model appears only in cost_per_model', () => {
    expect(isSingleModel(d([], ['opus']))).toBe(true);
  });
});
