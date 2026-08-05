import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, test } from 'vitest';
import {
  literalRanges,
  projectMarkdown,
  projectPlain,
  sliceRangeToLeaves,
} from './findProjection';

interface ProjectionCase {
  name: string;
  kind: 'markdown' | 'plain';
  source?: string;
  leaves?: Array<{ key: string; text: string }>;
  expected: unknown;
}

interface LiteralCase {
  name: string;
  text: string;
  query: string;
  caseSensitive: boolean;
  expected: unknown;
}

interface SliceCase {
  name: string;
  match: { start: number; end: number };
  leaves: Array<{ key: string; start: number; end: number }>;
  expected: unknown;
}

const cases = JSON.parse(readFileSync(
  resolve(process.cwd(), '../../tests/fixtures/codex-find-projection/cases.json'),
  'utf8',
)) as {
  projectionCases: ProjectionCase[];
  literalCases: LiteralCase[];
  sliceCases: SliceCase[];
};

describe('canonical Codex find projection', () => {
  for (const fixture of cases.projectionCases) {
    test(fixture.name, () => {
      const actual = fixture.kind === 'markdown'
        ? projectMarkdown(fixture.source ?? '')
        : projectPlain(fixture.leaves ?? []);
      expect(actual).toEqual(fixture.expected);
    });
  }

  for (const fixture of cases.literalCases) {
    test(fixture.name, () => {
      expect(literalRanges(fixture.text, fixture.query, fixture.caseSensitive))
        .toEqual(fixture.expected);
    });
  }

  for (const fixture of cases.sliceCases) {
    test(fixture.name, () => {
      expect(sliceRangeToLeaves(fixture.match, fixture.leaves)).toEqual(fixture.expected);
    });
  }
});
