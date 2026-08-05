import { Fragment, useContext, useMemo } from 'react';
import type { Element, Root, RootContent, Text } from 'hast';
import { ExactFindContext, HighlightContext, type HighlightTerms } from './HighlightContext';
import { projectMarkdown } from './findProjection';

// #223 — mirror the server find-scan caps in bin/_lib_conversation_query.py.
export const FIND_REGEX_MAX_LEN = 1000;
export const FIND_SCAN_TEXT_CAP = 200_000;

export type Part = { s: string; hit: boolean };
export type SplitFn = (value: string) => Part[] | null;

// #177 S6 / #217 S4 — split a value on term matches (longest first). Moved
// verbatim from Markdown.tsx.
export function splitByTerms(value: string, terms: string[], caseSensitive: boolean): Part[] | null {
  const cmpVal = caseSensitive ? value : value.toLowerCase();
  const cmpTerms = [...terms]
    .map((t) => (caseSensitive ? t : t.toLowerCase()))
    .sort((a, b) => b.length - a.length);
  const out: Part[] = [];
  let i = 0;
  let any = false;
  while (i < value.length) {
    let matched = 0;
    for (const t of cmpTerms) {
      if (t && cmpVal.startsWith(t, i)) { matched = t.length; break; }
    }
    if (matched > 0) {
      out.push({ s: value.slice(i, i + matched), hit: true });
      i += matched;
      any = true;
    } else {
      const last = out[out.length - 1];
      if (last && !last.hit) last.s += value[i];
      else out.push({ s: value[i], hit: false });
      i += 1;
    }
  }
  return any ? out : null;
}

// #223 — regex variant (global). Moved verbatim from Markdown.tsx.
export function splitByRegex(value: string, re: RegExp): Part[] | null {
  if (value.length > FIND_SCAN_TEXT_CAP) return null;
  re.lastIndex = 0;
  const out: Part[] = [];
  let last = 0;
  let any = false;
  let m: RegExpExecArray | null;
  while ((m = re.exec(value)) !== null) {
    if (m[0].length === 0) { re.lastIndex += 1; continue; }
    if (m.index > last) out.push({ s: value.slice(last, m.index), hit: false });
    out.push({ s: m[0], hit: true });
    last = m.index + m[0].length;
    any = true;
  }
  if (!any) return null;
  if (last < value.length) out.push({ s: value.slice(last), hit: false });
  return out;
}

// One place that turns a HighlightTerms into a split fn (or null = no matcher).
// Mirrors the old Markdown.tsx behavior: an empty term list, an over-cap source,
// or an invalid regex yields null (the caller then adds no highlighting).
export function buildSplit(highlight: HighlightTerms | null): SplitFn | null {
  if (!highlight) return null;
  if (highlight.kind === 'terms') {
    const terms = highlight.terms.filter(Boolean);
    if (!terms.length) return null;
    return (value) => splitByTerms(value, terms, highlight.caseSensitive);
  }
  if (highlight.source.length > FIND_REGEX_MAX_LEN) return null;
  let re: RegExp;
  try { re = new RegExp(highlight.source, highlight.caseSensitive ? 'g' : 'gi'); }
  catch { return null; }
  return (value) => splitByRegex(value, re);
}

// Hook: the active split fn from HighlightContext, memoized on primitive keys
// (so memoized renderers re-run only when the matcher actually changes). null
// when find is closed — the zero-overhead common path.
export function useFindSplit(): SplitFn | null {
  const highlight = useContext(HighlightContext);
  const kind = highlight?.kind ?? null;
  const caseSensitive = highlight?.caseSensitive ?? false;
  const key = highlight
    ? (highlight.kind === 'terms' ? highlight.terms.join(' ') : highlight.source)
    : '';
  return useMemo(() => {
    if (!kind || !key) return null;
    if (kind === 'terms') return buildSplit({ kind: 'terms', terms: key.split(' '), caseSensitive });
    return buildSplit({ kind: 'regex', source: key, caseSensitive });
  }, [kind, key, caseSensitive]);
}

