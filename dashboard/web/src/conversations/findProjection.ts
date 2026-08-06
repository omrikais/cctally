import { decodeString } from 'micromark-util-decode-string';
import { parseUnifiedDiff, segmentContextBody } from './contextDiff';

export interface RenderLeaf {
  key: string;
  text: string;
}

export interface ProjectedLeaf {
  key: string;
  start: number;
  end: number;
}

export interface FindRange {
  start: number;
  end: number;
}

export interface LeafFragment {
  leaf_key: string;
  start: number;
  end: number;
}

export interface Projection {
  text: string;
  leaves: ProjectedLeaf[];
}

const scalarLength = (value: string) => Array.from(value).length;

class ProjectionBuilder {
  private parts: string[] = [];
  private length = 0;
  private openLeaf: number | null = null;
  readonly leaves: ProjectedLeaf[] = [];

  boundary(): void {
    this.openLeaf = null;
  }

  separator(value: string): void {
    if (!value) return;
    this.boundary();
    this.parts.push(value);
    this.length += scalarLength(value);
  }

  emit(value: string, options: { boundary?: boolean; key?: string } = {}): void {
    if (!value) return;
    if (options.boundary) this.boundary();
    const start = this.length;
    this.parts.push(value);
    this.length += scalarLength(value);
    if (options.key !== undefined) {
      this.leaves.push({ key: options.key, start, end: this.length });
      this.openLeaf = null;
      return;
    }
    if (this.openLeaf === null) {
      this.openLeaf = this.leaves.length;
      this.leaves.push({ key: `t${this.openLeaf}`, start, end: this.length });
    } else {
      this.leaves[this.openLeaf].end = this.length;
    }
  }

  value(): Projection {
    return { text: this.parts.join(''), leaves: this.leaves };
  }
}

