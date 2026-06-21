import { describe, expect, it } from 'vitest';
import { segmentContextBody, parseUnifiedDiff } from './contextDiff';

// #217 S5 F6 — the segmenter splits an injected-context body into prose + diff
// regions, anchoring CONSERVATIVELY on a real `diff --git a/… b/…` marker (NOT
// bare +/- prefixes, which appear in markdown lists). The parser turns a diff
// region into per-file `{ oldPath, newPath, hunks }` reusing computeDiff's
// Hunk/DiffRow shape.

// A real injected git-context body (spec Q6 real-data grounding): a leading
// prose sentence on the SAME line as the `diff --git` marker, an `index` header,
// `---`/`+++`, an `@@` hunk header, and signed body lines.
const realBody =
  '- Unstaged changes: diff --git a/CLAUDE.md b/CLAUDE.md\n' +
  'index a1b2c3d..e4f5g6h 100644\n' +
  '--- a/CLAUDE.md\n' +
  '+++ b/CLAUDE.md\n' +
  '@@ -1,2 +1,3 @@\n' +
  ' ctx line\n' +
  '+added line\n' +
  '-removed line\n';

describe('segmentContextBody', () => {
  it('splits prose + diff on a real injected git-context body', () => {
    const segs = segmentContextBody(realBody);
    expect(segs.some((s) => s.kind === 'diff')).toBe(true);
    expect(segs.some((s) => s.kind === 'prose')).toBe(true);
    // The pre-marker prose ("- Unstaged changes:") is flushed as prose.
    const prose = segs.find((s) => s.kind === 'prose')!;
    expect(prose.text).toContain('Unstaged changes');
    // The diff region starts at the `diff --git` marker.
    const diff = segs.find((s) => s.kind === 'diff')!;
    expect(diff.text).toContain('diff --git a/CLAUDE.md b/CLAUDE.md');
    expect(diff.text).toContain('@@ -1,2 +1,3 @@');
  });

  // NON-VACUITY ANCHOR (spec §6 / Codex P2-1): a markdown bullet body whose
  // lines start with `-`/`+` must stay PURE prose — the anchor is `diff --git`,
  // not a bare line-prefix, so this never false-detects a diff.
  it('keeps a markdown bullet body as pure prose (no false diff)', () => {
    const segs = segmentContextBody('- a bullet\n+ another bullet\n- third\n');
    expect(segs.every((s) => s.kind === 'prose')).toBe(true);
  });

  it('a body with no diff marker is a single prose segment', () => {
    const segs = segmentContextBody('just some prose\nwith two lines\n');
    expect(segs).toHaveLength(1);
    expect(segs[0].kind).toBe('prose');
    expect(segs[0].text).toContain('just some prose');
  });

  it('ends the diff region on the first non-diff line and resumes prose', () => {
    const body =
      'diff --git a/x b/x\n' +
      '@@ -1 +1 @@\n' +
      '-a\n' +
      '+b\n' +
      'trailing prose after the diff\n';
    const segs = segmentContextBody(body);
    const diff = segs.find((s) => s.kind === 'diff')!;
    expect(diff.text).toContain('+b');
    expect(diff.text).not.toContain('trailing prose');
    const trailing = segs.filter((s) => s.kind === 'prose');
    expect(trailing.some((s) => s.text.includes('trailing prose'))).toBe(true);
  });

  it('keeps consecutive multi-file diffs in ONE diff region', () => {
    const body =
      'diff --git a/x b/x\n' +
      '@@ -1 +1 @@\n' +
      '-a\n' +
      '+b\n' +
      'diff --git a/y b/y\n' +
      '@@ -1 +1 @@\n' +
      '-c\n' +
      '+d\n';
    const segs = segmentContextBody(body);
    const diffs = segs.filter((s) => s.kind === 'diff');
    expect(diffs).toHaveLength(1);
    expect(diffs[0].text).toContain('diff --git a/x b/x');
    expect(diffs[0].text).toContain('diff --git a/y b/y');
  });

  it('consumes git extended headers (new file mode / rename) without splitting', () => {
    const body =
      'diff --git a/old.txt b/new.txt\n' +
      'old mode 100644\n' +
      'new mode 100755\n' +
      'similarity index 95%\n' +
      'rename from old.txt\n' +
      'rename to new.txt\n' +
      'new file mode 100644\n' +
      'deleted file mode 100644\n' +
      'copy from a.txt\n' +
      'copy to b.txt\n' +
      'dissimilarity index 5%\n' +
      'index 111..222 100644\n' +
      '--- a/old.txt\n' +
      '+++ b/new.txt\n' +
      '@@ -1 +1 @@\n' +
      '-a\n' +
      '+b\n';
    const segs = segmentContextBody(body);
    const diffs = segs.filter((s) => s.kind === 'diff');
    // The extended headers must NOT terminate the diff early → one diff region
    // containing the full block.
    expect(diffs).toHaveLength(1);
    expect(diffs[0].text).toContain('rename to new.txt');
    expect(diffs[0].text).toContain('+b');
    // No prose leaked out of the extended-header lines.
    expect(segs.every((s) => s.kind === 'diff')).toBe(true);
  });
});

describe('parseUnifiedDiff', () => {
  it('parses a multi-file diff into per-file hunks', () => {
    const files = parseUnifiedDiff(
      'diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\ndiff --git a/y b/y\n@@ -1 +1 @@\n-c\n+d\n',
    );
    expect(files).toHaveLength(2);
    expect(files[0].newPath).toBe('x');
    expect(files[1].newPath).toBe('y');
    expect(files[0].hunks).toHaveLength(1);
  });

  it('maps signed body lines to add/del/context rows with running numbers', () => {
    const files = parseUnifiedDiff(
      'diff --git a/x b/x\n@@ -1,2 +1,2 @@\n ctx\n-old\n+new\n',
    );
    const rows = files[0].hunks[0];
    expect(rows.map((r) => r.type)).toEqual(['context', 'del', 'add']);
    // context row carries both gutter numbers; del has only old; add has only new.
    const ctx = rows[0];
    expect(ctx.oldNo).toBe(1);
    expect(ctx.newNo).toBe(1);
    expect(rows[1].oldNo).toBe(2);
    expect(rows[1].newNo).toBeNull();
    expect(rows[2].newNo).toBe(2);
    expect(rows[2].oldNo).toBeNull();
  });

  it('uses the @@ header start lines for gutter numbering', () => {
    const files = parseUnifiedDiff(
      'diff --git a/x b/x\n@@ -10,1 +20,2 @@\n-old\n+new\n+extra\n',
    );
    const rows = files[0].hunks[0];
    expect(rows[0].oldNo).toBe(10); // del starts at old 10
    expect(rows[1].newNo).toBe(20); // first add starts at new 20
    expect(rows[2].newNo).toBe(21);
  });

  it('parses oldPath/newPath from the diff --git line', () => {
    const files = parseUnifiedDiff('diff --git a/dir/foo.py b/dir/foo.py\n@@ -1 +1 @@\n-a\n+b\n');
    expect(files[0].oldPath).toBe('dir/foo.py');
    expect(files[0].newPath).toBe('dir/foo.py');
  });
});