export function useExactSurfaceTargets(
  containerBlockKey: string | null | undefined,
  surface: 'body' | 'call' | 'output' | 'completion',
): ReadonlyMap<string, ExactLeafTarget[]> {
  const exact = useContext(ExactFindContext);
  return useMemo(() => {
    const byLeaf = new Map<string, ExactLeafTarget[]>();
    if (!exact || !containerBlockKey) return byLeaf;
    for (const occurrence of exact.occurrences) {
      if (
        occurrence.container_block_key !== containerBlockKey
        || occurrence.surface !== surface
      ) continue;
      for (const fragment of occurrence.fragments) {
        const targets = byLeaf.get(fragment.leaf_key) ?? [];
        targets.push({
          occurrenceId: occurrence.occurrence_id,
          leafKey: fragment.leaf_key,
          start: fragment.start,
          end: fragment.end,
          current: occurrence.occurrence_id === exact.selectedOccurrenceId,
        });
        byLeaf.set(fragment.leaf_key, targets);
      }
    }
    return byLeaf;
  }, [exact, containerBlockKey, surface]);
}

export function useExactLeafTargets(
  containerBlockKey: string | null | undefined,
  surface: 'body' | 'call' | 'output' | 'completion',
  leafKey = 't0',
): ExactLeafTarget[] {
  return useExactSurfaceTargets(containerBlockKey, surface).get(leafKey) ?? [];
}

// #177 S6 / #223 — the shared rehype tree-walk. Wraps each `hit` segment in
// <mark>. `skipCode: true` skips {code,pre} subtrees (prose — inline code +
// fenced blocks are handled elsewhere); `skipCode: false` marks everywhere
// (used on refractor's hast inside a code block).
export function applyMarksToHast(tree: Root, split: SplitFn, opts: { skipCode: boolean }): void {
  const SKIP = new Set(['code', 'pre']);
  const walkChildren = (children: RootContent[], inSkip: boolean): RootContent[] => {
    const next: RootContent[] = [];
    for (const child of children) {
      if (child.type === 'element') {
        const childSkip = inSkip || (opts.skipCode && SKIP.has(child.tagName));
        child.children = walkChildren(child.children, childSkip) as Element['children'];
        next.push(child);
      } else if (child.type === 'text' && !inSkip && child.value) {
        const parts = split(child.value);
        if (parts) {
          for (const p of parts) {
            if (p.hit) {
              next.push({
                type: 'element', tagName: 'mark', properties: {},
                children: [{ type: 'text', value: p.s } as Text],
              } as Element);
            } else {
              next.push({ type: 'text', value: p.s } as Text);
            }
          }
        } else {
          next.push(child);
        }
      } else {
        next.push(child);
      }
    }
    return next;
  };
  tree.children = walkChildren(tree.children, false) as Root['children'];
}

// react-markdown rehype plugin factory for PROSE (skipCode: true). Mirrors the
// old `[[rehypeMarkTerms, …]]` tuple shape: react-markdown calls this with the
// split arg and uses the returned transformer.
export function applyMarksPlugin(split: SplitFn) {
  return (tree: Root) => applyMarksToHast(tree, split, { skipCode: true });
}

// Split a plain string into React nodes (strings + keyed <mark>s), for the
// non-hast render surfaces (unregistered fences, plain tool <pre>s). Over-cap or
// no-match → the bare string (byte-identical to today).
export function splitToReactNodes(value: string, split: SplitFn): React.ReactNode {
  if (value.length > FIND_SCAN_TEXT_CAP) return value;
  const parts = split(value);
  if (!parts) return value;
  return parts.map((p, i) =>
    p.hit ? <mark key={i}>{p.s}</mark> : <Fragment key={i}>{p.s}</Fragment>);
}

export interface ExactLeafTarget {
  occurrenceId: string;
  leafKey: string;
  sourceLeafKey?: string;
  start: number;
  end: number;
  current: boolean;
}

type ExactPart = {
  text: string;
  target: ExactLeafTarget | null;
};

function exactParts(value: string, targets: ExactLeafTarget[]): ExactPart[] {
  const sorted = [...targets]
    .filter((target) => target.start >= 0 && target.end > target.start && target.end <= [...value].length)
    .sort((a, b) => a.start - b.start || a.end - b.end);
  if (!sorted.length) return [{ text: value, target: null }];
  const scalars = [...value];
  const parts: ExactPart[] = [];
  let cursor = 0;
  for (const target of sorted) {
    if (target.start < cursor) continue;
    if (target.start > cursor) parts.push({ text: scalars.slice(cursor, target.start).join(''), target: null });
    parts.push({ text: scalars.slice(target.start, target.end).join(''), target });
    cursor = target.end;
  }
  if (cursor < scalars.length) parts.push({ text: scalars.slice(cursor).join(''), target: null });
  return parts;
}