const tableDelimiter = /^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/;
const blockPrefix = /^\s*(?:(?:#{1,6})\s+|>\s?|(?:[-+*]|\d+[.)])\s+)/;
const taskMarker = /^\[[ xX]\]\s+/;
const autolink = /^<((?:https?:\/\/|mailto:)[^ <>]+|[^ <>@]+@[^ <>@]+)>/;

function tableCells(line: string): string[] {
  let value = line.trim();
  if (value.startsWith('|')) value = value.slice(1);
  if (value.endsWith('|')) value = value.slice(0, -1);
  const cells: string[] = [];
  let current = '';
  let escaped = false;
  for (const char of value) {
    if (escaped) {
      current += char;
      escaped = false;
    } else if (char === '\\') {
      current += char;
      escaped = true;
    } else if (char === '|') {
      cells.push(current.trim());
      current = '';
    } else {
      current += char;
    }
  }
  cells.push(current.trim());
  return cells;
}

function findClosing(source: string, token: string, start: number): number {
  let cursor = start;
  for (;;) {
    const found = source.indexOf(token, cursor);
    if (found < 0) return -1;
    let backslashes = 0;
    let probe = found - 1;
    while (probe >= 0 && source[probe] === '\\') {
      backslashes += 1;
      probe -= 1;
    }
    if (backslashes % 2 === 0) return found;
    cursor = found + token.length;
  }
}

function projectInline(source: string, builder: ProjectionBuilder): void {
  let plain = '';
  const flush = () => {
    if (plain) builder.emit(decodeString(plain));
    plain = '';
  };
  let cursor = 0;
  while (cursor < source.length) {
    if (source[cursor] === '\\' && cursor + 1 < source.length) {
      plain += source[cursor + 1];
      cursor += 2;
      continue;
    }
    if (source[cursor] === '`') {
      let run = 1;
      while (source[cursor + run] === '`') run += 1;
      const token = '`'.repeat(run);
      const close = findClosing(source, token, cursor + run);
      if (close >= 0) {
        flush();
        builder.emit(source.slice(cursor + run, close).trim(), { boundary: true });
        builder.boundary();
        cursor = close + run;
        continue;
      }
    }
    const image = source.startsWith('![', cursor);
    if (image || source[cursor] === '[') {
      const labelStart = cursor + (image ? 2 : 1);
      const labelEnd = source.indexOf('](', labelStart);
      if (labelEnd >= 0) {
        const destinationEnd = source.indexOf(')', labelEnd + 2);
        if (destinationEnd >= 0) {
          flush();
          builder.boundary();
          if (!image) projectInline(source.slice(labelStart, labelEnd), builder);
          builder.boundary();
          cursor = destinationEnd + 1;
          continue;
        }
      }
    }
    if (source[cursor] === '<') {
      const match = autolink.exec(source.slice(cursor));
      if (match) {
        flush();
        builder.boundary();
        builder.emit(match[1].startsWith('mailto:') ? match[1].slice(7) : match[1]);
        builder.boundary();
        cursor += match[0].length;
        continue;
      }
    }
    let matched = false;
    for (const token of ['**', '__', '~~', '*', '_']) {
      if (!source.startsWith(token, cursor)) continue;
      const close = findClosing(source, token, cursor + token.length);
      if (close < 0 || close === cursor + token.length) continue;
      flush();
      builder.boundary();
      projectInline(source.slice(cursor + token.length, close), builder);
      builder.boundary();
      cursor = close + token.length;
      matched = true;
      break;
    }
    if (matched) continue;
    const scalar = Array.from(source.slice(cursor))[0];
    plain += scalar;
    cursor += scalar.length;
  }
  flush();
}

type Block =
  | { kind: 'code'; value: string }
  | { kind: 'table'; value: string[][] }
  | { kind: 'paragraph'; value: string[] };

export function projectMarkdown(source: string): Projection {
  const builder = new ProjectionBuilder();
  const lines = source.replace(/\r\n?/g, '\n').split('\n');
  const blocks: Block[] = [];
  let cursor = 0;
  while (cursor < lines.length) {
    const line = lines[cursor];
    if (!line.trim()) {
      cursor += 1;
      continue;
    }
    const fence = /^\s*(`{3,}|~{3,})(?:[^`]*)$/.exec(line);
    if (fence) {
      const token = fence[1];
      const body: string[] = [];
      cursor += 1;
      const close = new RegExp(`^\\s*${token[0]}{${token.length},}\\s*$`);
      while (cursor < lines.length && !close.test(lines[cursor])) {
        body.push(lines[cursor]);
        cursor += 1;
      }
      if (cursor < lines.length) cursor += 1;
      blocks.push({ kind: 'code', value: body.length ? `${body.join('\n')}\n` : '' });
      continue;
    }
    if (cursor + 1 < lines.length && line.includes('|') && tableDelimiter.test(lines[cursor + 1])) {
      const rows = [tableCells(line)];
      cursor += 2;
      while (cursor < lines.length && lines[cursor].trim() && lines[cursor].includes('|')) {
        rows.push(tableCells(lines[cursor]));
        cursor += 1;
      }
      blocks.push({ kind: 'table', value: rows });
      continue;
    }
    const stripLine = (value: string) => {
      let stripped = value.replace(blockPrefix, '');
      if (taskMarker.test(stripped)) stripped = stripped.replace(taskMarker, ' ');
      if (stripped.endsWith('  ')) stripped = stripped.slice(0, -2);
      else if (stripped.endsWith('\\')) stripped = stripped.slice(0, -1);
      return stripped;
    };
    const paragraph = [stripLine(line)];
    cursor += 1;
    while (cursor < lines.length && lines[cursor].trim()) {
      if (/^\s*(`{3,}|~{3,})/.test(lines[cursor])) break;
      paragraph.push(stripLine(lines[cursor]));
      cursor += 1;
    }
    blocks.push({ kind: 'paragraph', value: paragraph });
  }

  blocks.forEach((block, blockIndex) => {
    if (blockIndex) builder.separator('\n');
    if (block.kind === 'code') {
      builder.emit(block.value, { boundary: true });
      builder.boundary();
    } else if (block.kind === 'table') {
      block.value.forEach((row, rowIndex) => {
        if (rowIndex) builder.separator('\n');
        row.forEach((cell, cellIndex) => {
          if (cellIndex) builder.separator('\t');
          builder.boundary();
          projectInline(cell, builder);
          builder.boundary();
        });
      });
    } else {
      block.value.forEach((line, lineIndex) => {
        if (lineIndex) builder.separator('\n');
        projectInline(line, builder);
      });
    }
  });
  return builder.value();
}

export function projectPlain(leaves: RenderLeaf[]): Projection {
  const builder = new ProjectionBuilder();
  for (const leaf of leaves) builder.emit(leaf.text, { key: leaf.key });
  return builder.value();
}

export function projectContext(source: string): Projection {
  const parts: string[] = [];
  const leaves: ProjectedLeaf[] = [];
  let cursor = 0;
  const separator = () => {
    if (!parts.length) return;
    parts.push('\n');
    cursor += 1;
  };
  const appendProjection = (projection: Projection, prefix: string) => {
    if (!projection.text) return;
    separator();
    const start = cursor;
    parts.push(projection.text);
    cursor += scalarLength(projection.text);
    leaves.push(...projection.leaves.map((leaf) => ({
      key: `${prefix}/${leaf.key}`,
      start: start + leaf.start,
      end: start + leaf.end,
    })));
  };
  segmentContextBody(source).forEach((segment, segmentIndex) => {
    if (segment.kind === 'prose') {
      appendProjection(projectMarkdown(segment.text), `segments.${segmentIndex}.prose`);
      return;
    }
    parseUnifiedDiff(segment.text).forEach((file, fileIndex) => {
      file.hunks.forEach((rows, hunkIndex) => {
        rows.forEach((row, rowIndex) => {
          if (!row.text) return;
          separator();
          const start = cursor;
          parts.push(row.text);
          cursor += scalarLength(row.text);
          leaves.push({
            key: `segments.${segmentIndex}.files.${fileIndex}.hunks.${hunkIndex}.rows.${rowIndex}`,
            start,
            end: cursor,
          });
        });
      });
    });
  });
  return { text: parts.join(''), leaves };
}

function scalarLower(value: string): string {
  return Array.from(value, (scalar) => {
    const lowered = scalar.toLowerCase();
    return Array.from(lowered).length === 1 ? lowered : scalar;
  }).join('');
}

export function literalRanges(text: string, query: string, caseSensitive: boolean): FindRange[] {
  if (!query) return [];
  const haystack = Array.from(caseSensitive ? text : scalarLower(text));
  const needle = Array.from(caseSensitive ? query : scalarLower(query));
  if (!needle.length) return [];
  const matches: FindRange[] = [];
  let cursor = 0;
  while (cursor <= haystack.length - needle.length) {
    let found = -1;
    for (let start = cursor; start <= haystack.length - needle.length; start += 1) {
      if (needle.every((scalar, index) => haystack[start + index] === scalar)) {
        found = start;
        break;
      }
    }
    if (found < 0) break;
    matches.push({ start: found, end: found + needle.length });
    cursor = found + needle.length;
  }
  return matches;
}

export function sliceRangeToLeaves(match: FindRange, leaves: ProjectedLeaf[]): LeafFragment[] {
  const fragments: LeafFragment[] = [];
  for (const leaf of leaves) {
    const start = Math.max(match.start, leaf.start);
    const end = Math.min(match.end, leaf.end);
    if (end <= start) continue;
    fragments.push({
      leaf_key: leaf.key,
      start: start - leaf.start,
      end: end - leaf.start,
    });
  }
  return fragments;
}
