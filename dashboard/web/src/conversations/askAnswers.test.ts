import { describe, expect, it } from 'vitest';
import { parseAnswersFromResult, matchSelectedLabels } from './askAnswers';

describe('parseAnswersFromResult', () => {
  it('extracts "Q"="A" pairs regardless of preamble', () => {
    expect(parseAnswersFromResult(
      'User has answered your questions: "Q1"="A1", "Q2"="A2". You can…',
    )).toEqual({ Q1: 'A1', Q2: 'A2' });
  });
  it('returns empty map when no pairs', () => {
    expect(parseAnswersFromResult('nothing here')).toEqual({});
  });
});

describe('matchSelectedLabels', () => {
  const opts = (labels: string[]) => labels.map((label) => ({ label }));
  it('single-select exact match', () => {
    expect(matchSelectedLabels('A', opts(['A', 'B'])))
      .toEqual({ selected: ['A'], custom: null });
  });
  it('multiSelect — comma-joined labels', () => {
    expect(matchSelectedLabels('A, B', opts(['A', 'B', 'C'])))
      .toEqual({ selected: ['A', 'B'], custom: null });
  });
  it('REGRESSION (Codex P1): label containing ", " is not mis-split', () => {
    // options include a label that itself contains ", "; the answer IS that label.
    expect(matchSelectedLabels('A, B', opts(['A', 'A, B'])))
      .toEqual({ selected: ['A, B'], custom: null });
  });
  it('custom "Other" answer matches no label', () => {
    expect(matchSelectedLabels('something typed', opts(['A', 'B'])))
      .toEqual({ selected: [], custom: 'something typed' });
  });
});
