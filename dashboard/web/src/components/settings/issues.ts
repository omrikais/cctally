// #513 S2 §3 — where a settings error lands.
//
// `_handle_post_settings` answers a 400 with `{error, field}`, and `field` is
// NOT always an editable leaf. It is sometimes an ANCESTOR: `alerts`, `budget`
// and `budget.codex` are all emitted, and so are `display`, `dashboard`,
// `update` and `update.check`. Walking a leaf-only registry up the dotted path
// would drop every one of those to form level, which breaks the promise that a
// Codex 400 paints on the Codex row rather than in a generic banner at the
// bottom of the form.
//
// So there are TWO maps. Leaf issues resolve through the registry's POST
// paths. Ancestor issues resolve through `GROUP_OWNERS`, which is kept OUTSIDE
// the registry on purpose: the registry carries a guard that no declared path
// is a prefix of another (`budget.codex.alerts_enabled` sits under `budget`),
// and adding `budget` to it would make that guard unsatisfiable.
import { REGISTRY, type SectionId, type ServerField } from './registry';

export type IssueTarget =
  | { kind: 'leaf'; id: string }
  | { kind: 'group'; section: SectionId }
  | { kind: 'form' };

// Every non-leaf path `_handle_post_settings` can name, mapped to the section
// that renders something owning it — or to `null` when this overlay renders
// nothing for it, in which case the issue belongs at form level.
//
// The `null` entries are a THIRD STATE, not an omission. Absence from this map
// and a `null` in it produce the same target today, but they say different
// things: absence means nobody has thought about the path, `null` means the
// overlay deliberately owns no row for it. That distinction is what lets
// `tests/test_settings_manifest.py` scrape the endpoint's own `"field":`
// literals and fail when a path appears there and not here — the check that
// makes the sentence above true rather than merely asserted. `cache_report` is
// the case that motivated it: the endpoint emits it, this overlay never renders
// it (it is edited in the cache-report popover), and the map used to say
// nothing at all about that.
//
// `display` is kept even though the handler never emits it bare: the block's
// five siblings are all here, and dropping the one that happens to be
// unreachable today would make the map read as an incomplete list rather than
// as the block-level vocabulary it is.
export const GROUP_OWNERS: Record<string, SectionId | null> = {
  display: 'display',
  alerts: 'alerts',
  budget: 'alerts',
  'budget.codex': 'alerts',
  dashboard: 'viewer',
  update: 'access',
  'update.check': 'access',
  // Both render disclosure rows in the access section (§2.4), so an error
  // naming one of them has a real place to land.
  'update.check.enabled': 'access',
  'update.check.ttl_hours': 'access',
  // Endpoint-writable, edited in the cache-report popover, rendered nowhere in
  // this overlay. Form level is the honest answer.
  cache_report: null,
};

const LEAF_BY_PATH = new Map<string, string>(
  REGISTRY.filter((field) => field.kind === 'server').map((field) => [
    (field as ServerField<unknown, unknown>).path,
    field.id,
  ]),
);

// `$` is the endpoint's own marker for "the body as a whole" (malformed JSON,
// not an object, too large). An unknown path is form-level too: we cannot
// point at a control we do not render.
export function resolveIssueTarget(field: string): IssueTarget {
  const leafId = LEAF_BY_PATH.get(field);
  if (leafId !== undefined) return { kind: 'leaf', id: leafId };
  // `Object.hasOwn`, not a truthiness test: `null` is a declared value here and
  // a bare lookup would also answer for inherited names like `constructor`.
  if (Object.hasOwn(GROUP_OWNERS, field)) {
    const section = GROUP_OWNERS[field];
    if (section !== null) return { kind: 'group', section };
  }
  return { kind: 'form' };
}

// #557: only an ignored path this overlay could actually submit can describe a
// broken promise. The endpoint's four accepted-then-discarded paths are
// disclosure-only rows: none is in REGISTRY, so buildBody() cannot send one.
// The manifest/static contract tests pin that disjointness. If a response lists
// an unsent path anyway, it says nothing about this save and needs no UI.
export function mismatchedIgnoredPaths(paths: readonly string[]): string[] {
  return paths.filter((path) => LEAF_BY_PATH.has(path));
}