export function splitToExactNodes(value: string, targets: ExactLeafTarget[]): React.ReactNode {
  const parts = exactParts(value, targets);
  if (parts.every((part) => part.target == null)) return value;
  return parts.map((part, index) => part.target ? (
    <mark
      key={`${part.target.occurrenceId}-${index}`}
      data-find-occurrence-id={part.target.occurrenceId}
      data-find-leaf-key={part.target.sourceLeafKey ?? part.target.leafKey}
      data-find-current={part.target.current ? 'true' : undefined}
      className={part.target.current ? CURRENT_MARK_CLASS : undefined}
    >{part.text}</mark>
  ) : <Fragment key={`plain-${index}`}>{part.text}</Fragment>);
}

function exactHastNodes(value: string, targets: ExactLeafTarget[]): RootContent[] {
  return exactParts(value, targets).map((part) => {
    if (!part.target) return { type: 'text', value: part.text } as Text;
    return {
      type: 'element',
      tagName: 'mark',
      properties: {
        'data-find-occurrence-id': part.target.occurrenceId,
        'data-find-leaf-key': part.target.sourceLeafKey ?? part.target.leafKey,
        ...(part.target.current ? {
          'data-find-current': 'true',
          className: [CURRENT_MARK_CLASS],
        } : {}),
      },
      children: [{ type: 'text', value: part.text } as Text],
    } as Element;
  });
}

export function applyExactMarksToHast(
  tree: Root,
  source: string,
  targets: ExactLeafTarget[],
): void {
  const { text: projectedText, leaves } = projectMarkdown(source);
  const byLeaf = new Map<string, ExactLeafTarget[]>();
  for (const target of targets) {
    const list = byLeaf.get(target.leafKey) ?? [];
    list.push(target);
    byLeaf.set(target.leafKey, list);
  }
  const textNodes: Text[] = [];
  const collect = (children: RootContent[]) => {
    for (const child of children) {
      if (child.type === 'element') collect(child.children);
      else if (child.type === 'text' && child.value) textNodes.push(child);
    }
  };
  collect(tree.children);
  const domText = textNodes.map((node) => node.value).join('');
  const domStartByLeaf = new Map<string, number>();
  let searchAt = 0;
  for (const leaf of leaves) {
    const expected = [...projectedText].slice(leaf.start, leaf.end).join('');
    const found = domText.indexOf(expected, searchAt);
    if (found < 0) continue;
    domStartByLeaf.set(leaf.key, [...domText.slice(0, found)].length);
    searchAt = found + expected.length;
  }
  const globalTargets: ExactLeafTarget[] = [];
  for (const [leafKey, targetsForLeaf] of byLeaf) {
    const leafStart = domStartByLeaf.get(leafKey);
    if (leafStart == null) continue;
    for (const target of targetsForLeaf) {
      globalTargets.push({
        ...target,
        sourceLeafKey: target.sourceLeafKey ?? target.leafKey,
        leafKey,
        start: leafStart + target.start,
        end: leafStart + target.end,
      });
    }
  }
  let domCursor = 0;
  const walk = (children: RootContent[]): RootContent[] => {
    const out: RootContent[] = [];
    for (const child of children) {
      if (child.type === 'element') {
        child.children = walk(child.children) as Element['children'];
        out.push(child);
        continue;
      }
      if (child.type !== 'text' || !child.value) {
        out.push(child);
        continue;
      }
      const length = [...child.value].length;
      const local = globalTargets
        .filter((target) => target.start < domCursor + length && target.end > domCursor)
        .map((target) => ({
          ...target,
          start: Math.max(0, target.start - domCursor),
          end: Math.min(length, target.end - domCursor),
        }));
      out.push(...exactHastNodes(child.value, local));
      domCursor += length;
    }
    return out;
  };
  tree.children = walk(tree.children) as Root['children'];
}

