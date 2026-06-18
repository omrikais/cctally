// #205 S3 (F6/F7) — abbreviate a verbose model id for compact (mobile)
// display: strip the `claude-` vendor prefix and a trailing `-YYYYMMDD` date
// stamp. Any shape that doesn't match (e.g. `gpt-5`, a future id) passes
// through unchanged, and the result is never empty (fail-safe on a degenerate
// input such as a bare `claude-`). Pure — unit-tested in modelName.test.ts.
export function abbreviateModel(name: string): string {
  const trimmed = name.trim();
  const stripped = trimmed.replace(/^claude-/i, '').replace(/-\d{8}$/, '');
  return stripped || trimmed;
}