export function applyGlobalExactMarksToHast(
  tree: Root,
  targets: ExactLeafTarget[],
): void {
  let cursor = 0;
  const walk = (children: RootContent[]): RootContent[] => {
    const out: RootContent[] = [];
    for (const child of children) {
      if (child.type === 'element') {
        child.children = walk(child.children) as Element['children'];
        out.push(child);
        continue;
      }
      if (child.type !== 'text' || !child.value) {
        out.push(child);
        continue;
      }
      const length = [...child.value].length;
      const local = targets
        .filter((target) => target.start < cursor + length && target.end > cursor)
        .map((target) => ({
          ...target,
          start: Math.max(0, target.start - cursor),
          end: Math.min(length, target.end - cursor),
        }));
      out.push(...exactHastNodes(child.value, local));
      cursor += length;
    }
    return out;
  };
  tree.children = walk(tree.children) as Root['children'];
}

export function applyExactMarksPlugin(source: string, targets: ExactLeafTarget[]) {
  return (tree: Root) => applyExactMarksToHast(tree, source, targets);
}

// ---- Part 1: landable-mark selection (#236) --------------------------------

type RectLike = { top: number; bottom: number; left: number; right: number };

// Pure: is `mark` outside ANY clip rect (so it would be visually clipped)?
export function markRectClipped(mark: RectLike, clips: RectLike[]): boolean {
  return clips.some((c) =>
    mark.bottom <= c.top || mark.top >= c.bottom || mark.right <= c.left || mark.left >= c.right);
}

function collectClipRects(mark: HTMLElement, stopAt: HTMLElement): RectLike[] {
  const clips: RectLike[] = [];
  let node: HTMLElement | null = mark.parentElement;
  while (node) {
    const style = getComputedStyle(node);
    const ov = `${style.overflow} ${style.overflowX} ${style.overflowY}`;
    if (/auto|scroll|hidden|clip/.test(ov)) clips.push(node.getBoundingClientRect());
    if (node === stopAt) break;
    node = node.parentElement;
  }
  return clips;
}

// The first <mark> in `turnEl` (document order) that is laid out AND not clipped
// by any inner scroll/clamp ancestor up to the turn root. null → caller centers
// the turn root instead (today's behavior). Read-only: it never scrolls anything.
export function firstLandableMark(turnEl: HTMLElement): HTMLElement | null {
  const marks = turnEl.querySelectorAll<HTMLElement>('mark');
  for (let i = 0; i < marks.length; i++) {
    const mark = marks[i];
    const mr = mark.getBoundingClientRect();
    if (mr.width === 0 && mr.height === 0) continue; // not laid out (e.g. closed <details>)
    if (!markRectClipped(mr, collectClipRects(mark, turnEl))) return mark;
  }
  return null;
}

// ---- #238 R5: distinct "current" find match -------------------------------

// #238 R5 — move the distinct "current match" class from prev to next. Imperative
// (marks live inside memoized markdown); returns the new current for the caller's
// ref. Best-effort: lost on a Virtuoso recycle, re-applied on the next n/N step.
export const CURRENT_MARK_CLASS = 'conv-mark--current';
export function applyCurrentMark(
  prev: HTMLElement | null,
  next: HTMLElement | null,
): HTMLElement | null {
  if (prev === next) return prev;
  prev?.classList.remove(CURRENT_MARK_CLASS);
  next?.classList.add(CURRENT_MARK_CLASS);
  return next;
}

export function applyCurrentOccurrence(
  root: ParentNode,
  occurrenceId: string | null,
): HTMLElement[] {
  const marks = Array.from(
    root.querySelectorAll<HTMLElement>('mark[data-find-occurrence-id]'),
  );
  for (const mark of marks) {
    const current = occurrenceId != null && mark.dataset.findOccurrenceId === occurrenceId;
    mark.toggleAttribute('data-find-current', current);
    mark.classList.toggle(CURRENT_MARK_CLASS, current);
  }
  return marks.filter((mark) => mark.dataset.findOccurrenceId === occurrenceId);
}

export function firstLandableOccurrenceFragment(
  turnEl: HTMLElement,
  occurrenceId: string,
): HTMLElement | null {
  const marks = turnEl.querySelectorAll<HTMLElement>(
    `mark[data-find-occurrence-id="${CSS.escape(occurrenceId)}"]`,
  );
  for (const mark of marks) {
    const rect = mark.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) continue;
    if (!markRectClipped(rect, collectClipRects(mark, turnEl))) return mark;
  }
  return null;
}
